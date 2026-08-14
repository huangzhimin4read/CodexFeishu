"""Startup and periodic tenant-drift preflight."""

from __future__ import annotations

from dataclasses import dataclass

from ..runtime_config import FeishuBinding, RemoteCapabilities
from ..runtime_storage import RuntimeStorage, utc_now
from .client import FeishuClient, ProviderOutcome
from .contracts import TenantContract


@dataclass(frozen=True, slots=True)
class PreflightResult:
    passed: bool
    checks: tuple[str, ...]
    failures: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GroupNameSyncResult:
    passed: bool
    expected_name: str
    verified_name: str | None
    provider_code: str


class ProvisioningPreflight:
    def __init__(
        self,
        storage: RuntimeStorage,
        client: FeishuClient,
        binding: FeishuBinding,
        contract: TenantContract,
    ) -> None:
        self.storage = storage
        self.client = client
        self.binding = binding
        self.contract = contract

    def run(
        self, *, live: bool, remote: RemoteCapabilities | None = None
    ) -> PreflightResult:
        remote = remote or RemoteCapabilities()
        checks: list[str] = []
        failures: list[str] = []
        if self.contract.tenant_key != self.binding.tenant_key:
            failures.append("tenant_key_mismatch")
        else:
            checks.append("tenant_key")
        if self.contract.app_id != self.binding.app_id:
            failures.append("app_id_mismatch")
        else:
            checks.append("app_id")
        mode = self.binding.conversation_mode.value
        if not self.contract.bot_enabled or not self.contract.supports_conversation_mode(mode):
            failures.append("bot_capability")
        else:
            checks.append("topic_group_bot" if mode == "topic_group" else "p2p_bot")
        required = {
            "tenant_token",
            "send_message",
            "reply_message",
            "upload_image",
            "preflight_chat",
        }
        if remote.images or remote.files:
            required.add("download_message_resource")
        missing = sorted(required - set(self.contract.endpoints))
        if missing:
            failures.append("missing_endpoints:" + ",".join(missing))
        else:
            checks.append("required_endpoints")
        if remote.receives_messages:
            if (
                "im.message.receive_v1" not in self.contract.event_subscriptions
                or "im:message.group_msg" not in self.contract.event_scopes
            ):
                failures.append("message_receive_contract")
            else:
                checks.append("message_receive_contract")
        if remote.approvals:
            if "card.action.trigger" not in self.contract.callback_subscriptions:
                failures.append("card_callback_contract")
            else:
                checks.append("card_callback_contract")
        if live and not failures:
            try:
                response = self.client.call(
                    "preflight_chat",
                    path_parameters={"chat_id": self.binding.target_chat_id},
                    chat_id=self.binding.target_chat_id,
                )
            except Exception:
                failures.append("live_preflight_exception")
            else:
                if response.outcome is ProviderOutcome.CONFIRMED:
                    if mode == "topic_group" and not _is_private_topic_group(response.response):
                        failures.append("live_topic_group_shape")
                    else:
                        checks.append("live_chat_visibility")
                        if mode == "topic_group":
                            checks.append("live_private_topic_group")
                else:
                    failures.append("live_chat_visibility")
        state = "active" if not failures else "drifted"
        self.storage.connection.execute(
            "INSERT INTO identity_bindings(binding_key,tenant_key,app_id,owner_open_id,p2p_chat_id,"
            "active_chat_id,active_chat_type,conversation_mode,binding_epoch,contract_hash,state,updated_at) "
            "VALUES('owner',?,?,?,?,?,?,?,1,?,?,?) "
            "ON CONFLICT(binding_key) DO UPDATE SET state=excluded.state,contract_hash=excluded.contract_hash,"
            "active_chat_id=excluded.active_chat_id,active_chat_type=excluded.active_chat_type,"
            "conversation_mode=excluded.conversation_mode,updated_at=excluded.updated_at",
            (
                self.binding.tenant_key,
                self.binding.app_id,
                self.binding.owner_open_id,
                self.binding.p2p_chat_id,
                self.binding.target_chat_id,
                self.binding.target_chat_type,
                mode,
                self.contract.contract_hash,
                state,
                utc_now(),
            ),
        )
        if failures:
            self.storage.connection.execute(
                "INSERT INTO circuit_breakers(breaker_name,state,reason,updated_at) VALUES('provisioning','open',?,?) "
                "ON CONFLICT(breaker_name) DO UPDATE SET state='open',reason=excluded.reason,updated_at=excluded.updated_at",
                (",".join(failures), utc_now()),
            )
        else:
            self.storage.connection.execute(
                "INSERT INTO circuit_breakers(breaker_name,state,reason,updated_at) "
                "VALUES('provisioning','closed',NULL,?) "
                "ON CONFLICT(breaker_name) DO UPDATE SET state='closed',reason=NULL,updated_at=excluded.updated_at",
                (utc_now(),),
            )
        return PreflightResult(not failures, tuple(checks), tuple(failures))


def sync_topic_group_name(
    client: FeishuClient,
    binding: FeishuBinding,
    expected_name: str,
) -> GroupNameSyncResult:
    """Set and then independently verify the project topic-group name."""
    if binding.conversation_mode.value != "topic_group":
        raise ValueError("group-name sync requires topic_group mode")
    if not expected_name or len(expected_name) > 60:
        raise ValueError("expected group name must contain 1..60 characters")
    updated = client.call(
        "update_chat",
        path_parameters={"chat_id": binding.target_chat_id},
        json_body={"name": expected_name},
        chat_id=binding.target_chat_id,
    )
    if updated.outcome is not ProviderOutcome.CONFIRMED:
        return GroupNameSyncResult(False, expected_name, None, updated.code)
    verified = client.call(
        "preflight_chat",
        path_parameters={"chat_id": binding.target_chat_id},
        chat_id=binding.target_chat_id,
    )
    data = verified.response.get("data") if verified.response else None
    actual = data.get("name") if isinstance(data, dict) else None
    return GroupNameSyncResult(
        verified.outcome is ProviderOutcome.CONFIRMED and actual == expected_name,
        expected_name,
        actual if isinstance(actual, str) else None,
        verified.code,
    )


def _is_private_topic_group(response: dict | None) -> bool:
    if not isinstance(response, dict):
        return False
    data = response.get("data")
    if not isinstance(data, dict):
        return False
    chat = data.get("chat") if isinstance(data.get("chat"), dict) else data
    if chat.get("chat_type") != "private":
        return False
    # Current Feishu topic groups are returned as chat_mode=topic and do not
    # include group_message_type.  Retain the older documented thread-form
    # group representation as an explicit second shape; an ordinary group is
    # never accepted by either branch.
    return chat.get("chat_mode") == "topic" or (
        chat.get("chat_mode") == "group"
        and chat.get("group_message_type") == "thread"
    )
