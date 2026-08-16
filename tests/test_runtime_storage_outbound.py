import json
import sqlite3
import unicodedata
from hashlib import sha256
from pathlib import Path

import pytest

from codex_feishu_bridge.codex.desktop_dispatch import desktop_submission_text_hash
from codex_feishu_bridge.feishu.client import ProviderOutcome, ProviderResult
from codex_feishu_bridge.feishu.outbound import (
    OutboundPipeline,
    OutboxWorker,
    suppress_queued_internal_user_notifications,
)
from codex_feishu_bridge.feishu.tasks import TaskAnchorManager
from codex_feishu_bridge.models import (
    EmbeddedImage,
    EventKind,
    NormalizedEvent,
    RolloutBatch,
    SourceCursor,
)
from codex_feishu_bridge.runtime_storage import RuntimeStorage, utc_now


def _event(
    kind: EventKind,
    item: str,
    text: str,
    *,
    source_type: str = "fixture",
) -> NormalizedEvent:
    return NormalizedEvent("thread", "turn", item, kind, 0, text, source_type)


def _prepare(
    storage: RuntimeStorage, *, conversation_mode: str = "p2p", chat_id: str = "chat"
) -> None:
    storage.connection.execute(
        "INSERT INTO task_bindings(thread_id,project_root,chat_id,anchor_message_id,anchor_state,"
        "anchor_uuid,anchor_marker,conversation_mode,opted_in,updated_at) "
        "VALUES('thread','D:/project',?,'anchor','confirmed','uuid','marker',?,1,?)",
        (chat_id, conversation_mode, utc_now()),
    )


def test_shadow_sink_is_immutable_and_cannot_enqueue(tmp_path: Path) -> None:
    path = tmp_path / "shadow.db"
    with RuntimeStorage(path) as storage:
        storage.initialize_runtime(sink_mode="shadow_only")
        with pytest.raises(PermissionError, match="shadow_only"):
            storage.enqueue_provider_message(
                logical_message_id="x",
                operation="final",
                endpoint_name="reply_message",
                stable_uuid="u",
                marker="m",
                body_json="{}",
                body_hash="h",
                priority=1,
            )
    with RuntimeStorage(path) as storage:
        with pytest.raises(ValueError, match="immutable"):
            storage.initialize_runtime(sink_mode="outbound")


def test_rollout_items_outbox_and_cursor_commit_atomically(tmp_path: Path) -> None:
    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="outbound")
        _prepare(storage)
        batch = RolloutBatch(
            (
                _event(EventKind.COMMENTARY, "one", "progress"),
                _event(EventKind.FINAL_ANSWER, "two", "done"),
            ),
            SourceCursor("source", "file", 100, "hash", "1"),
        )
        result = OutboundPipeline(storage).ingest_rollout_batch(batch)
        assert result.inserted_items == 2 and result.queued_messages == 3
        rows = storage.connection.execute(
            "SELECT operation,state,body_json FROM provider_outbox ORDER BY outbox_id"
        ).fetchall()
        assert [(row["operation"], row["state"]) for row in rows] == [
            ("commentary", "pending"),
            ("commentary", "pending"),
            ("final", "pending"),
        ]
        assert json.loads(rows[-1]["body_json"])["text"] == "🔔【等待你的回应】"
        first = storage.lease_outbox("worker", "2999-01-01T00:00:00Z")
        assert first["operation"] == "commentary"
        assert storage.lease_outbox("other", "2999-01-01T00:00:00Z") is None
        storage.finish_outbox(first["outbox_id"], "worker", state="confirmed", provider_message_id="m1")
        second = storage.lease_outbox("worker", "2999-01-01T00:00:00Z")
        assert second["operation"] == "commentary"
        storage.finish_outbox(second["outbox_id"], "worker", state="confirmed", provider_message_id="m2")
        third = storage.lease_outbox("worker", "2999-01-01T00:00:00Z")
        assert third["operation"] == "final"


def test_expired_outbox_lease_becomes_unknown_before_any_retry(tmp_path: Path) -> None:
    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="control")
        now = utc_now()
        storage.connection.execute(
            "INSERT INTO provider_outbox(logical_message_id,thread_id,turn_id,item_id,operation,"
            "message_type,endpoint_name,target_message_id,reply_in_thread,stable_uuid,marker,body_json,"
            "body_hash,priority,state,next_attempt_at,created_at,updated_at) "
            "VALUES('expired','thread','turn','item','commentary','text','reply_message','anchor',1,"
            "'uuid','marker','{}','hash',100,'pending',?,?,?)",
            (now, now, now),
        )
        leased = storage.lease_outbox("dead-worker", "2000-01-01T00:00:00Z")
        assert leased is not None and leased["state"] == "leased"

        assert storage.lease_outbox("new-worker", "2999-01-01T00:00:00Z") is None
        recovered = storage.connection.execute(
            "SELECT state,lease_owner,lease_expires_at,last_error_code FROM provider_outbox "
            "WHERE logical_message_id='expired'"
        ).fetchone()
        assert tuple(recovered) == ("unknown", None, None, "lease_expired")


def test_queued_subagent_notification_is_retired_and_releases_following_message(
    tmp_path: Path,
) -> None:
    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="control")
        _prepare(storage)
        now = utc_now()
        internal_body = json.dumps(
            {
                "text": (
                    "<subagent_notification>\ninternal\n"
                    "</subagent_notification>"
                )
            }
        )
        for logical_id, item_id, operation, body, state in (
            ("internal", "internal", "user_message", internal_body, "retryable"),
            ("visible", "visible", "commentary", '{"text":"visible"}', "pending"),
        ):
            storage.connection.execute(
                "INSERT INTO provider_outbox(logical_message_id,thread_id,turn_id,item_id,operation,"
                "message_type,endpoint_name,target_message_id,reply_in_thread,stable_uuid,marker,"
                "body_json,body_hash,priority,state,next_attempt_at,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'text','reply_message','anchor',1,?,?,?,?,'100',?,?,?,?)",
                (
                    logical_id,
                    "thread",
                    "turn",
                    item_id,
                    operation,
                    logical_id + "-uuid",
                    logical_id + "-marker",
                    body,
                    sha256(body.encode()).hexdigest(),
                    state,
                    now,
                    now,
                    now,
                ),
            )

        assert suppress_queued_internal_user_notifications(storage) == 1
        internal = storage.connection.execute(
            "SELECT state,last_error_code FROM provider_outbox WHERE logical_message_id='internal'"
        ).fetchone()
        assert tuple(internal) == (
            "permanent",
            "internal_user_notification_suppressed",
        )
        leased = storage.lease_outbox("worker", "2999-01-01T00:00:00Z")
        assert leased is not None and leased["logical_message_id"] == "visible"


