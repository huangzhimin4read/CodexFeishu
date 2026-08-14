from pathlib import Path
from dataclasses import replace

import pytest

from codex_feishu_bridge.runtime_storage import RuntimeStorage, utc_now
from codex_feishu_bridge.security.approvals import (
    ApprovalBroker,
    ApprovalContext,
    ApprovalError,
)
from codex_feishu_bridge.security.audit import AuditChain, AuditError
from codex_feishu_bridge.security.emergency import EmergencyController


def context() -> ApprovalContext:
    return ApprovalContext(
        approval_id="approval",
        server_request_id="7",
        server_method="item/fileChange/requestApproval",
        thread_id="thread",
        turn_id="turn",
        tenant_key="tenant",
        app_id="app",
        chat_id="chat",
        card_message_id="card",
        operator_open_id="owner",
        binding_epoch=2,
        identity_binding_epoch=3,
        kill_generation=4,
        server_epoch="server",
        connection_epoch="connection",
        session_id="session",
        service_fencing_token=5,
    )


def test_approval_token_is_single_use_and_response_hash_is_immutable(tmp_path: Path) -> None:
    with RuntimeStorage(tmp_path / "db.sqlite") as storage:
        storage.initialize_runtime(sink_mode="control")
        audit = AuditChain(storage, b"a" * 32)
        broker = ApprovalBroker(storage, b"b" * 32, audit)
        issued = broker.issue(
            context=context(),
            request={"threadId": "thread", "itemId": "item"},
            decision_map={"accept": {"decision": "accept"}},
        )
        approval_id, response = broker.commit_card_action(
            opaque_token=issued.opaque_token,
            decision="accept",
            tenant_key="tenant",
            app_id="app",
            chat_id="chat",
            card_message_id="card",
            operator_open_id="owner",
            binding_epoch=2,
            identity_binding_epoch=3,
            kill_generation=4,
            server_epoch="server",
            connection_epoch="connection",
            session_id="session",
            service_fencing_token=5,
        )
        assert response == {"decision": "accept"}
        with pytest.raises(ApprovalError, match="already consumed"):
            broker.commit_card_action(
                opaque_token=issued.opaque_token,
                decision="accept",
                tenant_key="tenant",
                app_id="app",
                chat_id="chat",
                card_message_id="card",
                operator_open_id="owner",
                binding_epoch=2,
                identity_binding_epoch=3,
                kill_generation=4,
                server_epoch="server",
                connection_epoch="connection",
                session_id="session",
                service_fencing_token=5,
            )
        response_hash = broker.commit_exact_response(approval_id, response)
        broker.record_response_result(approval_id, accepted=None)
        row = storage.connection.execute(
            "SELECT state,response_hash FROM approval_requests WHERE approval_id='approval'"
        ).fetchone()
        assert tuple(row) == ("outcome_unknown", response_hash)
        assert audit.verify() == 3


def test_approval_context_mismatch_does_not_consume_token(tmp_path: Path) -> None:
    with RuntimeStorage(tmp_path / "db.sqlite") as storage:
        storage.initialize_runtime(sink_mode="control")
        broker = ApprovalBroker(storage, b"b" * 32)
        issued = broker.issue(
            context=context(), request={"x": 1}, decision_map={"accept": {"decision": "accept"}}
        )
        with pytest.raises(ApprovalError, match="binding mismatch"):
            broker.commit_card_action(
                opaque_token=issued.opaque_token,
                decision="accept",
                tenant_key="tenant",
                app_id="app",
                chat_id="wrong",
                card_message_id="card",
                operator_open_id="owner",
                binding_epoch=2,
                identity_binding_epoch=3,
                kill_generation=4,
                server_epoch="server",
                connection_epoch="connection",
                session_id="session",
                service_fencing_token=5,
            )
        assert storage.connection.execute(
            "SELECT consumed_at FROM approval_actions"
        ).fetchone()[0] is None


def test_approval_from_previous_service_session_cannot_be_consumed(tmp_path: Path) -> None:
    with RuntimeStorage(tmp_path / "db.sqlite") as storage:
        storage.initialize_runtime(sink_mode="control")
        broker = ApprovalBroker(storage, b"b" * 32)
        issued = broker.issue(
            context=context(), request={"x": 1}, decision_map={"accept": {"decision": "accept"}}
        )
        with pytest.raises(ApprovalError, match="binding mismatch"):
            broker.commit_card_action(
                opaque_token=issued.opaque_token,
                decision="accept",
                tenant_key="tenant",
                app_id="app",
                chat_id="chat",
                card_message_id="card",
                operator_open_id="owner",
                binding_epoch=2,
                identity_binding_epoch=3,
                kill_generation=4,
                server_epoch="new-server",
                connection_epoch="new-connection",
                session_id="new-session",
                service_fencing_token=6,
            )
        assert storage.connection.execute(
            "SELECT consumed_at FROM approval_actions"
        ).fetchone()[0] is None


