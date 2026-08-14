import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace

from codex_feishu_bridge.feishu.client import ProviderOutcome, ProviderResult
from codex_feishu_bridge.feishu.reconciliation import SendReconciler
from codex_feishu_bridge.runtime_storage import RuntimeStorage, utc_now


def test_unknown_image_reply_reconciles_by_persisted_image_key(tmp_path) -> None:
    body_json = json.dumps({"image_key": "img-fixture"}, separators=(",", ":"))

    class Contract:
        @staticmethod
        def endpoint(name: str, *, require_enabled: bool = True):
            if name == "reply_message":
                return SimpleNamespace(uuid_window_seconds=1, enabled=True)
            if name == "list_messages":
                return SimpleNamespace(uuid_window_seconds=None, enabled=True)
            raise AssertionError(name)

    class Client:
        app_id = "app"
        contract = Contract()

        @staticmethod
        def call(endpoint: str, **kwargs):
            assert endpoint == "list_messages"
            return ProviderResult(
                ProviderOutcome.CONFIRMED,
                "0",
                response={
                    "code": 0,
                    "data": {
                        "has_more": False,
                        "items": [
                            {
                                "message_id": "provider-image-message",
                                "msg_type": "image",
                                "sender": {"sender_type": "bot", "id": "app"},
                                "body": {"content": body_json},
                            }
                        ],
                    },
                },
            )

    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="outbound")
        storage.connection.execute(
            "INSERT INTO task_bindings(thread_id,project_root,chat_id,anchor_message_id,anchor_state,"
            "anchor_uuid,anchor_marker,opted_in,updated_at) "
            "VALUES('thread','D:/project','chat','anchor','confirmed','uuid','marker',1,?)",
            (utc_now(),),
        )
        outbox_id = storage.enqueue_provider_message(
            logical_message_id="image:unknown",
            thread_id="thread",
            operation="final",
            message_type="image",
            endpoint_name="reply_message",
            target_message_id="anchor",
            stable_uuid="stable-image",
            marker="img:fixture",
            body_json=body_json,
            body_hash=sha256(body_json.encode()).hexdigest(),
            priority=100,
        )
        old = (datetime.now(UTC) - timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
        storage.connection.execute(
            "UPDATE provider_outbox SET state='unknown',first_attempt_at=? WHERE outbox_id=?",
            (old, outbox_id),
        )
        result = SendReconciler(storage, Client()).reconcile(outbox_id)
        assert result.state == "confirmed"
        assert result.message_id == "provider-image-message"
        row = storage.connection.execute(
            "SELECT state,provider_message_id FROM provider_outbox WHERE outbox_id=?",
            (outbox_id,),
        ).fetchone()
        assert tuple(row) == ("confirmed", "provider-image-message")


def test_unknown_text_reconciles_by_exact_clean_body_without_ui_marker(tmp_path) -> None:
    body_json = json.dumps({"text": "用户可见正文"}, ensure_ascii=False, separators=(",", ":"))

    class Contract:
        @staticmethod
        def endpoint(name: str, *, require_enabled: bool = True):
            if name == "reply_message":
                return SimpleNamespace(uuid_window_seconds=1, enabled=True)
            if name == "list_messages":
                return SimpleNamespace(uuid_window_seconds=None, enabled=True)
            raise AssertionError(name)

    class Client:
        app_id = "app"
        contract = Contract()

        @staticmethod
        def call(endpoint: str, **kwargs):
            assert endpoint == "list_messages"
            return ProviderResult(
                ProviderOutcome.CONFIRMED,
                "0",
                response={
                    "data": {
                        "has_more": False,
                        "items": [
                            {
                                "message_id": "provider-text-message",
                                "msg_type": "text",
                                "sender": {"sender_type": "bot", "id": "app"},
                                "body": {"content": body_json},
                            }
                        ],
                    }
                },
            )

    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="outbound")
        storage.connection.execute(
            "INSERT INTO task_bindings(thread_id,project_root,chat_id,anchor_message_id,anchor_state,"
            "anchor_uuid,anchor_marker,opted_in,updated_at) "
            "VALUES('thread','D:/project','chat','anchor','confirmed','uuid','local-marker',1,?)",
            (utc_now(),),
        )
        outbox_id = storage.enqueue_provider_message(
            logical_message_id="text:unknown",
            thread_id="thread",
            operation="final",
            endpoint_name="reply_message",
            target_message_id="anchor",
            stable_uuid="stable-text",
            marker="local-marker-not-in-body",
            body_json=body_json,
            body_hash=sha256(body_json.encode()).hexdigest(),
            priority=100,
        )
        old = (datetime.now(UTC) - timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
        storage.connection.execute(
            "UPDATE provider_outbox SET state='unknown',first_attempt_at=? WHERE outbox_id=?",
            (old, outbox_id),
        )
        result = SendReconciler(storage, Client()).reconcile(outbox_id)
        assert result == result.__class__("confirmed", "provider-text-message", 1)


def test_unknown_text_rejects_same_body_from_another_application(tmp_path) -> None:
    body_json = json.dumps({"text": "相同正文"}, ensure_ascii=False, separators=(",", ":"))

    class Contract:
        @staticmethod
        def endpoint(name: str, *, require_enabled: bool = True):
            if name == "reply_message":
                return SimpleNamespace(uuid_window_seconds=1, enabled=True)
            if name == "list_messages":
                return SimpleNamespace(uuid_window_seconds=None, enabled=True)
            raise AssertionError(name)

    class Client:
        app_id = "expected-app"
        contract = Contract()

        @staticmethod
        def call(endpoint: str, **kwargs):
            assert endpoint == "list_messages"
            return ProviderResult(
                ProviderOutcome.CONFIRMED,
                "0",
                response={
                    "data": {
                        "has_more": False,
                        "items": [
                            {
                                "message_id": "another-app-message",
                                "msg_type": "text",
                                "sender": {"sender_type": "bot", "id": "another-app"},
                                "body": {"content": body_json},
                            }
                        ],
                    }
                },
            )

    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="outbound")
        storage.connection.execute(
            "INSERT INTO task_bindings(thread_id,project_root,chat_id,anchor_message_id,anchor_state,"
            "anchor_uuid,anchor_marker,opted_in,updated_at) "
            "VALUES('thread','D:/project','chat','anchor','confirmed','uuid','local-marker',1,?)",
            (utc_now(),),
        )
        outbox_id = storage.enqueue_provider_message(
            logical_message_id="text:wrong-app",
            thread_id="thread",
            operation="final",
            endpoint_name="reply_message",
            target_message_id="anchor",
            stable_uuid="stable-text-wrong-app",
            marker="local-marker-not-in-body",
            body_json=body_json,
            body_hash=sha256(body_json.encode()).hexdigest(),
            priority=100,
        )
        old = (datetime.now(UTC) - timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
        storage.connection.execute(
            "UPDATE provider_outbox SET state='unknown',first_attempt_at=? WHERE outbox_id=?",
            (old, outbox_id),
        )
        result = SendReconciler(storage, Client()).reconcile(outbox_id)
        assert result == result.__class__("delivery_indeterminate", None, 0)


def test_disabled_history_endpoint_persists_indeterminate_state(tmp_path) -> None:
    class Contract:
        @staticmethod
        def endpoint(name: str, *, require_enabled: bool = True):
            if name == "reply_message":
                return SimpleNamespace(uuid_window_seconds=1, enabled=True)
            if name == "list_messages":
                return SimpleNamespace(uuid_window_seconds=None, enabled=False)
            raise AssertionError(name)

    class Client:
        app_id = "app"
        contract = Contract()

    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="outbound")
        outbox_id = storage.enqueue_provider_message(
            logical_message_id="history-disabled",
            operation="commentary",
            endpoint_name="reply_message",
            stable_uuid="stable-disabled",
            marker="marker",
            body_json='{"text":"unknown"}',
            body_hash="hash",
            priority=1,
        )
        old = (datetime.now(UTC) - timedelta(seconds=10)).isoformat().replace(
            "+00:00", "Z"
        )
        storage.connection.execute(
            "UPDATE provider_outbox SET state='unknown',first_attempt_at=? WHERE outbox_id=?",
            (old, outbox_id),
        )

        result = SendReconciler(storage, Client()).reconcile(outbox_id)

        assert result == result.__class__("delivery_indeterminate", None, 0)
        row = storage.connection.execute(
            "SELECT state,last_error_code FROM provider_outbox WHERE outbox_id=?",
            (outbox_id,),
        ).fetchone()
        assert tuple(row) == (
            "delivery_indeterminate",
            "reconciliation_endpoint_disabled",
        )