def test_codex_user_message_is_mirrored_once_but_exact_feishu_return_is_suppressed(
    tmp_path: Path,
) -> None:
    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="outbound")
        _prepare(storage)
        storage.connection.execute(
            "INSERT INTO dispatch_records(dispatch_attempt_id,ingress_message_id,thread_id,"
            "client_user_message_id,profile_hash,binding_epoch,identity_binding_epoch,fencing_token,"
            "server_epoch,connection_epoch,request_hash,state,turn_id,user_item_id,created_at,updated_at) "
            "VALUES('attempt','ingress','thread','client','desktop-host-managed',1,1,1,"
            "'server','connection','hash','accepted','turn','from-feishu',datetime('now'),datetime('now'))"
        )
        batch = RolloutBatch(
            (
                _event(
                    EventKind.COMMENTARY,
                    "from-feishu",
                    "同一条飞书消息",
                    source_type="user_message",
                ),
                _event(
                    EventKind.COMMENTARY,
                    "from-codex",
                    "Codex 用户消息",
                    source_type="user_message",
                ),
            ),
            SourceCursor("source", "file", 100, "hash", "1"),
        )
        result = OutboundPipeline(storage, owner_display_name="项目所有者").ingest_rollout_batch(batch)
        assert result.inserted_items == 2 and result.queued_messages == 1
        row = storage.connection.execute(
            "SELECT item_id,body_json FROM provider_outbox"
        ).fetchone()
        assert row["item_id"] == "from-codex"
        assert json.loads(row["body_json"])["text"].startswith("👤 项目所有者：Codex 用户消息")

        repeated = OutboundPipeline(storage).ingest_rollout_batch(batch)
        assert repeated.inserted_items == 0 and repeated.queued_messages == 0


def test_codex_user_message_can_be_replied_as_authorized_feishu_user(
    tmp_path: Path,
) -> None:
    class RecordingClient:
        calls: list[tuple[str, dict]] = []

        def call(self, endpoint: str, **kwargs):
            self.calls.append((endpoint, kwargs))
            return ProviderResult(ProviderOutcome.CONFIRMED, "0", message_id="bot-reply")

    class RecordingUserSender:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def reply_text(self, **kwargs):
            self.calls.append(kwargs)
            return ProviderResult(
                ProviderOutcome.CONFIRMED,
                "0",
                message_id="user-reply",
            )

    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="outbound")
        _prepare(storage, conversation_mode="topic_group")
        batch = RolloutBatch(
            (
                _event(
                    EventKind.COMMENTARY,
                    "codex-user",
                    "这是 Codex 端输入",
                    source_type="user_message",
                ),
            ),
            SourceCursor("source", "file", 100, "hash", "1"),
        )
        queued = OutboundPipeline(
            storage,
            owner_display_name="项目所有者",
            user_messages_as_user=True,
        ).ingest_rollout_batch(batch)
        row = storage.connection.execute(
            "SELECT operation,body_json FROM provider_outbox"
        ).fetchone()
        assert queued.queued_messages == 1
        assert row["operation"] == "user_message"
        assert json.loads(row["body_json"])["text"] == "这是 Codex 端输入"

        client = RecordingClient()
        sender = RecordingUserSender()
        assert OutboxWorker(
            storage,
            client,
            "worker",
            user_message_sender=sender,
            owner_display_name="项目所有者",
        ).run_once()

        assert client.calls == []
        assert sender.calls == [
            {
                "message_id": "anchor",
                "text": "这是 Codex 端输入",
                "reply_in_thread": True,
                "idempotency_key": storage.connection.execute(
                    "SELECT stable_uuid FROM provider_outbox"
                ).fetchone()[0],
            }
        ]
        delivered = storage.connection.execute(
            "SELECT state,provider_message_id FROM provider_outbox"
        ).fetchone()
        assert tuple(delivered) == ("confirmed", "user-reply")
        assert storage.connection.execute(
            "SELECT COUNT(*) FROM transient_messages"
        ).fetchone()[0] == 0
        ancestry = storage.connection.execute(
            "SELECT source,thread_id,parent_id FROM message_ancestry WHERE message_id='user-reply'"
        ).fetchone()
        assert tuple(ancestry) == ("outbound", "thread", "anchor")