def test_approval_request_ids_are_idempotent_per_connection_and_reusable_after_reconnect(
    tmp_path: Path,
) -> None:
    with RuntimeStorage(tmp_path / "db.sqlite") as storage:
        storage.initialize_runtime(sink_mode="control")
        broker = ApprovalBroker(storage, b"b" * 32)
        first = broker.issue(
            context=context(), request={"x": 1}, decision_map={"accept": {"decision": "accept"}}
        )
        duplicate = broker.issue(
            context=context(), request={"x": 1}, decision_map={"accept": {"decision": "accept"}}
        )
        reconnected = broker.issue(
            context=replace(
                context(),
                approval_id="approval-after-reconnect",
                server_epoch="server-2",
                connection_epoch="connection-2",
                session_id="session-2",
            ),
            request={"x": 1},
            decision_map={"accept": {"decision": "accept"}},
        )

        assert first.created and first.opaque_token
        assert not duplicate.created and duplicate.opaque_token is None
        assert reconnected.created and reconnected.opaque_token
        assert storage.connection.execute(
            "SELECT COUNT(*) FROM approval_actions WHERE server_request_id='7'"
        ).fetchone()[0] == 2


def test_approval_request_id_content_conflict_fails_closed(tmp_path: Path) -> None:
    with RuntimeStorage(tmp_path / "db.sqlite") as storage:
        storage.initialize_runtime(sink_mode="control")
        broker = ApprovalBroker(storage, b"b" * 32)
        broker.issue(
            context=context(), request={"x": 1}, decision_map={"accept": {"decision": "accept"}}
        )
        with pytest.raises(ApprovalError, match="reused with different content"):
            broker.issue(
                context=context(),
                request={"x": 2},
                decision_map={"accept": {"decision": "accept"}},
            )


def test_audit_chain_rejects_body_fields_and_detects_tamper(tmp_path: Path) -> None:
    with RuntimeStorage(tmp_path / "db.sqlite") as storage:
        storage.initialize_runtime(sink_mode="control")
        audit = AuditChain(storage, b"x" * 32)
        audit.append({"event": "started", "item_hash": "h"})
        with pytest.raises(AuditError, match="forbidden"):
            audit.append({"event": "bad", "text": "secret"})
        storage.connection.execute("UPDATE audit_chain SET record_hash='changed' WHERE sequence=1")
        with pytest.raises(AuditError, match="HMAC"):
            audit.verify()


def test_hard_stop_fences_then_terminates_and_revokes(tmp_path: Path) -> None:
    calls: list[str] = []
    with RuntimeStorage(tmp_path / "db.sqlite") as storage:
        storage.initialize_runtime(sink_mode="control")
        storage.connection.execute(
            "INSERT INTO active_grants(grant_id,server_epoch,connection_epoch,session_id,kill_generation,expires_at) "
            "VALUES('g','s','c','session',0,'2999-01-01T00:00:00Z')"
        )
        storage.connection.execute(
            "INSERT INTO task_bindings(thread_id,project_root,chat_id,anchor_state,current_binding_epoch,"
            "identity_binding_epoch,conversation_mode,opted_in,updated_at) "
            "VALUES('thread','D:/project','chat','confirmed',1,1,'topic_group',1,?)",
            (utc_now(),),
        )
        storage.connection.execute(
            "INSERT INTO remote_task_grants(thread_id,project_root,chat_id,task_binding_epoch,"
            "identity_binding_epoch,service_fencing_token,capabilities_json,capabilities_hash,state,"
            "authorized_at,updated_at) VALUES('thread','D:/project','chat',1,1,1,'{}','hash','active',?,?)",
            (utc_now(), utc_now()),
        )
        result = EmergencyController(storage, lambda: calls.append("terminated")).hard_stop("fixture")
        assert calls == ["terminated"] and result.grants_revoked == 1
        assert storage.connection.execute("SELECT kill_generation FROM service_state").fetchone()[0] == 1
        assert storage.connection.execute(
            "SELECT state FROM remote_task_grants WHERE thread_id='thread'"
        ).fetchone()[0] == "revoked"
