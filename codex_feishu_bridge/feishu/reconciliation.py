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

    def _mark_indeterminate(
        self, outbox_id: int, *, matches: int, reason: str
    ) -> ReconciliationResult:
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        self.storage.connection.execute(
            "UPDATE provider_outbox SET state='delivery_indeterminate',last_error_code=?,"
            "updated_at=? WHERE outbox_id=? AND state='unknown'",
            (reason, now, outbox_id),
        )
        return ReconciliationResult("delivery_indeterminate", None, matches)

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
            return self._mark_indeterminate(
                outbox_id, matches=0, reason="reconciliation_endpoint_disabled"
            )
        binding = self.storage.connection.execute(
            "SELECT chat_id FROM task_bindings WHERE thread_id=?", (row["thread_id"],)
        ).fetchone()
        if binding is None:
            return self._mark_indeterminate(
                outbox_id, matches=0, reason="reconciliation_binding_missing"
            )
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
                return self._mark_indeterminate(
                    outbox_id,
                    matches=len(matches),
                    reason="reconciliation_provider_unavailable",
                )
            data = response.response.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("items", []), list):
                return self._mark_indeterminate(
                    outbox_id,
                    matches=len(matches),
                    reason="reconciliation_invalid_response",
                )
            for item in data.get("items", []):
                if not isinstance(item, dict):
                    continue
                sender = item.get("sender")
                sender_type = sender.get("sender_type") if isinstance(sender, dict) else None
                sender_id = sender.get("id") if isinstance(sender, dict) else None
                if row["operation"] == "user_message":
                    if sender_type != "user":
                        continue
                elif sender_type not in {"app", "bot"} or sender_id != self.client.app_id:
                    continue
                try:
                    persisted_body = json.loads(row["body_json"])
                except (TypeError, json.JSONDecodeError):
                    persisted_body = None
                is_rich_post = (
                    isinstance(persisted_body, dict)
                    and persisted_body.get("_cfb_message_type") == "post"
                )
                expected_type = (
                    "interactive"
                    if row["operation"] == "approval"
                    else "post"
                    if is_rich_post
                    else row["message_type"]
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
                return self._mark_indeterminate(
                    outbox_id,
                    matches=len(matches),
                    reason="reconciliation_invalid_page_token",
                )
            page_token = next_token
        if len(matches) == 1:
            self.storage.connection.execute(
                "UPDATE provider_outbox SET state='confirmed',provider_message_id=?,updated_at=? "
                "WHERE outbox_id=? AND state='unknown'",
                (matches[0], datetime.now(UTC).isoformat().replace("+00:00", "Z"), outbox_id),
            )
            return ReconciliationResult("confirmed", matches[0], 1)
        return self._mark_indeterminate(
            outbox_id,
            matches=len(matches),
            reason="reconciliation_no_unique_match",
        )