def test_new_topic_auto_subscribes_through_verified_owner_reply(tmp_path: Path) -> None:
    class RecordingClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        def call(self, endpoint: str, **kwargs):
            self.calls.append((endpoint, kwargs))
            return ProviderResult(
                ProviderOutcome.CONFIRMED,
                "0",
                message_id="anchor",
                thread_id="provider-thread",
            )

    class RecordingUserSender:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def reply_text(self, **kwargs):
            self.calls.append(kwargs)
            return ProviderResult(
                ProviderOutcome.CONFIRMED,
                "0",
                message_id="subscription-reply",
            )

    project = tmp_path / "project"
    project.mkdir()
    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="outbound")
        TaskAnchorManager(storage).opt_in(
            thread_id="thread",
            project_root=project,
            chat_id="topic-chat",
            conversation_mode="topic_group",
            task_title="主力开发",
            project_name="CODEX飞书接口",
        )
        client = RecordingClient()
        sender = RecordingUserSender()
        worker = OutboxWorker(
            storage,
            client,
            "worker",
            user_message_sender=sender,
        )

        assert worker.run_once()
        subscription = storage.connection.execute(
            "SELECT operation,target_message_id,reply_in_thread,body_json,state "
            "FROM provider_outbox WHERE operation='subscription'"
        ).fetchone()
        assert tuple(subscription)[:3] == ("subscription", "anchor", 1)
        assert json.loads(subscription["body_json"])["text"] == "🔔 已订阅任务更新"
        assert subscription["state"] == "pending"

        assert worker.run_once()
        assert sender.calls == [
            {
                "message_id": "anchor",
                "text": "🔔 已订阅任务更新",
                "reply_in_thread": True,
                "idempotency_key": storage.connection.execute(
                    "SELECT stable_uuid FROM provider_outbox WHERE operation='subscription'"
                ).fetchone()[0],
            }
        ]
        assert storage.connection.execute(
            "SELECT state FROM provider_outbox WHERE operation='subscription'"
        ).fetchone()[0] == "confirmed"
        ancestry = storage.connection.execute(
            "SELECT source,parent_id FROM message_ancestry WHERE message_id='subscription-reply'"
        ).fetchone()
        assert tuple(ancestry) == ("outbound", "anchor")


def test_user_cli_auth_failure_falls_back_to_labeled_bot_message(
    tmp_path: Path,
) -> None:
    class RecordingClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        def call(self, endpoint: str, **kwargs):
            self.calls.append((endpoint, kwargs))
            return ProviderResult(ProviderOutcome.CONFIRMED, "0", message_id="bot-reply")

    class ExpiredUserSender:
        def reply_text(self, **kwargs):
            return ProviderResult(ProviderOutcome.PERMANENT, "user_cli_token_expired")

    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="outbound")
        _prepare(storage, conversation_mode="topic_group")
        OutboundPipeline(
            storage,
            user_messages_as_user=True,
        ).ingest_rollout_batch(
            RolloutBatch(
                (
                    _event(
                        EventKind.COMMENTARY,
                        "codex-user",
                        "用户输入",
                        source_type="user_message",
                    ),
                ),
                SourceCursor("source", "file", 100, "hash", "1"),
            )
        )
        client = RecordingClient()
        assert OutboxWorker(
            storage,
            client,
            "worker",
            user_message_sender=ExpiredUserSender(),
            owner_display_name="项目所有者",
        ).run_once()

        assert len(client.calls) == 1
        sent = json.loads(client.calls[0][1]["json_body"]["content"])
        assert sent["text"] == "👤 项目所有者：用户输入"
        fallback = storage.connection.execute(
            "SELECT value FROM runtime_metadata WHERE key='user_cli_last_fallback'"
        ).fetchone()
        assert fallback is not None and "user_cli_token_expired" in fallback[0]


def test_delayed_desktop_user_item_claims_dispatch_and_is_suppressed_once(
    tmp_path: Path,
) -> None:
    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="outbound")
        _prepare(storage)
        storage.connection.execute(
            "INSERT INTO dispatch_attempts(dispatch_attempt_id,state,updated_at) "
            "VALUES('attempt','outcome_unknown',datetime('now'))"
        )
        storage.connection.execute(
            "INSERT INTO dispatch_records(dispatch_attempt_id,ingress_message_id,thread_id,"
            "client_user_message_id,profile_hash,binding_epoch,identity_binding_epoch,fencing_token,"
            "server_epoch,connection_epoch,request_hash,request_id,submitted_text_hash,"
            "has_attachments,state,created_at,updated_at) "
            "VALUES('attempt','ingress','thread','client','desktop-host-managed',1,1,1,"
            "'server','connection','request','desktop-ui-submitted',?,0,'outcome_unknown',"
            "datetime('now'),datetime('now'))",
            (desktop_submission_text_hash("来自飞书"),),
        )
        batch = RolloutBatch(
            (
                _event(
                    EventKind.COMMENTARY,
                    "late-feishu-item",
                    '\n<in-app-browser-context source="ambient-ui-state">\n'
                    "Codex supplied browser state.\n"
                    "</in-app-browser-context>\n\n"
                    "## My request:\n"
                    "来自飞书\n",
                    source_type="user_message",
                ),
            ),
            SourceCursor("source", "file", 100, "hash", "1"),
        )

        result = OutboundPipeline(storage).ingest_rollout_batch(batch)

        assert result.inserted_items == 1 and result.queued_messages == 0
        record = storage.connection.execute(
            "SELECT state,turn_id,user_item_id FROM dispatch_records"
        ).fetchone()
        assert tuple(record) == ("accepted", "turn", "late-feishu-item")
        assert storage.connection.execute(
            "SELECT state FROM dispatch_attempts"
        ).fetchone()[0] == "accepted"
        assert storage.connection.execute(
            "SELECT COUNT(*) FROM executed_command_tombstones"
        ).fetchone()[0] == 1

        second = RolloutBatch(
            (
                NormalizedEvent(
                    "thread",
                    "turn-2",
                    "codex-identical-item",
                    EventKind.COMMENTARY,
                    0,
                    "来自飞书",
                    "user_message",
                ),
            ),
            SourceCursor("source", "file", 200, "hash-2", "1"),
        )
        repeated_text = OutboundPipeline(storage).ingest_rollout_batch(second)
        assert repeated_text.inserted_items == 1 and repeated_text.queued_messages == 1
        assert storage.connection.execute(
            "SELECT item_id FROM provider_outbox"
        ).fetchone()[0] == "codex-identical-item"


def test_runtime_outbox_completion_is_compare_and_swap(tmp_path: Path) -> None:
    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="outbound")
        _prepare(storage)
        outbox_id = storage.enqueue_provider_message(
            logical_message_id="x",
            operation="control",
            endpoint_name="send_message",
            stable_uuid="u",
            marker="m",
            body_json="{}",
            body_hash="h",
            priority=1,
        )
        row = storage.lease_outbox("one", "2999-01-01T00:00:00Z")
        with pytest.raises(Exception, match="compare-and-swap"):
            storage.finish_outbox(outbox_id, "two", state="confirmed")


