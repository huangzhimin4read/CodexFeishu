from pathlib import Path
from types import SimpleNamespace

from codex_feishu_bridge.codex.rollout_observer import IncrementalRolloutReader
from codex_feishu_bridge.codex.state_discovery import RolloutSource
from codex_feishu_bridge.codex.controller import DispatchResult
from codex_feishu_bridge.runtime_storage import RuntimeStorage
from codex_feishu_bridge.service import (
    BridgeService,
    _turn_failure_ack,
    checkpoint_unseen_source,
)
from codex_feishu_bridge.runtime_config import RuntimeMode
from codex_feishu_bridge.runtime_config import ConversationMode, FeishuBinding


SESSION = b'{"type":"session_meta","payload":{"rollout_version":"1","id":"t"}}\n'
COMMENTARY = (
    b'{"type":"response_item","payload":{"type":"message","role":"assistant",'
    b'"phase":"commentary","thread_id":"t","turn_id":"u","item_id":"old",'
    b'"content":[{"type":"output_text","text":"before activation"}]}}\n'
)
FINAL = (
    b'{"type":"response_item","payload":{"type":"message","role":"assistant",'
    b'"phase":"final_answer","thread_id":"t","turn_id":"u","item_id":"new",'
    b'"content":[{"type":"output_text","text":"after activation"}]}}\n'
)


def test_activation_checkpoint_does_not_replay_existing_visible_records(tmp_path: Path) -> None:
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_bytes(SESSION + COMMENTARY)
    source = RolloutSource(
        path=rollout,
        thread_id="t",
        project_root=tmp_path,
        rollout_version="1",
        modified_ns=rollout.stat().st_mtime_ns,
    )
    reader = IncrementalRolloutReader()

    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="outbound")
        assert checkpoint_unseen_source(storage, reader, source)
        assert storage.connection.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
        cursor = storage.cursor_for(str(rollout.resolve()))
        assert cursor is not None
        assert cursor.committed_offset == len(SESSION + COMMENTARY)
        assert not checkpoint_unseen_source(storage, reader, source)

        with rollout.open("ab") as handle:
            handle.write(FINAL)
        batch = reader.read(rollout, cursor, expected_thread_id="t")
        assert [event.text for event in batch.events] == ["after activation"]


def test_database_fence_is_reclaimed_after_mutex_proves_old_instance_is_gone(
    tmp_path: Path,
) -> None:
    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="outbound")
        storage.connection.execute(
            "UPDATE service_state SET instance_id='dead',process_state='running',fencing_token=4 "
            "WHERE singleton=1"
        )
        service = object.__new__(BridgeService)
        service.storage = storage
        service.instance_id = "replacement"
        assert service._acquire_service_fence() == 5
        row = storage.connection.execute(
            "SELECT instance_id,process_state,fencing_token,kill_generation FROM service_state"
        ).fetchone()
        assert tuple(row) == ("replacement", "starting", 5, 1)
        recovery = storage.connection.execute(
            "SELECT value FROM runtime_metadata WHERE key='last_stale_fence_recovery'"
        ).fetchone()
        assert '"previous_instance_id":"dead"' in recovery[0]


def test_service_passes_codex_task_title_to_new_topic_binding(tmp_path: Path) -> None:
    class Config:
        project_name = "CODEX飞书接口"
        feishu = SimpleNamespace(
            target_chat_id="topic-chat",
            conversation_mode=SimpleNamespace(value="topic_group"),
        )

        @staticmethod
        def allows(mode: RuntimeMode) -> bool:
            return False

    rollout = tmp_path / "rollout.jsonl"
    rollout.write_bytes(SESSION)
    source = RolloutSource(
        path=rollout,
        thread_id="thread",
        project_root=tmp_path,
        rollout_version="1",
        modified_ns=rollout.stat().st_mtime_ns,
    )

    class ReadyTitleReader:
        @staticmethod
        def title_for(thread_id: str, project_root: Path) -> str:
            assert thread_id == "thread" and project_root == tmp_path
            return "主力开发"

        @staticmethod
        def completed_attempt(thread_id: str) -> bool:
            return True

    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="outbound")
        service = object.__new__(BridgeService)
        service.config = Config()
        service.storage = storage
        service.title_reader = ReadyTitleReader()
        service._ensure_task_bindings((source,))
        row = storage.connection.execute(
            "SELECT task_title,project_name,pending_title_hash,title_revision FROM task_bindings "
            "WHERE thread_id='thread'"
        ).fetchone()
        assert row["task_title"] == "主力开发"
        assert row["project_name"] == "CODEX飞书接口"
        assert row["pending_title_hash"] and row["title_revision"] == 1


