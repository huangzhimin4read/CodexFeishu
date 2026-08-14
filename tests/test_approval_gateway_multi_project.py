import json
import queue
from hashlib import sha256
from pathlib import Path

from codex_feishu_bridge.codex.approval_gateway import ApprovalGateway
from codex_feishu_bridge.runtime_config import ConversationMode, FeishuBinding
from codex_feishu_bridge.runtime_storage import RuntimeStorage, utc_now
from codex_feishu_bridge.security.approvals import ApprovalBroker
from codex_feishu_bridge.security.jcs import canonicalize


class FakeConnection:
    def __init__(self) -> None:
        self.server_requests: queue.Queue[dict] = queue.Queue()
        self.responses: list[tuple[object, dict]] = []
        self.error_responses: list[tuple[object, int, str]] = []

    def respond(self, request_id: object, response: dict) -> None:
        self.responses.append((request_id, response))

    def respond_error(self, request_id: object, *, code: int, message: str) -> None:
        self.error_responses.append((request_id, code, message))


def test_unsupported_server_request_is_rejected_without_crashing_gateway(tmp_path: Path) -> None:
    contract = tmp_path / "contract.json"
    contract.write_text("{}", encoding="utf-8")
    binding = FeishuBinding(
        "tenant",
        "app",
        "owner",
        "p2p",
        "credential",
        contract,
        ConversationMode.TOPIC_GROUP,
        "primary-chat",
    )
    with RuntimeStorage(tmp_path / "db.sqlite") as storage:
        storage.initialize_runtime(sink_mode="control")
        connection = FakeConnection()
        connection.server_requests.put(
            {
                "id": 91,
                "method": "item/tool/call",
                "params": {
                    "threadId": "thread",
                    "turnId": "turn",
                    "callId": "call",
                    "tool": "not-enabled",
                    "arguments": {},
                },
            }
        )
        gateway = ApprovalGateway(
            storage,
            connection,
            ApprovalBroker(storage, b"k" * 32),
            binding,
            server_epoch="server",
            connection_epoch="connection",
            session_id="session",
        )

        assert gateway.publish_next_request()
        assert connection.error_responses == [
            (91, -32601, "Server request is not available in the remote bridge")
        ]
        assert connection.server_requests.empty()


def test_approval_card_binds_to_the_task_project_chat_not_primary_chat(tmp_path: Path) -> None:
    contract = tmp_path / "contract.json"
    contract.write_text("{}", encoding="utf-8")
    binding = FeishuBinding(
        "tenant",
        "app",
        "owner",
        "p2p",
        "credential",
        contract,
        ConversationMode.TOPIC_GROUP,
        "primary-chat",
    )
    with RuntimeStorage(tmp_path / "db.sqlite") as storage:
        storage.initialize_runtime(sink_mode="control")
        storage.connection.execute(
            "INSERT INTO identity_bindings(binding_key,tenant_key,app_id,owner_open_id,p2p_chat_id,"
            "active_chat_id,active_chat_type,conversation_mode,binding_epoch,contract_hash,state,updated_at) "
            "VALUES('owner','tenant','app','owner','p2p','primary-chat','group','topic_group',"
            "1,'hash','active',?)",
            (utc_now(),),
        )
        storage.connection.execute(
            "INSERT INTO project_groups(project_id,project_kind,display_name,root_paths_json,chat_id,"
            "chat_mode,group_message_type,state,last_activity_ms,created_at,updated_at) "
            "VALUES('second','local','Second','[\"D:/second\"]','second-chat','group','thread',"
            "'active',1,?,?)",
            (utc_now(), utc_now()),
        )
        storage.connection.execute(
            "INSERT INTO task_bindings(thread_id,project_root,chat_id,anchor_message_id,anchor_state,"
            "anchor_uuid,anchor_marker,current_binding_epoch,identity_binding_epoch,conversation_mode,"
            "opted_in,updated_at) VALUES('thread','D:/second','second-chat','anchor','confirmed',"
            "'uuid','marker',1,1,'topic_group',1,?)",
            (utc_now(),),
        )
        capability_value = {
            "text": True,
            "image": True,
            "file": True,
            "approvals": True,
            "controls": True,
        }
        encoded_capabilities = canonicalize(capability_value)
        capabilities = encoded_capabilities.decode("utf-8")
        storage.connection.execute(
            "UPDATE service_state SET process_state='running',fencing_token=1,updated_at=? WHERE singleton=1",
            (utc_now(),),
        )
        storage.connection.execute(
            "INSERT INTO remote_task_grants(thread_id,project_root,chat_id,task_binding_epoch,"
            "identity_binding_epoch,service_fencing_token,capabilities_json,capabilities_hash,state,authorized_at,updated_at) "
            "VALUES('thread','D:/second','second-chat',1,1,1,?,?,'active',?,?)",
            (capabilities, sha256(encoded_capabilities).hexdigest(), utc_now(), utc_now()),
        )
        connection = FakeConnection()
        connection.server_requests.put(
            {
                "id": 41,
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "threadId": "thread",
                    "turnId": "turn",
                    "availableDecisions": ["accept", "decline"],
                },
            }
        )
        gateway = ApprovalGateway(
            storage,
            connection,
            ApprovalBroker(storage, b"k" * 32),
            binding,
            server_epoch="server",
            connection_epoch="connection",
            session_id="session",
        )
        assert gateway.publish_next_request()
        action = storage.connection.execute("SELECT * FROM approval_actions").fetchone()
        assert action["chat_id"] == "second-chat"
        outbox = storage.connection.execute(
            "SELECT body_json,thread_id,target_message_id FROM provider_outbox WHERE operation='approval'"
        ).fetchone()
        assert outbox["thread_id"] == "thread" and outbox["target_message_id"] == "anchor"
        card = json.loads(outbox["body_json"])
        token = card["elements"][1]["actions"][0]["value"]["token"]
        storage.connection.execute(
            "UPDATE approval_actions SET card_message_id='card-message' WHERE approval_id=?",
            (action["approval_id"],),
        )
        result = gateway.handle_card_action(
            {
                "header": {"tenant_key": "tenant", "app_id": "app"},
                "event": {
                    "operator": {"open_id": "owner"},
                    "context": {
                        "open_chat_id": "second-chat",
                        "open_message_id": "card-message",
                    },
                    "action": {"value": {"token": token, "decision": "accept"}},
                },
            }
        )
        assert result["toast"]["type"] == "success"
        assert connection.responses == [(41, {"decision": "accept"})]