def test_outbox_places_uuid_in_json_body_for_reply(tmp_path: Path) -> None:
    class RecordingClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        def call(self, endpoint: str, **kwargs):
            self.calls.append((endpoint, kwargs))
            return ProviderResult(ProviderOutcome.CONFIRMED, "0", message_id="reply")

    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="outbound")
        _prepare(storage)
        storage.enqueue_provider_message(
            logical_message_id="reply:test",
            thread_id="thread",
            operation="commentary",
            endpoint_name="reply_message",
            target_message_id="anchor",
            stable_uuid="stable-uuid",
            marker="marker",
            body_json='{"text":"hello"}',
            body_hash="hash",
            priority=1,
        )
        client = RecordingClient()
        assert OutboxWorker(storage, client, "worker").run_once()
        endpoint, kwargs = client.calls[0]
        assert endpoint == "reply_message"
        assert kwargs.get("query") is None
        assert kwargs["json_body"]["uuid"] == "stable-uuid"
        assert kwargs["json_body"]["reply_in_thread"] is False


def test_outbox_rejects_unknown_reply_ancestry_without_provider_call(tmp_path: Path) -> None:
    class RecordingClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        def call(self, endpoint: str, **kwargs):
            self.calls.append((endpoint, kwargs))
            return ProviderResult(ProviderOutcome.CONFIRMED, "0", message_id="reply")

    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="outbound")
        _prepare(storage)
        storage.enqueue_provider_message(
            logical_message_id="reply:unknown-parent",
            thread_id="thread",
            operation="commentary",
            endpoint_name="reply_message",
            target_message_id="unknown-parent",
            stable_uuid="stable-uuid",
            marker="marker",
            body_json='{"text":"hello"}',
            body_hash="hash",
            priority=1,
        )
        client = RecordingClient()
        assert OutboxWorker(storage, client, "worker").run_once()
        assert client.calls == []
        outbox = storage.connection.execute(
            "SELECT state,last_error_code FROM provider_outbox"
        ).fetchone()
        assert tuple(outbox) == ("permanent", "reply_ancestry_conflict")
        assert storage.connection.execute(
            "SELECT COUNT(*) FROM dead_letters WHERE category='reply_ancestry'"
        ).fetchone()[0] == 1


def test_topic_group_outbox_replies_inside_task_thread(tmp_path: Path) -> None:
    class RecordingClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        def call(self, endpoint: str, **kwargs):
            self.calls.append((endpoint, kwargs))
            return ProviderResult(
                ProviderOutcome.CONFIRMED,
                "0",
                message_id="reply",
                thread_id="provider-thread",
            )

    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="outbound")
        _prepare(storage, conversation_mode="topic_group")
        storage.enqueue_provider_message(
            logical_message_id="reply:topic",
            thread_id="thread",
            operation="final",
            endpoint_name="reply_message",
            target_message_id="anchor",
            reply_in_thread=True,
            stable_uuid="stable-topic",
            marker="marker",
            body_json='{"text":"hello"}',
            body_hash="hash",
            priority=100,
        )
        client = RecordingClient()
        assert OutboxWorker(storage, client, "worker").run_once()
        assert client.calls[0][1]["json_body"]["reply_in_thread"] is True
        ancestry = storage.connection.execute(
            "SELECT provider_thread_id FROM message_ancestry WHERE message_id='reply'"
        ).fetchone()
        assert ancestry["provider_thread_id"] == "provider-thread"


def test_provider_uuids_are_scoped_across_parallel_delivery_surfaces(tmp_path: Path) -> None:
    batch = RolloutBatch(
        (_event(EventKind.COMMENTARY, "same-item", "same output"),),
        SourceCursor("source", "file", 100, "hash", "1"),
    )
    uuids: list[str] = []
    for name, mode, chat_id in (
        ("p2p.db", "p2p", "p2p-chat"),
        ("topic.db", "topic_group", "topic-chat"),
    ):
        with RuntimeStorage(tmp_path / name) as storage:
            storage.initialize_runtime(sink_mode="outbound")
            _prepare(storage, conversation_mode=mode, chat_id=chat_id)
            OutboundPipeline(storage).ingest_rollout_batch(batch)
            uuids.append(
                storage.connection.execute(
                    "SELECT stable_uuid FROM provider_outbox"
                ).fetchone()[0]
            )
    assert uuids[0] != uuids[1]


def test_task_anchor_uuid_is_scoped_to_target_chat_and_mode(tmp_path: Path) -> None:
    uuids: list[str] = []
    project = tmp_path / "project"
    project.mkdir()
    for name, mode, chat_id in (
        ("p2p.db", "p2p", "p2p-chat"),
        ("topic.db", "topic_group", "topic-chat"),
    ):
        with RuntimeStorage(tmp_path / name) as storage:
            storage.initialize_runtime(sink_mode="outbound")
            TaskAnchorManager(storage).opt_in(
                thread_id="same-thread",
                project_root=project,
                chat_id=chat_id,
                conversation_mode=mode,
            )
            uuids.append(
                storage.connection.execute(
                    "SELECT stable_uuid FROM provider_outbox WHERE operation='anchor'"
                ).fetchone()[0]
            )
    assert uuids[0] != uuids[1]


def test_topic_anchor_uses_codex_task_title(tmp_path: Path) -> None:
    project = tmp_path / "project-directory"
    project.mkdir()
    with RuntimeStorage(tmp_path / "topic.db") as storage:
        storage.initialize_runtime(sink_mode="outbound")
        TaskAnchorManager(storage).opt_in(
            thread_id="019ff3cb-thread",
            project_root=project,
            chat_id="topic-chat",
            conversation_mode="topic_group",
            task_title="主力开发",
            project_name="CODEX飞书接口",
        )
        body = json.loads(
            storage.connection.execute(
                "SELECT body_json FROM provider_outbox WHERE operation='anchor'"
            ).fetchone()[0]
        )
        assert body["text"] == "主力开发|CODEX飞书接口"
        assert "task:" not in body["text"]
        assert not any(unicodedata.category(character) == "Cf" for character in body["text"])