def test_active_turn_barrier_blocks_desktop_and_current_worker_but_not_orphan(
    tmp_path: Path,
) -> None:
    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="control")
        storage.connection.execute(
            "UPDATE service_state SET fencing_token=5,process_state='running' WHERE singleton=1"
        )
        service = object.__new__(BridgeService)
        service.storage = storage
        service._active_rollout_turns = {"thread": frozenset({"desktop"})}
        assert service._has_blocking_active_turn("thread")
        assert service._active_turn_for_steer("thread") == "desktop"

        values = (
            "attempt-old",
            "ingress-old",
            "thread",
            "client-old",
            "profile",
            1,
            1,
            4,
            "server",
            "connection",
            "hash",
            "old-remote",
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
        )
        storage.connection.execute(
            "INSERT INTO dispatch_records(dispatch_attempt_id,ingress_message_id,thread_id,"
            "client_user_message_id,profile_hash,binding_epoch,identity_binding_epoch,"
            "fencing_token,server_epoch,connection_epoch,request_hash,state,turn_id,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,'accepted',?,?,?)",
            values,
        )
        service._active_rollout_turns = {"thread": frozenset({"old-remote"})}
        assert not service._has_blocking_active_turn("thread")

        storage.connection.execute(
            "UPDATE dispatch_records SET fencing_token=5 WHERE turn_id='old-remote'"
        )
        assert service._has_blocking_active_turn("thread")

        service._active_rollout_turns = {
            "thread": frozenset({"old-remote", "current-desktop"})
        }
        assert service._rollout_turn_hint("thread") is None


def test_turn_failure_ack_is_safe_and_actionable() -> None:
    text = _turn_failure_ack({"message": "request timed out", "secret": "do-not-leak"})
    assert "模型连接超时" in text
    assert "重新发送" in text
    assert "do-not-leak" not in text


def test_dispatch_acceptance_queues_truthful_submitted_status(tmp_path: Path) -> None:
    contract = tmp_path / "contract.json"
    contract.write_text("{}", encoding="utf-8")
    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="control")
        storage.connection.execute(
            "INSERT INTO task_bindings(thread_id,project_root,chat_id,anchor_message_id,anchor_state,"
            "anchor_uuid,anchor_marker,conversation_mode,opted_in,updated_at) "
            "VALUES('thread','D:/project','topic-chat','anchor','confirmed','uuid','marker',"
            "'topic_group',1,datetime('now'))"
        )
        service = object.__new__(BridgeService)
        service.storage = storage
        service.config = SimpleNamespace(
            feishu=FeishuBinding(
                "tenant",
                "app",
                "owner",
                "fallback",
                "target",
                contract,
                ConversationMode.TOPIC_GROUP,
                "topic-chat",
            )
        )

        service._queue_dispatch_ack("incoming", "thread")
        service._queue_dispatch_ack("incoming", "thread")

        rows = storage.connection.execute(
            "SELECT logical_message_id,endpoint_name,target_message_id,reply_in_thread,body_json "
            "FROM provider_outbox WHERE logical_message_id='submitted-ack:incoming'"
        ).fetchall()
        assert len(rows) == 1
        assert tuple(rows[0])[:4] == (
            "submitted-ack:incoming",
            "reply_message",
            "incoming",
            1,
        )
        assert "已提交 Codex" in rows[0]["body_json"]


def test_busy_dispatch_queues_not_submitted_status_once(tmp_path: Path) -> None:
    contract = tmp_path / "contract.json"
    contract.write_text("{}", encoding="utf-8")
    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="control")
        storage.connection.execute(
            "INSERT INTO task_bindings(thread_id,project_root,chat_id,anchor_message_id,anchor_state,"
            "anchor_uuid,anchor_marker,conversation_mode,opted_in,updated_at) "
            "VALUES('thread','D:/project','topic-chat','anchor','confirmed','uuid','marker',"
            "'topic_group',1,datetime('now'))"
        )
        service = object.__new__(BridgeService)
        service.storage = storage
        service.config = SimpleNamespace(
            feishu=FeishuBinding(
                "tenant",
                "app",
                "owner",
                "fallback",
                "target",
                contract,
                ConversationMode.TOPIC_GROUP,
                "topic-chat",
            )
        )

        service._queue_pending_ack("incoming", "thread")
        service._queue_pending_ack("incoming", "thread")

        rows = storage.connection.execute(
            "SELECT logical_message_id,endpoint_name,target_message_id,reply_in_thread,body_json "
            "FROM provider_outbox WHERE logical_message_id='pending-ack:incoming'"
        ).fetchall()
        assert len(rows) == 1
        assert tuple(rows[0])[:4] == (
            "pending-ack:incoming",
            "reply_message",
            "incoming",
            1,
        )
        assert "尚未提交 Codex" in rows[0]["body_json"]


