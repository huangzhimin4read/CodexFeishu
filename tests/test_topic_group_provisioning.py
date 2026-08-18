from pathlib import Path
from types import SimpleNamespace

from codex_feishu_bridge.feishu.client import ProviderOutcome, ProviderResult
from codex_feishu_bridge.feishu.provisioning import (
    ProvisioningPreflight,
    sync_topic_group_name,
)
from codex_feishu_bridge.runtime_config import ConversationMode, FeishuBinding
from codex_feishu_bridge.runtime_storage import RuntimeStorage


def _binding(tmp_path: Path) -> FeishuBinding:
    contract = tmp_path / "contract.json"
    contract.write_text("{}", encoding="utf-8")
    return FeishuBinding(
        "tenant",
        "app",
        "owner",
        "fallback-p2p",
        "credential",
        contract,
        ConversationMode.TOPIC_GROUP,
        "topic-chat",
    )


def _contract():
    return SimpleNamespace(
        tenant_key="tenant",
        app_id="app",
        bot_enabled=True,
        endpoints={name: object() for name in (
            "tenant_token",
            "send_message",
            "reply_message",
            "upload_image",
            "preflight_chat",
        )},
        contract_hash="hash",
        supports_conversation_mode=lambda mode: mode == "topic_group",
    )


class ChatClient:
    def __init__(self, *, chat_mode: str, group_message_type: str | None = None) -> None:
        self.chat_mode = chat_mode
        self.group_message_type = group_message_type

    def call(self, endpoint: str, **kwargs):
        assert endpoint == "preflight_chat"
        assert kwargs["path_parameters"] == {"chat_id": "topic-chat"}
        return ProviderResult(
            ProviderOutcome.CONFIRMED,
            "0",
            response={
                "code": 0,
                "data": {
                    "chat_mode": self.chat_mode,
                    "chat_type": "private",
                    "group_message_type": self.group_message_type,
                },
            },
        )


def test_topic_group_preflight_accepts_current_topic_chat_shape(tmp_path: Path) -> None:
    with RuntimeStorage(tmp_path / "db.sqlite") as storage:
        storage.initialize_runtime(sink_mode="outbound")
        result = ProvisioningPreflight(
            storage, ChatClient(chat_mode="topic"), _binding(tmp_path), _contract()
        ).run(live=True)
        assert result.passed
        identity = storage.connection.execute(
            "SELECT active_chat_id,active_chat_type,conversation_mode,state "
            "FROM identity_bindings WHERE binding_key='owner'"
        ).fetchone()
        assert tuple(identity) == ("topic-chat", "group", "topic_group", "active")
        breaker = storage.connection.execute(
            "SELECT state,reason FROM circuit_breakers WHERE breaker_name='provisioning'"
        ).fetchone()
        assert tuple(breaker) == ("closed", None)


def test_normal_group_chat_cannot_pass_topic_group_preflight(tmp_path: Path) -> None:
    with RuntimeStorage(tmp_path / "db.sqlite") as storage:
        storage.initialize_runtime(sink_mode="outbound")
        result = ProvisioningPreflight(
            storage,
            ChatClient(chat_mode="group", group_message_type="chat"),
            _binding(tmp_path),
            _contract(),
        ).run(live=True)
        assert not result.passed
        assert result.failures == ("live_topic_group_shape",)
        identity = storage.connection.execute(
            "SELECT state FROM identity_bindings WHERE binding_key='owner'"
        ).fetchone()
        assert identity["state"] == "active"
        breaker = storage.connection.execute(
            "SELECT state FROM circuit_breakers WHERE breaker_name='provisioning'"
        ).fetchone()
        assert breaker["state"] == "closed"


def test_legacy_private_thread_form_group_remains_explicitly_supported(tmp_path: Path) -> None:
    with RuntimeStorage(tmp_path / "db.sqlite") as storage:
        storage.initialize_runtime(sink_mode="outbound")
        result = ProvisioningPreflight(
            storage,
            ChatClient(chat_mode="group", group_message_type="thread"),
            _binding(tmp_path),
            _contract(),
        ).run(live=True)
        assert result.passed


def test_group_name_sync_uses_bot_api_and_verifies_the_project_name(tmp_path: Path) -> None:
    class RenameClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        def call(self, endpoint: str, **kwargs):
            self.calls.append((endpoint, kwargs))
            if endpoint == "update_chat":
                return ProviderResult(ProviderOutcome.CONFIRMED, "0", response={"code": 0})
            return ProviderResult(
                ProviderOutcome.CONFIRMED,
                "0",
                response={"code": 0, "data": {"name": "CodexFeishu"}},
            )

    client = RenameClient()
    result = sync_topic_group_name(client, _binding(tmp_path), "CodexFeishu")
    assert result.passed
    assert result.verified_name == "CodexFeishu"
    assert [call[0] for call in client.calls] == ["update_chat", "preflight_chat"]
    assert client.calls[0][1]["json_body"] == {"name": "CodexFeishu"}