def test_existing_topic_title_is_updated_durably_and_only_once(tmp_path: Path) -> None:
    class RecordingClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        def call(self, endpoint: str, **kwargs):
            self.calls.append((endpoint, kwargs))
            return ProviderResult(ProviderOutcome.CONFIRMED, "0")

    project = tmp_path / "project"
    project.mkdir()
    with RuntimeStorage(tmp_path / "topic.db") as storage:
        storage.initialize_runtime(sink_mode="outbound")
        manager = TaskAnchorManager(storage)
        manager.opt_in(
            thread_id="thread",
            project_root=project,
            chat_id="topic-chat",
            conversation_mode="topic_group",
            task_title="旧名称",
            project_name="CODEX飞书接口",
        )
        storage.connection.execute(
            "UPDATE provider_outbox SET state='confirmed',provider_message_id='anchor' "
            "WHERE operation='anchor'"
        )
        storage.connection.execute(
            "UPDATE task_bindings SET anchor_message_id='anchor',anchor_state='confirmed',"
            "anchor_title_hash=pending_title_hash,pending_title_hash=NULL,anchor_marker='task:legacy' "
            "WHERE thread_id='thread'"
        )

        assert manager.sync_title("thread", " 主力\n开发 ", "CODEX飞书接口")
        assert not manager.sync_title("thread", "another name", "CODEX飞书接口")
        row = storage.connection.execute(
            "SELECT operation,endpoint_name,target_message_id,item_id,body_json,state "
            "FROM provider_outbox WHERE operation='anchor_title'"
        ).fetchone()
        assert (row["endpoint_name"], row["target_message_id"], row["state"]) == (
            "update_message",
            "anchor",
            "pending",
        )
        body_text = json.loads(row["body_json"])["text"]
        assert body_text == "主力 开发|CODEX飞书接口"
        assert "task:" not in body_text
        assert not any(unicodedata.category(character) == "Cf" for character in body_text)

        client = RecordingClient()
        assert OutboxWorker(storage, client, "worker").run_once()
        endpoint, kwargs = client.calls[0]
        assert endpoint == "update_message"
        assert kwargs["path_parameters"] == {"message_id": "anchor"}
        assert set(kwargs["json_body"]) == {"content", "msg_type"}
        binding = storage.connection.execute(
            "SELECT task_title,project_name,anchor_marker,anchor_title_hash,pending_title_hash,"
            "title_revision "
            "FROM task_bindings WHERE thread_id='thread'"
        ).fetchone()
        assert binding["task_title"] == "主力 开发"
        assert binding["project_name"] == "CODEX飞书接口"
        assert all(unicodedata.category(character) == "Cf" for character in binding["anchor_marker"])
        assert binding["anchor_title_hash"] == row["item_id"]
        assert binding["pending_title_hash"] is None
        assert binding["title_revision"] == 2
        assert not manager.sync_title("thread", "主力 开发", "CODEX飞书接口")


def test_permanent_title_failure_is_persisted_without_retry_loop(tmp_path: Path) -> None:
    class ExpiredEditWindowClient:
        @staticmethod
        def call(endpoint: str, **kwargs):
            assert endpoint == "update_message"
            return ProviderResult(ProviderOutcome.PERMANENT, "230075")

    project = tmp_path / "project"
    project.mkdir()
    with RuntimeStorage(tmp_path / "topic.db") as storage:
        storage.initialize_runtime(sink_mode="outbound")
        manager = TaskAnchorManager(storage)
        manager.opt_in(
            thread_id="thread",
            project_root=project,
            chat_id="topic-chat",
            conversation_mode="topic_group",
            task_title="旧名称",
            project_name="CODEX飞书接口",
        )
        storage.connection.execute(
            "UPDATE provider_outbox SET state='confirmed' WHERE operation='anchor'"
        )
        storage.connection.execute(
            "UPDATE task_bindings SET anchor_message_id='anchor',anchor_state='confirmed',"
            "anchor_title_hash=pending_title_hash,pending_title_hash=NULL WHERE thread_id='thread'"
        )

        assert manager.sync_title("thread", "新名称", "CODEX飞书接口")
        failed_hash = storage.connection.execute(
            "SELECT item_id FROM provider_outbox WHERE operation='anchor_title'"
        ).fetchone()[0]
        assert OutboxWorker(storage, ExpiredEditWindowClient(), "worker").run_once()
        binding = storage.connection.execute(
            "SELECT pending_title_hash,blocked_title_hash,title_sync_error "
            "FROM task_bindings WHERE thread_id='thread'"
        ).fetchone()
        assert tuple(binding) == (None, failed_hash, "230075")
        assert not manager.sync_title("thread", "新名称", "CODEX飞书接口")
        assert storage.connection.execute(
            "SELECT COUNT(*) FROM provider_outbox WHERE operation='anchor_title'"
        ).fetchone()[0] == 1

        assert manager.sync_title("thread", "另一个名称", "CODEX飞书接口")
        binding = storage.connection.execute(
            "SELECT blocked_title_hash,title_sync_error,pending_title_hash "
            "FROM task_bindings WHERE thread_id='thread'"
        ).fetchone()
        assert binding["blocked_title_hash"] is None
        assert binding["title_sync_error"] is None
        assert binding["pending_title_hash"] is not None


