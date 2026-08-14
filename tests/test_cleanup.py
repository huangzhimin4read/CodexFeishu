import json
import unicodedata
from pathlib import Path

from codex_feishu_bridge.feishu.cleanup import CleanupWorker, schedule_legacy_marker_cleanup
from codex_feishu_bridge.feishu.client import ProviderOutcome, ProviderResult
from codex_feishu_bridge.runtime_storage import RuntimeStorage, utc_now


class RecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def call(self, endpoint: str, **kwargs) -> ProviderResult:
        self.calls.append((endpoint, kwargs))
        if endpoint == "delete_message":
            return ProviderResult(ProviderOutcome.PERMANENT, "not_withdrawable")
        return ProviderResult(ProviderOutcome.CONFIRMED, "0")


def test_cleanup_archive_update_includes_required_message_type(tmp_path: Path) -> None:
    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="outbound")
        outbox_id = storage.enqueue_provider_message(
            logical_message_id="logical",
            operation="commentary",
            endpoint_name="send_message",
            stable_uuid="uuid",
            marker="marker",
            body_json="{}",
            body_hash="hash",
            priority=1,
        )
        storage.connection.execute(
            "UPDATE provider_outbox SET state='confirmed',provider_message_id='message' "
            "WHERE outbox_id=?",
            (outbox_id,),
        )
        now = utc_now()
        storage.connection.execute(
            "INSERT INTO transient_messages(message_id,turn_id,message_type,lifecycle_state,"
            "created_at,updated_at) VALUES('message','turn','commentary','cleanup_queued',?,?)",
            (now, now),
        )
        client = RecordingClient()
        assert CleanupWorker(storage, client).run_once() is True
        endpoint, kwargs = client.calls[-1]
        assert endpoint == "update_message"
        assert kwargs["json_body"]["msg_type"] == "text"
        state = storage.connection.execute(
            "SELECT lifecycle_state FROM transient_messages WHERE message_id='message'"
        ).fetchone()[0]
        assert state == "archived"


def test_legacy_marker_cleanup_only_queues_messages_still_visible(tmp_path: Path) -> None:
    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="outbound")
        now = utc_now()
        for operation, provider_id, lifecycle in (
            ("final", "final-message", None),
            ("commentary", "active-message", "transient_active"),
            ("commentary", "withdrawn-message", "withdrawn"),
        ):
            marker = "cfb:" + provider_id.ljust(24, "0")[:24]
            body = json.dumps(
                {"text": f"正文\n\u2063{marker}"}, ensure_ascii=False, separators=(",", ":")
            )
            outbox_id = storage.enqueue_provider_message(
                logical_message_id="source:" + provider_id,
                thread_id="thread",
                operation=operation,
                endpoint_name="reply_message",
                target_message_id="anchor",
                stable_uuid="uuid:" + provider_id,
                marker=marker,
                body_json=body,
                body_hash="legacy-hash",
                priority=1,
            )
            storage.connection.execute(
                "UPDATE provider_outbox SET state='confirmed',provider_message_id=? WHERE outbox_id=?",
                (provider_id, outbox_id),
            )
            if lifecycle is not None:
                storage.connection.execute(
                    "INSERT INTO transient_messages(message_id,turn_id,message_type,lifecycle_state,"
                    "created_at,updated_at) VALUES(?,?,'text',?,?,?)",
                    (provider_id, provider_id, lifecycle, now, now),
                )

        first = schedule_legacy_marker_cleanup(storage)
        assert first.queued == 2
        second = schedule_legacy_marker_cleanup(storage)
        assert second.queued == 0
        rows = storage.connection.execute(
            "SELECT target_message_id,body_json FROM provider_outbox "
            "WHERE operation='marker_cleanup' ORDER BY target_message_id"
        ).fetchall()
        assert [row["target_message_id"] for row in rows] == [
            "active-message",
            "final-message",
        ]
        assert all(json.loads(row["body_json"]) == {"text": "正文"} for row in rows)
        assert all(
            not any(
                unicodedata.category(character) == "Cf"
                for character in json.loads(row["body_json"])["text"]
            )
            for row in rows
        )