def test_unknown_user_message_reconciles_only_from_configured_owner(tmp_path) -> None:
    body_json = json.dumps(
        {"text": "用户身份消息"}, ensure_ascii=False, separators=(",", ":")
    )

    class Contract:
        @staticmethod
        def endpoint(name: str, *, require_enabled: bool = True):
            if name == "reply_message":
                return SimpleNamespace(uuid_window_seconds=1, enabled=True)
            if name == "list_messages":
                return SimpleNamespace(uuid_window_seconds=None, enabled=True)
            raise AssertionError(name)

    class Client:
        app_id = "app"
        contract = Contract()

        @staticmethod
        def call(endpoint: str, **kwargs):
            assert endpoint == "list_messages"
            return ProviderResult(
                ProviderOutcome.CONFIRMED,
                "0",
                response={
                    "data": {
                        "has_more": False,
                        "items": [
                            {
                                "message_id": "owner-user-message",
                                "msg_type": "text",
                                "sender": {"sender_type": "user", "id": "owner"},
                                "body": {"content": body_json},
                            }
                        ],
                    }
                },
            )

    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="outbound")
        storage.connection.execute(
            "INSERT INTO identity_bindings(binding_key,tenant_key,app_id,owner_open_id,p2p_chat_id,"
            "binding_epoch,contract_hash,state,updated_at) "
            "VALUES('owner','tenant','app','owner','chat',1,'hash','active',?)",
            (utc_now(),),
        )
        storage.connection.execute(
            "INSERT INTO task_bindings(thread_id,project_root,chat_id,anchor_message_id,anchor_state,"
            "anchor_uuid,anchor_marker,opted_in,updated_at) "
            "VALUES('thread','D:/project','chat','anchor','confirmed','uuid','marker',1,?)",
            (utc_now(),),
        )
        outbox_id = storage.enqueue_provider_message(
            logical_message_id="user:unknown",
            thread_id="thread",
            operation="user_message",
            endpoint_name="reply_message",
            target_message_id="anchor",
            stable_uuid="stable-user",
            marker="marker",
            body_json=body_json,
            body_hash=sha256(body_json.encode()).hexdigest(),
            priority=10,
        )
        old = (datetime.now(UTC) - timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
        storage.connection.execute(
            "UPDATE provider_outbox SET state='unknown',first_attempt_at=? WHERE outbox_id=?",
            (old, outbox_id),
        )
        result = SendReconciler(storage, Client()).reconcile(outbox_id)
        assert result == result.__class__("confirmed", "owner-user-message", 1)