def test_archive_deactivates_same_topic_and_reactivation_restores_renamed_title(
    tmp_path: Path,
) -> None:
    class RecordingClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        def call(self, endpoint: str, **kwargs):
            self.calls.append((endpoint, kwargs))
            return ProviderResult(ProviderOutcome.CONFIRMED, "0")

    project = tmp_path / "project"
    project.mkdir()
    with RuntimeStorage(tmp_path / "topic.db") as storage:
        storage.initialize_runtime(sink_mode="outbound")
        manager = TaskAnchorManager(storage)
        manager.opt_in(
            thread_id="thread",
            project_root=project,
            chat_id="topic-chat",
            conversation_mode="topic_group",
            task_title="主力开发",
            project_name="CODEX飞书接口",
        )
        storage.connection.execute(
            "UPDATE provider_outbox SET state='confirmed',provider_message_id='anchor' "
            "WHERE operation='anchor'"
        )
        storage.connection.execute(
            "UPDATE task_bindings SET anchor_message_id='anchor',anchor_state='confirmed',"
            "anchor_title_hash=pending_title_hash,pending_title_hash=NULL WHERE thread_id='thread'"
        )
        storage.connection.execute(
            "INSERT INTO remote_task_grants(thread_id,project_root,chat_id,task_binding_epoch,"
            "identity_binding_epoch,service_fencing_token,capabilities_json,capabilities_hash,state,"
            "authorized_at,updated_at) VALUES(?,?,?,?,?,?,?,?,'active',?,?)",
            (
                "thread",
                str(project.resolve()),
                "topic-chat",
                0,
                0,
                1,
                "{}",
                "hash",
                utc_now(),
                utc_now(),
            ),
        )

        assert manager.archive("thread")
        binding = storage.connection.execute(
            "SELECT opted_in,lifecycle_state FROM task_bindings WHERE thread_id='thread'"
        ).fetchone()
        assert tuple(binding) == (0, "archived")
        assert storage.connection.execute(
            "SELECT state FROM remote_task_grants WHERE thread_id='thread'"
        ).fetchone()[0] == "revoked"
        archived_update = storage.connection.execute(
            "SELECT target_message_id,body_json,state FROM provider_outbox "
            "WHERE operation='anchor_title'"
        ).fetchone()
        assert archived_update["target_message_id"] == "anchor"
        assert json.loads(archived_update["body_json"])["text"] == (
            "【已归档】主力开发|CODEX飞书接口"
        )
        assert not manager.archive("thread")

        client = RecordingClient()
        assert OutboxWorker(storage, client, "worker").run_once()
        assert client.calls[0][0] == "update_message"
        assert not manager.archive("thread")

        assert manager.reactivate("thread")
        assert manager.sync_title("thread", "主力开发新版", "CODEX飞书接口")
        restored = storage.connection.execute(
            "SELECT target_message_id,body_json FROM provider_outbox "
            "WHERE operation='anchor_title' AND state='pending'"
        ).fetchone()
        assert restored["target_message_id"] == "anchor"
        assert json.loads(restored["body_json"])["text"] == (
            "主力开发新版|CODEX飞书接口"
        )
        binding = storage.connection.execute(
            "SELECT opted_in,lifecycle_state FROM task_bindings WHERE thread_id='thread'"
        ).fetchone()
        assert tuple(binding) == (1, "active")


