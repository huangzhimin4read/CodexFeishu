"""Validated loader for the exported per-tenant endpoint contract."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from ..security.jcs import canonicalize


class ContractError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RateLimit:
    capacity: int
    refill_per_second: float
    user_chat_capacity: int | None = None
    user_chat_refill_per_second: float | None = None

    def __post_init__(self) -> None:
        if self.capacity <= 0 or self.refill_per_second <= 0:
            raise ContractError("rate limits must be positive")
        if (self.user_chat_capacity is None) != (self.user_chat_refill_per_second is None):
            raise ContractError("per-user-chat rate limit must be fully specified")


@dataclass(frozen=True, slots=True)
class EndpointContract:
    name: str
    method: str
    path: str
    exact_scopes: tuple[str, ...]
    administrator_approved: bool
    enabled: bool
    rate_limit: RateLimit
    receive_id_type: str | None = None
    uuid_window_seconds: int | None = None
    uuid_location: str | None = None
    response_message_id_pointer: str | None = None
    response_thread_id_pointer: str | None = None
    response_chat_id_pointer: str | None = None
    response_image_key_pointer: str | None = None
    response_file_key_pointer: str | None = None
    response_binary: bool = False

    def __post_init__(self) -> None:
        if self.method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise ContractError(f"unsupported method for {self.name}")
        if not self.path.startswith("/open-apis/") or "//" in self.path:
            raise ContractError(f"invalid path for {self.name}")
        if self.enabled and self.name != "tenant_token":
            if not self.exact_scopes or not self.administrator_approved:
                raise ContractError(f"enabled endpoint {self.name} lacks approved exact scopes")
            if any(value.startswith("REPLACE_") for value in self.exact_scopes):
                raise ContractError(f"enabled endpoint {self.name} still contains scope placeholders")
        if self.enabled and self.name in {"send_message", "reply_message"}:
            if not self.response_message_id_pointer:
                raise ContractError(f"{self.name} must bind a response message-id pointer")
            if self.uuid_location != "json_body":
                raise ContractError(f"{self.name} must bind UUID to the JSON request body")
        if self.uuid_window_seconds is not None and self.uuid_window_seconds <= 0:
            raise ContractError("uuid window must be positive")
        if self.uuid_location is not None and self.uuid_location not in {"json_body", "query"}:
            raise ContractError("unsupported UUID location")
        if self.enabled and self.name == "create_chat":
            if self.uuid_location != "query" or not self.response_chat_id_pointer:
                raise ContractError("create_chat must bind query UUID and response chat id")
        if self.enabled and self.name == "upload_image":
            if (
                self.method != "POST"
                or self.exact_scopes != ("im:resource",)
                or not self.response_image_key_pointer
            ):
                raise ContractError(
                    "upload_image must bind POST, exact im:resource scope, and a response image key"
                )
        if self.enabled and self.name == "upload_file":
            if (
                self.method != "POST"
                or self.path != "/open-apis/im/v1/files"
                or self.exact_scopes != ("im:resource",)
                or not self.response_file_key_pointer
            ):
                raise ContractError(
                    "upload_file must bind the official POST endpoint, exact im:resource scope, and a response file key"
                )
        if self.enabled and self.name == "download_message_resource":
            if (
                self.method != "GET"
                or self.path
                != "/open-apis/im/v1/messages/{message_id}/resources/{file_key}"
                or not self.response_binary
                or not set(self.exact_scopes).intersection(
                    {"im:message", "im:message:readonly", "im:message.history:readonly"}
                )
            ):
                raise ContractError(
                    "download_message_resource must bind the official binary resource endpoint and an approved message-read scope"
                )


@dataclass(frozen=True, slots=True)
class TenantContract:
    schema_version: int
    tenant_key: str
    app_id: str
    published_version: str
    bot_enabled: bool
    p2p_only: bool
    group_topic_enabled: bool
    endpoints: dict[str, EndpointContract]
    event_subscriptions: tuple[str, ...]
    event_scopes: tuple[str, ...]
    callback_subscriptions: tuple[str, ...]
    source_export_sha256: str
    canonical_json: bytes

    @property
    def contract_hash(self) -> str:
        return sha256(self.canonical_json).hexdigest()

    def endpoint(self, name: str, *, require_enabled: bool = True) -> EndpointContract:
        try:
            endpoint = self.endpoints[name]
        except KeyError as exc:
            raise ContractError(f"endpoint is absent from contract: {name}") from exc
        if require_enabled and not endpoint.enabled:
            raise ContractError(f"endpoint is disabled by contract: {name}")
        return endpoint

    def supports_conversation_mode(self, mode: str) -> bool:
        if mode == "p2p":
            return self.p2p_only
        if mode == "topic_group":
            return self.group_topic_enabled
        return False


_TOP_FIELDS = {
    "schema_version",
    "tenant_key",
    "app_id",
    "published_version",
    "bot_enabled",
    "p2p_only",
    "group_topic_enabled",
    "source_export_sha256",
    "endpoints",
    "event_subscriptions",
    "event_scopes",
    "callback_subscriptions",
}
_ENDPOINT_FIELDS = {
    "name",
    "method",
    "path",
    "exact_scopes",
    "administrator_approved",
    "enabled",
    "receive_id_type",
    "uuid_window_seconds",
    "uuid_location",
    "response_message_id_pointer",
    "response_thread_id_pointer",
    "response_chat_id_pointer",
    "response_image_key_pointer",
    "response_file_key_pointer",
    "response_binary",
    "rate_limit",
}
_RATE_FIELDS = {
    "capacity",
    "refill_per_second",
    "user_chat_capacity",
    "user_chat_refill_per_second",
}


def _exact_fields(value: dict[str, Any], allowed: set[str], context: str) -> None:
    extra = set(value) - allowed
    if extra:
        raise ContractError(f"unknown {context} fields: {sorted(extra)}")


def load_tenant_contract(path: Path) -> TenantContract:
    with path.resolve().open(encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise ContractError("tenant contract must be an object")
    _exact_fields(raw, _TOP_FIELDS, "contract")
    schema_version = int(raw.get("schema_version", 0))
    if schema_version not in {1, 2}:
        raise ContractError("unsupported tenant contract schema")
    endpoints_raw = raw.get("endpoints")
    if not isinstance(endpoints_raw, list) or not endpoints_raw:
        raise ContractError("contract endpoints must be a non-empty list")
    endpoints: dict[str, EndpointContract] = {}
    for item in endpoints_raw:
        if not isinstance(item, dict):
            raise ContractError("endpoint entries must be objects")
        _exact_fields(item, _ENDPOINT_FIELDS, "endpoint")
        rate_raw = item.get("rate_limit")
        if not isinstance(rate_raw, dict):
            raise ContractError("rate_limit must be an object")
        _exact_fields(rate_raw, _RATE_FIELDS, "rate limit")
        rate = RateLimit(
            capacity=int(rate_raw["capacity"]),
            refill_per_second=float(rate_raw["refill_per_second"]),
            user_chat_capacity=(
                int(rate_raw["user_chat_capacity"])
                if rate_raw.get("user_chat_capacity") is not None
                else None
            ),
            user_chat_refill_per_second=(
                float(rate_raw["user_chat_refill_per_second"])
                if rate_raw.get("user_chat_refill_per_second") is not None
                else None
            ),
        )
        endpoint = EndpointContract(
            name=str(item["name"]),
            method=str(item["method"]).upper(),
            path=str(item["path"]),
            exact_scopes=tuple(str(value) for value in item.get("exact_scopes", [])),
            administrator_approved=bool(item["administrator_approved"]),
            enabled=bool(item["enabled"]),
            rate_limit=rate,
            receive_id_type=(str(item["receive_id_type"]) if item.get("receive_id_type") else None),
            uuid_window_seconds=(
                int(item["uuid_window_seconds"])
                if item.get("uuid_window_seconds") is not None
                else None
            ),
            uuid_location=(
                str(item["uuid_location"])
                if item.get("uuid_location")
                else None
            ),
            response_message_id_pointer=(
                str(item["response_message_id_pointer"])
                if item.get("response_message_id_pointer")
                else None
            ),
            response_thread_id_pointer=(
                str(item["response_thread_id_pointer"])
                if item.get("response_thread_id_pointer")
                else None
            ),
            response_chat_id_pointer=(
                str(item["response_chat_id_pointer"])
                if item.get("response_chat_id_pointer")
                else None
            ),
            response_image_key_pointer=(
                str(item["response_image_key_pointer"])
                if item.get("response_image_key_pointer")
                else None
            ),
            response_file_key_pointer=(
                str(item["response_file_key_pointer"])
                if item.get("response_file_key_pointer")
                else None
            ),
            response_binary=bool(item.get("response_binary", False)),
        )
        if endpoint.name in endpoints:
            raise ContractError(f"duplicate endpoint contract: {endpoint.name}")
        endpoints[endpoint.name] = endpoint
    tenant_key = str(raw.get("tenant_key", ""))
    app_id = str(raw.get("app_id", ""))
    published_version = str(raw.get("published_version", ""))
    export_hash = str(raw.get("source_export_sha256", ""))
    if not all((tenant_key, app_id, published_version)) or len(export_hash) != 64:
        raise ContractError("contract identity/export hash is incomplete")
    if set(export_hash) == {"0"} or any(
        value.startswith("REPLACE_") for value in (tenant_key, app_id, published_version)
    ):
        raise ContractError("tenant contract still contains identity/export placeholders")
    bot_enabled = bool(raw.get("bot_enabled"))
    p2p_only = bool(raw.get("p2p_only"))
    group_topic_enabled = bool(raw.get("group_topic_enabled", False))
    if not bot_enabled or not (p2p_only or group_topic_enabled):
        raise ContractError("contract must enable the bot and one supported conversation mode")
    if p2p_only and group_topic_enabled:
        raise ContractError("p2p_only and group_topic_enabled are mutually exclusive")
    event_subscriptions = tuple(str(value) for value in raw.get("event_subscriptions", []))
    event_scopes = tuple(str(value) for value in raw.get("event_scopes", []))
    callback_subscriptions = tuple(
        str(value) for value in raw.get("callback_subscriptions", [])
    )
    if any(not value or value.startswith("REPLACE_") for value in (
        *event_subscriptions,
        *event_scopes,
        *callback_subscriptions,
    )):
        raise ContractError("event/callback contract contains an empty or placeholder value")
    return TenantContract(
        schema_version=schema_version,
        tenant_key=tenant_key,
        app_id=app_id,
        published_version=published_version,
        bot_enabled=bot_enabled,
        p2p_only=p2p_only,
        group_topic_enabled=group_topic_enabled,
        endpoints=endpoints,
        event_subscriptions=event_subscriptions,
        event_scopes=event_scopes,
        callback_subscriptions=callback_subscriptions,
        source_export_sha256=export_hash,
        canonical_json=canonicalize(raw),
    )
