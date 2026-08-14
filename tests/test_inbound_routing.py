import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from codex_feishu_bridge.feishu.inbound import IngressRejected, IngressRouter
from codex_feishu_bridge.runtime_config import (
    ConversationMode,
    FeishuBinding,
    RemoteCapabilities,
)
from codex_feishu_bridge.runtime_storage import RuntimeStorage, utc_now


def binding(tmp_path: Path) -> FeishuBinding:
    contract = tmp_path / "contract.json"
    contract.write_text("{}", encoding="utf-8")
    return FeishuBinding("tenant", "app", "owner", "chat", "target", contract)


def prepare(storage: RuntimeStorage) -> None:
    storage.connection.execute(
        "INSERT INTO identity_bindings(binding_key,tenant_key,app_id,owner_open_id,p2p_chat_id,"
        "binding_epoch,contract_hash,state,updated_at) VALUES('owner','tenant','app','owner','chat',"
        "1,'hash','active',?)",
        (utc_now(),),
    )
    storage.connection.execute(
        "INSERT INTO task_bindings(thread_id,project_root,chat_id,anchor_message_id,anchor_state,"
        "anchor_uuid,anchor_marker,current_binding_epoch,identity_binding_epoch,opted_in,updated_at) "
        "VALUES('thread','D:/project','chat','anchor','confirmed','u','m',1,1,1,?)",
        (utc_now(),),
    )
    storage.connection.execute(
        "INSERT INTO chat_sequences(binding_key,next_ingest_seq,current_task_id,active_binding_epoch,"
        "selection_state,updated_at) VALUES('owner',1,'thread',1,'active',?)",
        (utc_now(),),
    )
    storage.connection.execute(
        "INSERT INTO message_ancestry(message_id,root_id,parent_id,thread_id,chat_id,source,created_at) "
        "VALUES('anchor',NULL,NULL,'thread','chat','anchor',?)",
        (utc_now(),),
    )


