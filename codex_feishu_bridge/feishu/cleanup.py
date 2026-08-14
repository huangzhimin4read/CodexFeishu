"""Final-first transient-message cleanup with contract-driven degradation."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from hashlib import sha256

from ..runtime_storage import RuntimeStorage, utc_now
from .client import FeishuClient, ProviderOutcome


_CLEANUP_NAMESPACE = uuid.UUID("b8561f90-fb75-57cd-8e40-a30da87160e2")


@dataclass(frozen=True, slots=True)
class MarkerCleanupSchedule:
    scanned: int
    queued: int


def _provider_clean_body(body_json: str, marker: str) -> str | None:
    try:
        body = json.loads(body_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(body, dict) or not isinstance(body.get("text"), str):
        return None
    text = body["text"]
    if not marker or not text.endswith(marker):
        return None
    cleaned = text[: -len(marker)]
    # The earliest bridge build placed an invisible separator immediately
    # before a plaintext marker. Remove only that exact legacy suffix; do not
    # strip arbitrary format characters from user-authored text or emoji.
    if cleaned.endswith("\n\u2063"):
        cleaned = cleaned[:-2]
    elif cleaned.endswith("\u2063"):
        cleaned = cleaned[:-1]
    clean_body = dict(body)
    clean_body["text"] = cleaned
    return json.dumps(clean_body, ensure_ascii=False, separators=(",", ":"))


def schedule_legacy_marker_cleanup(storage: RuntimeStorage) -> MarkerCleanupSchedule:
    """Queue safe edits for confirmed bridge text still carrying a UI marker."""

    candidates = storage.connection.execute(
        "SELECT outbox.outbox_id,outbox.thread_id,outbox.provider_message_id,outbox.marker,"
        "outbox.body_json FROM provider_outbox outbox "
        "LEFT JOIN transient_messages transient ON transient.message_id=outbox.provider_message_id "
        "WHERE outbox.state='confirmed' AND outbox.message_type='text' "
        "AND outbox.provider_message_id IS NOT NULL "
        "AND (outbox.operation='final' OR (outbox.operation='commentary' "
        "AND transient.lifecycle_state='transient_active')) ORDER BY outbox.outbox_id"
    ).fetchall()
    queued = 0
    now = utc_now()
    with storage.immediate() as connection:
        for row in candidates:
            clean_body = _provider_clean_body(row["body_json"], row["marker"])
            if clean_body is None:
                continue
            body_hash = sha256(clean_body.encode("utf-8")).hexdigest()
            logical_id = f"marker-cleanup:{row['outbox_id']}:{body_hash[:16]}"
            result = connection.execute(
                "INSERT OR IGNORE INTO provider_outbox(logical_message_id,thread_id,item_id,operation,"
                "endpoint_name,target_message_id,stable_uuid,marker,body_json,body_hash,priority,state,"
                "next_attempt_at,created_at,updated_at) VALUES(?,?,?,'marker_cleanup','update_message',"
                "?,?,?,?,?,1,'pending',?,?,?)",
                (
                    logical_id,
                    row["thread_id"],
                    str(row["outbox_id"]),
                    row["provider_message_id"],
                    str(uuid.uuid5(_CLEANUP_NAMESPACE, logical_id)),
                    "local-only:" + body_hash[:24],
                    clean_body,
                    body_hash,
                    now,
                    now,
                    now,
                ),
            )
            queued += int(result.rowcount == 1)
    return MarkerCleanupSchedule(len(candidates), queued)


class CleanupWorker:
    def __init__(self, storage: RuntimeStorage, client: FeishuClient) -> None:
        self.storage = storage
        self.client = client

    def run_once(self) -> bool:
        row = self.storage.connection.execute(
            "SELECT * FROM transient_messages WHERE lifecycle_state='cleanup_queued' "
            "ORDER BY created_at,message_id LIMIT 1"
        ).fetchone()
        if row is None:
            return False
        ownership = self.storage.connection.execute(
            "SELECT 1 FROM provider_outbox WHERE provider_message_id=? AND state='confirmed'",
            (row["message_id"],),
        ).fetchone()
        if ownership is None:
            self._transition(row["message_id"], "retention_policy_blocked")
            return True
        try:
            result = self.client.call(
                "delete_message", path_parameters={"message_id": row["message_id"]}
            )
        except Exception:
            result = None
        if result is not None and result.outcome is ProviderOutcome.CONFIRMED:
            self._transition(row["message_id"], "withdrawn")
            return True
        archive_text = json.dumps({"text": "过程消息已归档；最终结果保留。"}, ensure_ascii=False)
        try:
            update = self.client.call(
                "update_message",
                path_parameters={"message_id": row["message_id"]},
                json_body={"msg_type": "text", "content": archive_text},
            )
        except Exception:
            update = None
        if update is not None and update.outcome is ProviderOutcome.CONFIRMED:
            self._transition(row["message_id"], "archived")
        else:
            self._transition(row["message_id"], "retention_policy_blocked")
        return True

    def _transition(self, message_id: str, state: str) -> None:
        self.storage.connection.execute(
            "UPDATE transient_messages SET lifecycle_state=?,operation_count=operation_count+1,"
            "updated_at=? WHERE message_id=? AND lifecycle_state='cleanup_queued'",
            (state, utc_now(), message_id),
        )