def test_runtime_v2_database_is_upgraded_without_rebinding(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE task_bindings(thread_id TEXT PRIMARY KEY);
        CREATE TABLE identity_bindings(
            binding_key TEXT PRIMARY KEY,
            p2p_chat_id TEXT NOT NULL
        );
        CREATE TABLE message_ancestry(message_id TEXT PRIMARY KEY);
        CREATE TABLE ingress_messages(message_id TEXT PRIMARY KEY);
        """
    )
    connection.execute(
        "INSERT INTO identity_bindings(binding_key,p2p_chat_id) VALUES('owner','legacy-chat')"
    )
    connection.commit()
    connection.close()
    with RuntimeStorage(path) as storage:
        storage.initialize_runtime(sink_mode="outbound")
        row = storage.connection.execute(
            "SELECT active_chat_id,conversation_mode FROM identity_bindings WHERE binding_key='owner'"
        ).fetchone()
        assert (row["active_chat_id"], row["conversation_mode"]) == ("legacy-chat", "p2p")
        assert storage.connection.execute(
            "SELECT value FROM runtime_metadata WHERE key='runtime_schema_version'"
        ).fetchone()[0] == "15"
        columns = {
            row[1]
            for row in storage.connection.execute("PRAGMA table_info(task_bindings)").fetchall()
        }
        assert {
            "task_title",
            "project_name",
            "anchor_title_hash",
            "pending_title_hash",
            "blocked_title_hash",
            "title_sync_error",
            "title_revision",
            "lifecycle_state",
        } <= columns
        dispatch_columns = {
            row[1]
            for row in storage.connection.execute(
                "PRAGMA table_info(dispatch_records)"
            ).fetchall()
        }
        assert "user_item_id" in dispatch_columns


def test_schema_migration_repairs_stuck_permanent_title_projection(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    with RuntimeStorage(path) as storage:
        storage.initialize_runtime(sink_mode="outbound")
        _prepare(storage)
        storage.connection.execute(
            "UPDATE task_bindings SET pending_title_hash='failed-title' WHERE thread_id='thread'"
        )
        storage.enqueue_provider_message(
            logical_message_id="failed-title-update",
            thread_id="thread",
            item_id="failed-title",
            operation="anchor_title",
            endpoint_name="update_message",
            target_message_id="anchor",
            stable_uuid="failed-title-uuid",
            marker="marker",
            body_json='{"text":"failed"}',
            body_hash="hash",
            priority=95,
        )
        storage.connection.execute(
            "UPDATE provider_outbox SET state='permanent',last_error_code='230075' "
            "WHERE logical_message_id='failed-title-update'"
        )

    with RuntimeStorage(path) as storage:
        storage.initialize_runtime(sink_mode="outbound")
        row = storage.connection.execute(
            "SELECT pending_title_hash,blocked_title_hash,title_sync_error "
            "FROM task_bindings WHERE thread_id='thread'"
        ).fetchone()
        assert tuple(row) == (None, "failed-title", "230075")


def test_legacy_global_approval_request_id_is_migrated_to_connection_scope(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-approval.db"
    with RuntimeStorage(path) as storage:
        storage.initialize()
        storage.execute_schema_migration(
            """
            CREATE TABLE approval_actions (
                token_hash TEXT PRIMARY KEY,
                approval_id TEXT UNIQUE NOT NULL,
                server_request_id TEXT UNIQUE NOT NULL,
                server_method TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                turn_id TEXT,
                tenant_key TEXT NOT NULL,
                app_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                card_message_id TEXT NOT NULL,
                operator_open_id TEXT NOT NULL,
                binding_epoch INTEGER NOT NULL,
                identity_binding_epoch INTEGER NOT NULL,
                kill_generation INTEGER NOT NULL,
                server_epoch TEXT NOT NULL,
                connection_epoch TEXT NOT NULL,
                session_id TEXT NOT NULL,
                service_fencing_token INTEGER NOT NULL,
                decision_map_json TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                consumed_decision TEXT,
                consumed_at TEXT
            );
            INSERT INTO approval_actions VALUES(
                'token','approval','0','method','thread',NULL,'tenant','app','chat','card',
                'owner',1,1,1,'server-1','connection-1','session-1',1,'{}',
                '2999-01-01T00:00:00Z',NULL,NULL
            );
            """
        )
        storage.initialize_runtime(sink_mode="control")
        storage.connection.execute(
            "INSERT INTO approval_actions VALUES("
            "'token-2','approval-2','0','method','thread',NULL,'tenant','app','chat','card',"
            "'owner',1,1,1,'server-2','connection-2','session-2',1,'{}',"
            "'2999-01-01T00:00:00Z',NULL,NULL)"
        )
        assert storage.connection.execute(
            "SELECT COUNT(*) FROM approval_actions WHERE server_request_id='0'"
        ).fetchone()[0] == 2


def test_local_markdown_image_is_uploaded_then_replied_in_task_topic(tmp_path: Path) -> None:
    class RecordingClient:
        def __init__(self) -> None:
            self.uploads: list[dict] = []
            self.calls: list[tuple[str, dict]] = []

        def upload_image(self, **kwargs):
            self.uploads.append(kwargs)
            return ProviderResult(ProviderOutcome.CONFIRMED, "0", image_key="img-fixture")

        def call(self, endpoint: str, **kwargs):
            self.calls.append((endpoint, kwargs))
            return ProviderResult(
                ProviderOutcome.CONFIRMED,
                "0",
                message_id=f"message-{len(self.calls)}",
                thread_id="provider-thread",
            )

    project = tmp_path / "project"
    project.mkdir()
    image = project / "plot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="outbound")
        storage.connection.execute(
            "INSERT INTO task_bindings(thread_id,project_root,chat_id,anchor_message_id,anchor_state,"
            "anchor_uuid,anchor_marker,conversation_mode,opted_in,updated_at) "
            "VALUES('thread',?,'chat','anchor','confirmed','uuid','marker','topic_group',1,?)",
            (str(project), utc_now()),
        )
        batch = RolloutBatch(
            (_event(EventKind.FINAL_ANSWER, "image-item", f"结果\n![曲线](<{image}>)"),),
            SourceCursor("source", "file", 100, "hash", "1"),
        )
        result = OutboundPipeline(storage).ingest_rollout_batch(batch)
        assert result.queued_messages == 3
        rows = storage.connection.execute(
            "SELECT outbox_id,operation,message_type,body_json,state FROM provider_outbox "
            "ORDER BY outbox_id"
        ).fetchall()
        assert [(row["operation"], row["message_type"]) for row in rows] == [
            ("commentary", "text"),
            ("commentary", "image"),
            ("final", "text"),
        ]
        assert str(image) not in rows[0]["body_json"]
        assert "图片：曲线" in rows[0]["body_json"]
        assert json.loads(rows[-1]["body_json"])["text"] == "🔔【等待你的回应】"
        stored = storage.connection.execute(
            "SELECT file_name,mime_type,content,content_hash,image_key FROM outbound_images"
        ).fetchone()
        assert stored["file_name"] == "plot.png" and stored["mime_type"] == "image/png"
        assert bytes(stored["content"]) == image.read_bytes() and stored["image_key"] is None

        client = RecordingClient()
        worker = OutboxWorker(storage, client, "worker")
        assert worker.run_once()  # visible text
        assert worker.run_once()  # durable upload phase
        assert client.uploads[0]["content"] == image.read_bytes()
        image_row = storage.connection.execute(
            "SELECT state,body_json FROM provider_outbox WHERE message_type='image'"
        ).fetchone()
        assert image_row["state"] == "retryable"
        assert json.loads(image_row["body_json"]) == {"image_key": "img-fixture"}
        assert worker.run_once()  # image reply using the persisted image_key
        assert client.calls[-1][1]["json_body"]["msg_type"] == "image"
        assert json.loads(client.calls[-1][1]["json_body"]["content"]) == {
            "image_key": "img-fixture"
        }
        assert client.calls[-1][1]["json_body"]["reply_in_thread"] is True
        assert worker.run_once()  # waiting-for-reply cue
        assert json.loads(client.calls[-1][1]["json_body"]["content"])["text"] == (
            "🔔【等待你的回应】"
        )
        assert storage.connection.execute(
            "SELECT COUNT(*) FROM transient_messages WHERE message_id IN "
            "('message-1','message-2','message-3')"
        ).fetchone()[0] == 0


def test_local_image_can_be_recovered_idempotently_after_content_is_corrected(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    image = project / "camera-export.png"
    image.write_bytes(b"not-an-image")
    event = _event(
        EventKind.FINAL_ANSWER,
        "recover-image",
        f"结果\n![照片](<{image}>)",
    )
    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="outbound")
        storage.connection.execute(
            "INSERT INTO task_bindings(thread_id,project_root,chat_id,anchor_message_id,anchor_state,"
            "anchor_uuid,anchor_marker,conversation_mode,opted_in,updated_at) "
            "VALUES('thread',?,'chat','anchor','confirmed','uuid','marker','topic_group',1,?)",
            (str(project), utc_now()),
        )
        batch = RolloutBatch(
            (event,),
            SourceCursor("source", "file", 100, "hash", "1"),
        )
        first = OutboundPipeline(storage).ingest_rollout_batch(batch)
        assert first.queued_messages == 2
        assert storage.connection.execute("SELECT COUNT(*) FROM dead_letters").fetchone()[0] == 1
        assert storage.connection.execute("SELECT COUNT(*) FROM outbound_images").fetchone()[0] == 0

        image.write_bytes(b"\xff\xd8\xff\xe0fixture")
        pipeline = OutboundPipeline(storage)
        with storage.immediate() as connection:
            recovered = pipeline._enqueue_event(connection, event)

        assert recovered == 1
        assert storage.connection.execute("SELECT COUNT(*) FROM provider_outbox").fetchone()[0] == 3
        stored = storage.connection.execute(
            "SELECT file_name,mime_type,content FROM outbound_images"
        ).fetchone()
        assert tuple(stored[:2]) == ("camera-export.jpg", "image/jpeg")
        assert bytes(stored["content"]) == image.read_bytes()


def test_final_markdown_image_is_resent_after_matching_commentary_image(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    content = b"\x89PNG\r\n\x1a\nvisible-output"
    image_path = project / "same.png"
    image_path.write_bytes(content)
    digest = sha256(content).hexdigest()
    embedded = EmbeddedImage("image/png", ".png", content, digest)

    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="outbound")
        storage.connection.execute(
            "INSERT INTO task_bindings(thread_id,project_root,chat_id,anchor_message_id,anchor_state,"
            "anchor_uuid,anchor_marker,conversation_mode,opted_in,updated_at) "
            "VALUES('thread',?,'chat','anchor','confirmed','uuid','marker','topic_group',1,?)",
            (str(project), utc_now()),
        )
        tool_event = NormalizedEvent(
            "thread",
            "turn",
            "tool-image",
            EventKind.COMMENTARY,
            0,
            "",
            "visible_tool_output",
            (embedded,),
        )
        final_event = NormalizedEvent(
            "thread",
            "turn",
            "final-with-same-image",
            EventKind.FINAL_ANSWER,
            0,
            f"完成\n![结果](<{image_path}>)",
            "response_item",
        )
        result = OutboundPipeline(storage).ingest_rollout_batch(
            RolloutBatch(
                (tool_event, final_event),
                SourceCursor("source", "file", 100, "hash", "1"),
            )
        )
        assert result.inserted_items == 2 and result.queued_messages == 4
        rows = storage.connection.execute(
            "SELECT operation,message_type,body_json FROM provider_outbox ORDER BY outbox_id"
        ).fetchall()
        assert [(row["operation"], row["message_type"]) for row in rows] == [
            ("commentary", "image"),
            ("commentary", "text"),
            ("commentary", "image"),
            ("final", "text"),
        ]
        assert "图片：结果" in rows[1]["body_json"]
        assert json.loads(rows[-1]["body_json"])["text"] == "🔔【等待你的回应】"
        stored = storage.connection.execute(
            "SELECT source_path,file_name,mime_type,content,content_hash FROM outbound_images"
        ).fetchall()
        assert len(stored) == 2
        assert stored[0]["source_path"].startswith("rollout://thread/turn/tool-image/")
        assert stored[0]["file_name"].endswith(".png")
        assert bytes(stored[0]["content"]) == content
        assert stored[1]["source_path"].endswith("same.png")
        assert stored[1]["file_name"] == "same.png"
        assert bytes(stored[1]["content"]) == content


def test_unknown_image_upload_is_retried_without_sending_a_visible_message(
    tmp_path: Path,
) -> None:
    class UnknownUploadClient:
        def __init__(self) -> None:
            self.uploads = 0
            self.calls = 0

        def upload_image(self, **kwargs):
            self.uploads += 1
            return ProviderResult(ProviderOutcome.UNKNOWN, "transport_unknown")

        def call(self, endpoint: str, **kwargs):
            self.calls += 1
            assert kwargs["json_body"]["msg_type"] == "text"
            return ProviderResult(
                ProviderOutcome.CONFIRMED,
                "0",
                message_id="image-label",
                thread_id="provider-thread",
            )

    project = tmp_path / "project"
    project.mkdir()
    image = project / "plot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="outbound")
        storage.connection.execute(
            "INSERT INTO task_bindings(thread_id,project_root,chat_id,anchor_message_id,anchor_state,"
            "anchor_uuid,anchor_marker,conversation_mode,opted_in,updated_at) "
            "VALUES('thread',?,'chat','anchor','confirmed','uuid','marker','topic_group',1,?)",
            (str(project), utc_now()),
        )
        batch = RolloutBatch(
            (_event(EventKind.FINAL_ANSWER, "image-only", f"![曲线](<{image}>)"),),
            SourceCursor("source", "file", 100, "hash", "1"),
        )
        OutboundPipeline(storage).ingest_rollout_batch(batch)
        client = UnknownUploadClient()
        worker = OutboxWorker(storage, client, "worker")
        assert worker.run_once()  # local-path-free image label
        assert worker.run_once()  # upload attempt; no image message is sent
        row = storage.connection.execute(
            "SELECT state,last_error_code,provider_message_id FROM provider_outbox "
            "WHERE message_type='image'"
        ).fetchone()
        assert tuple(row) == ("retryable", "image_upload:transport_unknown", None)
        image_row = storage.connection.execute(
            "SELECT image_key,upload_attempt_count FROM outbound_images"
        ).fetchone()
        assert tuple(image_row) == (None, 1)
        assert client.uploads == 1 and client.calls == 1