def test_desktop_ingress_uses_desktop_writer_and_only_then_queues_submitted(
    tmp_path: Path,
) -> None:
    contract = tmp_path / "contract.json"
    contract.write_text("{}", encoding="utf-8")

    class RecordingDesktopDispatcher:
        calls: list[dict[str, object]] = []

        def dispatch(self, **kwargs):
            self.calls.append(kwargs)
            return DispatchResult("attempt", "desktop-turn", "accepted")

    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="control")
        storage.connection.execute(
            "INSERT INTO task_bindings(thread_id,project_root,chat_id,anchor_message_id,anchor_state,"
            "anchor_uuid,anchor_marker,conversation_mode,opted_in,updated_at) "
            "VALUES('thread','D:/project','topic-chat','anchor','confirmed','uuid','marker',"
            "'topic_group',1,datetime('now'))"
        )
        storage.connection.execute(
            "INSERT INTO ingress_messages(tenant_key,app_id,message_id,chat_id,sender_open_id,"
            "chat_type,message_type,content_hash,raw_hash,received_at,ingest_seq,routing_state,"
            "target_thread_id) VALUES('tenant','app','incoming','topic-chat','owner','group',"
            "'text','content','raw',datetime('now'),1,'routed_current','thread')"
        )
        storage.connection.execute(
            "INSERT INTO ingress_payloads(message_id,text,created_at,expires_at) "
            "VALUES('incoming','来自飞书的正式输入',datetime('now'),datetime('now','+1 day'))"
        )
        service = object.__new__(BridgeService)
        service.storage = storage
        service.config = SimpleNamespace(
            feishu=FeishuBinding(
                "tenant",
                "app",
                "owner",
                "fallback",
                "target",
                contract,
                ConversationMode.TOPIC_GROUP,
                "topic-chat",
            ),
            remote=SimpleNamespace(uses_desktop=True),
        )
        service.ingress = object()
        service.controller = object()
        service.client = object()
        service.desktop_dispatcher = RecordingDesktopDispatcher()
        service._active_rollout_turns = {}

        service._process_ingress()

        assert service.desktop_dispatcher.calls == [
            {
                "ingress_message_id": "incoming",
                "thread_id": "thread",
                "text": "来自飞书的正式输入",
                "required_capability": "text",
                "attachment_paths": (),
            }
        ]
        acknowledgement = storage.connection.execute(
            "SELECT logical_message_id,body_json FROM provider_outbox "
            "WHERE logical_message_id='submitted-ack:incoming'"
        ).fetchone()
        assert acknowledgement is not None
        assert "已提交 Codex" in acknowledgement["body_json"]
        assert service._active_rollout_turns == {
            "thread": frozenset({"desktop-turn"})
        }


def test_unconfirmed_desktop_dispatch_does_not_claim_retry_or_submission(
    tmp_path: Path,
) -> None:
    contract = tmp_path / "contract.json"
    contract.write_text("{}", encoding="utf-8")
    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="control")
        service = object.__new__(BridgeService)
        service.storage = storage
        service.config = SimpleNamespace(
            feishu=FeishuBinding(
                "tenant",
                "app",
                "owner",
                "fallback",
                "target",
                contract,
                ConversationMode.TOPIC_GROUP,
                "topic-chat",
            )
        )

        service._queue_unconfirmed_ack("incoming", "thread")

        row = storage.connection.execute(
            "SELECT logical_message_id,body_json FROM provider_outbox "
            "WHERE logical_message_id='unconfirmed-ack:incoming'"
        ).fetchone()
        assert row is not None
        assert "未能确认已提交 Codex" in row["body_json"]
        assert "重试" not in row["body_json"]


def test_desktop_ui_submission_queues_truthful_submitted_receipt(tmp_path: Path) -> None:
    contract = tmp_path / "contract.json"
    contract.write_text("{}", encoding="utf-8")
    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="control")
        service = object.__new__(BridgeService)
        service.storage = storage
        service.config = SimpleNamespace(
            feishu=FeishuBinding(
                "tenant",
                "app",
                "owner",
                "fallback",
                "target",
                contract,
                ConversationMode.TOPIC_GROUP,
                "topic-chat",
            )
        )

        service._queue_submitted_unconfirmed_ack("incoming", "thread")

        row = storage.connection.execute(
            "SELECT logical_message_id,body_json FROM provider_outbox "
            "WHERE logical_message_id='submitted-ack:incoming'"
        ).fetchone()
        assert row is not None
        assert "已提交 Codex" in row["body_json"]
        assert "未能确认" not in row["body_json"]
