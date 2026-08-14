"""Provider history reconciliation for uncertain sends."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from ..runtime_storage import RuntimeStorage
from .client import FeishuClient, ProviderOutcome


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    state: str
    message_id: str | None
    matches: int


class SendReconciler:
    def __init__(self, storage: RuntimeStorage, client: FeishuClient) -> None:
        self.storage = storage
        self.client = client

    def reconcile(self, outbox_id: int) -> ReconciliationResult:
        row = self.storage.connection.execute(
            "SELECT * FROM provider_outbox WHERE outbox_id=? AND state='unknown'", (outbox_id,)
        ).fetchone()
        if row is None:
            raise ValueError("outbox row is not unknown")
        first_attempt = datetime.fromisoformat(str(row["first_attempt_at"]).replace("Z", "+00:00"))
        send_endpoint = self.client.contract.endpoint(row["endpoint_name"])
        age = (datetime.now(UTC) - first_attempt).total_seconds()
        if send_endpoint.uuid_window_seconds is not None and age <= send_endpoint.uuid_window_seconds:
            now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            self.storage.connection.execute(
                "UPDATE provider_outbox SET state='retryable',next_attempt_at=?,updated_at=? "
                "WHERE outbox_id=? AND state='unknown'",
                (now, now, outbox_id),
            )
            return ReconciliationResult("retry_with_same_uuid", None, 0)
        endpoint = self.client.contract.endpoint("list_messages", require_enabled=False)
        if not endpoint.enabled:
            return ReconciliationResult("delivery_indeterminate", None, 0)
        binding = self.storage.connection.execute(
            "SELECT chat_id FROM task_bindings WHERE thread_id=?", (row["thread_id"],)
        ).fetchone()
        if binding is None:
            return ReconciliationResult("delivery_indeterminate", None, 0)
        expected_user_open_id: str | None = None
        if row["operation"] == "user_message":
            identity = self.storage.connection.execute(
                "SELECT owner_open_id FROM identity_bindings "
                "WHERE binding_key='owner' AND state='active'"
            ).fetchone()
            if identity is None:
                return ReconciliationResult("delivery_indeterminate", None, 0)
            expected_user_open_id = str(identity["owner_open_id"])
        page_token: str | None = None
        matches: list[str] = []
        while True:
            query = {
                "container_id_type": "chat",
                "container_id": binding["chat_id"],
                "page_size": "50",
                "start_time": str(int(first_attempt.timestamp()) - 60),
                "end_time": str(int(datetime.now(UTC).timestamp()) + 60),
            }
            if page_token:
                query["page_token"] = page_token
            response = self.client.call("list_messages", query=query, chat_id=binding["chat_id"])
            if response.outcome is not ProviderOutcome.CONFIRMED or not response.response:
                return ReconciliationResult("delivery_indeterminate", None, len(matches))
            data = response.response.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("items", []), list):
                return ReconciliationResult("delivery_indeterminate", None, len(matches))
            for item in data.get("items", []):
                if not isinstance(item, dict):
                    continue
                sender = item.get("sender")
                sender_type = sender.get("sender_type") if isinstance(sender, dict) else None
                sender_id = sender.get("id") if isinstance(sender, dict) else None
                if row["operation"] == "user_message":
                    if sender_type != "user" or sender_id != expected_user_open_id:
                        continue
                elif sender_type not in {"app", "bot"} or sender_id != self.client.app_id:
                    continue
                expected_type = (
                    "interactive" if row["operation"] == "approval" else row["message_type"]
                )
                if item.get("msg_type") != expected_type:
                    continue
                body = item.get("body")
                content = body.get("content") if isinstance(body, dict) else None
                if not isinstance(content, str):
                    continue
                # Provider-visible bodies deliberately carry no machine marker:
                # Feishu mobile clients render Unicode tag characters as garbage.
                # Exact body hash, sender/app, type, chat and bounded time window
                # form the match; anything other than one result stays indeterminate.
                if sha256(content.encode("utf-8")).hexdigest() == row["body_hash"]:
                    message_id = item.get("message_id")
                    if isinstance(message_id, str):
                        matches.append(message_id)
            has_more = bool(data.get("has_more"))
            next_token = data.get("page_token")
            if not has_more:
                break
            if not isinstance(next_token, str) or not next_token or next_token == page_token:
                return ReconciliationResult("delivery_indeterminate", None, len(matches))
            page_token = next_token
        if len(matches) == 1:
            self.storage.connection.execute(
                "UPDATE provider_outbox SET state='confirmed',provider_message_id=?,updated_at=? "
                "WHERE outbox_id=? AND state='unknown'",
                (matches[0], datetime.now(UTC).isoformat().replace("+00:00", "Z"), outbox_id),
            )
            return ReconciliationResult("confirmed", matches[0], 1)
        self.storage.connection.execute(
            "UPDATE provider_outbox SET state='delivery_indeterminate',updated_at=? "
            "WHERE outbox_id=? AND state='unknown'",
            (datetime.now(UTC).isoformat().replace("+00:00", "Z"), outbox_id),
        )
        return ReconciliationResult("delivery_indeterminate", None, len(matches))