def event(message_id: str, text: str, *, root=None, parent=None, chat="chat") -> dict:
    return {
        "header": {"tenant_key": "tenant", "app_id": "app", "event_id": "e-" + message_id},
        "event": {
            "sender": {
                "sender_id": {"open_id": "owner"},
                "sender_type": "user",
                "tenant_key": "tenant",
            },
            "message": {
                "message_id": message_id,
                "chat_id": chat,
                "chat_type": "p2p",
                "message_type": "text",
                "root_id": root,
                "parent_id": parent,
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        },
    }


def test_unquoted_and_known_reply_route_with_frozen_sequence(tmp_path: Path) -> None:
    with RuntimeStorage(tmp_path / "db.sqlite") as storage:
        storage.initialize_runtime(sink_mode="control")
        prepare(storage)
        router = IngressRouter(storage, binding(tmp_path))
        first = router.ingest(event("m1", "do work"))
        assert first.routing_state == "routed_current" and first.target_thread_id == "thread"
        reply = router.ingest(event("m2", "more", root="anchor", parent="anchor"))
        assert reply.routing_state == "routed_reply"
        assert router.ingest(event("m2", "more", root="anchor", parent="anchor")).duplicate


def test_reply_control_is_persisted_as_known_ancestry(tmp_path: Path) -> None:
    with RuntimeStorage(tmp_path / "db.sqlite") as storage:
        storage.initialize_runtime(sink_mode="control")
        prepare(storage)
        decision = IngressRouter(
            storage,
            binding(tmp_path),
            RemoteCapabilities(enabled=True, text=True, controls=True),
        ).ingest(
            event("m-control", "/status", root="anchor", parent="anchor")
        )
        assert decision.routing_state == "control"
        ancestry = storage.connection.execute(
            "SELECT root_id,parent_id,thread_id,chat_id FROM message_ancestry WHERE message_id=?",
            ("m-control",),
        ).fetchone()
        assert tuple(ancestry) == ("anchor", "anchor", "thread", "chat")


def test_known_outbound_user_message_is_not_dispatched_back_to_codex(tmp_path: Path) -> None:
    with RuntimeStorage(tmp_path / "db.sqlite") as storage:
        storage.initialize_runtime(sink_mode="control")
        prepare(storage)
        storage.connection.execute(
            "INSERT OR REPLACE INTO message_ancestry(message_id,root_id,parent_id,thread_id,chat_id,source,created_at) "
            "VALUES('user-reply','anchor','anchor','thread','chat','outbound',?)",
            (utc_now(),),
        )
        decision = IngressRouter(storage, binding(tmp_path)).ingest(
            event("user-reply", "Codex 原生用户输入", root="anchor", parent="anchor")
        )
        assert decision.duplicate
        assert decision.routing_state == "outbound_echo"
        assert storage.connection.execute(
            "SELECT COUNT(*) FROM ingress_messages WHERE message_id='user-reply'"
        ).fetchone()[0] == 0


def test_pending_user_send_delays_callback_for_ancestry_reconciliation(
    tmp_path: Path,
) -> None:
    with RuntimeStorage(tmp_path / "db.sqlite") as storage:
        storage.initialize_runtime(sink_mode="control")
        prepare(storage)
        body = json.dumps(
            {"text": "Codex 原生用户输入"}, ensure_ascii=False, separators=(",", ":")
        )
        outbox_id = storage.enqueue_provider_message(
            logical_message_id="user-message",
            thread_id="thread",
            operation="user_message",
            endpoint_name="reply_message",
            target_message_id="anchor",
            stable_uuid="stable-user-message",
            marker="marker",
            body_json=body,
            body_hash="hash",
            priority=10,
        )
        storage.connection.execute(
            "UPDATE provider_outbox SET state='leased',lease_owner='worker' WHERE outbox_id=?",
            (outbox_id,),
        )
        router = IngressRouter(storage, binding(tmp_path))
        router.ingest(
            event("racing-reply", "Codex 原生用户输入", root="anchor", parent="anchor")
        )
        delayed = storage.connection.execute(
            "SELECT dispatch_not_before FROM ingress_messages WHERE message_id='racing-reply'"
        ).fetchone()
        assert delayed["dispatch_not_before"] is not None

        storage.connection.execute(
            "INSERT OR REPLACE INTO message_ancestry(message_id,root_id,parent_id,thread_id,chat_id,source,created_at) "
            "VALUES('racing-reply','anchor','anchor','thread','chat','outbound',?)",
            (utc_now(),),
        )
        assert router.suppress_if_outbound_echo("racing-reply")
        assert storage.connection.execute(
            "SELECT routing_state FROM ingress_messages WHERE message_id='racing-reply'"
        ).fetchone()[0] == "outbound_echo"

        storage.connection.execute(
            "UPDATE provider_outbox SET state='confirmed',provider_message_id='racing-reply' "
            "WHERE outbox_id=?",
            (outbox_id,),
        )
        router.ingest(
            event("human-same-text", "Codex 原生用户输入", root="anchor", parent="anchor")
        )
        independent = storage.connection.execute(
            "SELECT dispatch_not_before FROM ingress_messages WHERE message_id='human-same-text'"
        ).fetchone()
        assert independent["dispatch_not_before"] is None


def test_conflict_and_indeterminate_reply_fail_closed(tmp_path: Path) -> None:
    with RuntimeStorage(tmp_path / "db.sqlite") as storage:
        storage.initialize_runtime(sink_mode="control")
        prepare(storage)
        router = IngressRouter(storage, binding(tmp_path))
        router.ingest(event("m1", "one"))
        with pytest.raises(IngressRejected, match="conflicting"):
            router.ingest(event("m1", "changed"))
        result = router.ingest(event("m2", "x", root=None, parent="unknown"))
        assert result.routing_state == "routing_indeterminate" and result.target_thread_id is None
        assert storage.connection.execute(
            "SELECT state FROM circuit_breakers WHERE breaker_name='ingress_conflict'"
        ).fetchone()[0] == "open"


def test_selection_pending_blocks_unquoted_input_until_confirmation(tmp_path: Path) -> None:
    with RuntimeStorage(tmp_path / "db.sqlite") as storage:
        storage.initialize_runtime(sink_mode="control")
        prepare(storage)
        router = IngressRouter(storage, binding(tmp_path))
        epoch = router.begin_selection("thread")
        blocked = router.ingest(event("m1", "do not dispatch"))
        assert blocked.routing_state == "routing_indeterminate"
        router.confirm_selection("thread", epoch)
        routed = router.ingest(event("m2", "now dispatch"))
        assert routed.routing_state == "routed_current"


def test_wrong_chat_is_rejected_before_storage(tmp_path: Path) -> None:
    with RuntimeStorage(tmp_path / "db.sqlite") as storage:
        storage.initialize_runtime(sink_mode="control")
        prepare(storage)
        with pytest.raises(IngressRejected, match="not an active"):
            IngressRouter(storage, binding(tmp_path)).ingest(event("m", "x", chat="wrong"))
        assert storage.connection.execute("SELECT COUNT(*) FROM ingress_messages").fetchone()[0] == 0


def test_topic_group_routes_only_a_known_task_thread(tmp_path: Path) -> None:
    contract = tmp_path / "contract.json"
    contract.write_text("{}", encoding="utf-8")
    topic_binding = FeishuBinding(
        "tenant",
        "app",
        "owner",
        "fallback-p2p",
        "target",
        contract,
        ConversationMode.TOPIC_GROUP,
        "topic-chat",
    )
    with RuntimeStorage(tmp_path / "db.sqlite") as storage:
        storage.initialize_runtime(sink_mode="control")
        storage.connection.execute(
            "INSERT INTO identity_bindings(binding_key,tenant_key,app_id,owner_open_id,p2p_chat_id,"
            "active_chat_id,active_chat_type,conversation_mode,binding_epoch,contract_hash,state,updated_at) "
            "VALUES('owner','tenant','app','owner','fallback-p2p','topic-chat','group','topic_group',"
            "1,'hash','active',?)",
            (utc_now(),),
        )
        storage.connection.execute(
            "INSERT INTO task_bindings(thread_id,project_root,chat_id,anchor_message_id,anchor_state,"
            "anchor_uuid,anchor_marker,conversation_mode,current_binding_epoch,identity_binding_epoch,"
            "opted_in,updated_at) VALUES('thread','D:/project','topic-chat','anchor','confirmed','u','m',"
            "'topic_group',1,1,1,?)",
            (utc_now(),),
        )
        storage.connection.execute(
            "INSERT INTO chat_sequences(binding_key,next_ingest_seq,active_binding_epoch,selection_state,updated_at) "
            "VALUES('owner',1,0,'active',?)",
            (utc_now(),),
        )
        storage.connection.execute(
            "INSERT INTO message_ancestry(message_id,root_id,parent_id,thread_id,chat_id,source,created_at) "
            "VALUES('anchor',NULL,NULL,'thread','topic-chat','anchor',?)",
            (utc_now(),),
        )
        storage.connection.execute(
            "INSERT INTO project_groups(project_id,project_kind,display_name,root_paths_json,chat_id,"
            "chat_mode,group_message_type,state,last_activity_ms,created_at,updated_at) "
            "VALUES('project','local','Project','[\"D:/project\"]','topic-chat','group','thread',"
            "'active',1,?,?)",
            (utc_now(), utc_now()),
        )

        unquoted = event("top", "new topic", chat="topic-chat")
        unquoted["event"]["message"]["chat_type"] = "group"
        assert IngressRouter(storage, topic_binding).ingest(unquoted).routing_state == "routing_indeterminate"

        reply = event("reply", "continue", root="anchor", parent="anchor", chat="topic-chat")
        reply["event"]["message"].update(
            {"chat_type": "group", "thread_id": "provider-thread"}
        )
        routed = IngressRouter(storage, topic_binding).ingest(reply)
        assert routed.routing_state == "routed_reply" and routed.target_thread_id == "thread"
        task = storage.connection.execute(
            "SELECT provider_thread_id FROM task_bindings WHERE thread_id='thread'"
        ).fetchone()
        assert task["provider_thread_id"] == "provider-thread"


def _prepare_topic_storage(storage: RuntimeStorage) -> FeishuBinding:
    contract = storage.path.parent / "contract.json"
    contract.write_text("{}", encoding="utf-8")
    topic_binding = FeishuBinding(
        "tenant", "app", "owner", "fallback", "target", contract,
        ConversationMode.TOPIC_GROUP, "topic-chat"
    )
    storage.connection.execute(
        "INSERT INTO identity_bindings(binding_key,tenant_key,app_id,owner_open_id,p2p_chat_id,"
        "active_chat_id,active_chat_type,conversation_mode,binding_epoch,contract_hash,state,updated_at) "
        "VALUES('owner','tenant','app','owner','fallback','topic-chat','group','topic_group',"
        "1,'hash','active',?)",
        (utc_now(),),
    )
    storage.connection.execute(
        "INSERT INTO project_groups(project_id,project_kind,display_name,root_paths_json,chat_id,"
        "chat_mode,group_message_type,state,last_activity_ms,created_at,updated_at) "
        "VALUES('project','local','Project','[\"D:/project\"]','topic-chat','group','thread',"
        "'active',1,?,?)",
        (utc_now(), utc_now()),
    )
    storage.connection.execute(
        "INSERT INTO task_bindings(thread_id,project_root,chat_id,anchor_message_id,anchor_state,"
        "anchor_uuid,anchor_marker,conversation_mode,provider_thread_id,current_binding_epoch,"
        "identity_binding_epoch,opted_in,updated_at) VALUES('thread','D:/project','topic-chat',"
        "'anchor','confirmed','u','m','topic_group','provider-thread',1,1,1,?)",
        (utc_now(),),
    )
    storage.connection.execute(
        "INSERT INTO message_ancestry(message_id,root_id,parent_id,thread_id,chat_id,provider_thread_id,"
        "source,created_at) VALUES('anchor',NULL,NULL,'thread','topic-chat','provider-thread','anchor',?)",
        (utc_now(),),
    )
    return topic_binding


def test_topic_attachment_is_persisted_without_network_io_in_callback(tmp_path: Path) -> None:
    with RuntimeStorage(tmp_path / "db.sqlite") as storage:
        storage.initialize_runtime(sink_mode="control")
        topic_binding = _prepare_topic_storage(storage)
        capabilities = RemoteCapabilities(
            enabled=True, text=True, images=True, files=True, approvals=True, controls=True
        )
        incoming = event("file-message", "", root="anchor", parent="anchor", chat="topic-chat")
        incoming["event"]["message"].update(
            {
                "chat_type": "group",
                "thread_id": "provider-thread",
                "message_type": "file",
                "content": json.dumps(
                    {"file_key": "file_resource-1", "file_name": "数据表.xlsx"},
                    ensure_ascii=False,
                ),
            }
        )
        decision = IngressRouter(storage, topic_binding, capabilities).ingest(incoming)
        assert decision.target_thread_id == "thread" and decision.routing_state == "routed_reply"
        attachment = storage.connection.execute(
            "SELECT resource_type,original_file_name,state,content FROM ingress_attachments "
            "WHERE message_id='file-message'"
        ).fetchone()
        assert tuple(attachment) == ("file", "数据表.xlsx", "pending", None)


def test_callback_threads_are_serialized_and_sequence_is_unique(tmp_path: Path) -> None:
    with RuntimeStorage(tmp_path / "db.sqlite") as storage:
        storage.initialize_runtime(sink_mode="control")
        topic_binding = _prepare_topic_storage(storage)
        router = IngressRouter(storage, topic_binding)

        def ingest(index: int) -> int:
            incoming = event(
                f"parallel-{index}", f"work-{index}", root="anchor", parent="anchor", chat="topic-chat"
            )
            incoming["event"]["message"].update(
                {"chat_type": "group", "thread_id": "provider-thread"}
            )
            return int(router.ingest(incoming).ingest_seq)

        with ThreadPoolExecutor(max_workers=4) as executor:
            sequences = list(executor.map(ingest, range(12)))
        assert sorted(sequences) == list(range(1, 13))