def test_auto_approve_uses_session_decision_without_feishu_card(tmp_path: Path) -> None:
    contract = tmp_path / "contract.json"
    contract.write_text("{}", encoding="utf-8")
    binding = FeishuBinding(
        "tenant", "app", "owner", "p2p", "credential", contract,
        ConversationMode.TOPIC_GROUP, "primary-chat",
    )
    with RuntimeStorage(tmp_path / "db.sqlite") as storage:
        storage.initialize_runtime(sink_mode="control")
        storage.connection.execute(
            "INSERT INTO identity_bindings(binding_key,tenant_key,app_id,owner_open_id,p2p_chat_id,"
            "active_chat_id,active_chat_type,conversation_mode,binding_epoch,contract_hash,state,updated_at) "
            "VALUES('owner','tenant','app','owner','p2p','primary-chat','group','topic_group',"
            "1,'hash','active',?)",
            (utc_now(),),
        )
        storage.connection.execute(
            "INSERT INTO project_groups(project_id,project_kind,display_name,root_paths_json,chat_id,"
            "state,last_activity_ms,created_at,updated_at) VALUES('project','local','Project','[]',"
            "'primary-chat','active',1,?,?)",
            (utc_now(), utc_now()),
        )
        storage.connection.execute(
            "INSERT INTO task_bindings(thread_id,project_root,chat_id,anchor_message_id,anchor_state,"
            "current_binding_epoch,identity_binding_epoch,conversation_mode,opted_in,updated_at) "
            "VALUES('thread','D:/project','primary-chat','anchor','confirmed',1,1,'topic_group',1,?)",
            (utc_now(),),
        )
        storage.connection.execute(
            "INSERT INTO thread_bindings(thread_id,ownership_state,updated_at) "
            "VALUES('thread','bridge_owned',?)",
            (utc_now(),),
        )
        storage.connection.execute(
            "UPDATE service_state SET process_state='running',fencing_token=1,updated_at=? WHERE singleton=1",
            (utc_now(),),
        )
        connection = FakeConnection()
        connection.server_requests.put(
            {
                "id": 7,
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "threadId": "thread",
                    "turnId": "turn",
                    "availableDecisions": ["accept", "acceptForSession", "decline"],
                },
            }
        )
        gateway = ApprovalGateway(
            storage,
            connection,
            ApprovalBroker(storage, b"k" * 32),
            binding,
            server_epoch="server",
            connection_epoch="connection",
            session_id="session",
            auto_approve=True,
        )

        assert gateway.publish_next_request()
        assert connection.responses == [(7, {"decision": "acceptForSession"})]
        assert storage.connection.execute(
            "SELECT COUNT(*) FROM provider_outbox WHERE operation='approval'"
        ).fetchone()[0] == 0
        assert storage.connection.execute(
            "SELECT state FROM approval_requests"
        ).fetchone()[0] == "outcome_unknown"
