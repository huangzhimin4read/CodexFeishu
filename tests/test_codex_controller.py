import json
from hashlib import sha256
from pathlib import Path

from codex_feishu_bridge.codex.controller import CodexController
from codex_feishu_bridge.codex.execution_profile import ExecutionProfile, SandboxType
from codex_feishu_bridge.controls import ProfileController
from codex_feishu_bridge.models import OwnershipState
from codex_feishu_bridge.runtime_storage import RuntimeStorage, utc_now
from codex_feishu_bridge.security.jcs import canonicalize


ROOT = Path(__file__).parents[1]


class FakeConnection:
    def __init__(
        self,
        *,
        fail_queue: bool = False,
        queue_after_failure: bool = False,
        thread_status: str = "idle",
        active_turns: tuple[str, ...] = ("turn-active",),
    ) -> None:
        self.fail_queue = fail_queue
        self.queue_after_failure = queue_after_failure
        self.thread_status = thread_status
        self.active_turns = active_turns
        self.last_client_id = ""
        self.calls = []

    def request(self, method, params, **kwargs):
        self.calls.append((method, params))
        if method in {"thread/read", "thread/resume"}:
            return {
                "thread": {
                    "status": {"type": self.thread_status},
                    "canAcceptDirectInput": True,
                    "turns": [
                        {"id": turn_id, "items": [], "status": "inProgress"}
                        for turn_id in self.active_turns
                    ]
                    if self.thread_status == "active"
                    else [],
                }
            }
        if method == "configRequirements/read":
            return {"requirements": None}
        if method == "thread/queue/add":
            self.last_client_id = params["clientUserMessageId"]
            if "before_send" in kwargs:
                kwargs["before_send"]({"id": 9, "method": method, "params": params})
            if self.fail_queue:
                raise OSError("fixture transport uncertainty")
            return {
                "queuedSubmission": {
                    "id": "queued-1",
                    "input": params["input"],
                    "clientUserMessageId": params["clientUserMessageId"],
                }
            }
        if method == "thread/queue/list":
            return {
                "data": [
                    {
                        "id": "queued-after-timeout",
                        "input": [{"type": "text", "text": "work"}],
                        "clientUserMessageId": self.last_client_id,
                    }
                ]
                if self.queue_after_failure
                else [],
                "nextCursor": None,
            }
        if method == "turn/steer":
            if "before_send" in kwargs:
                kwargs["before_send"]({"id": 10, "method": method, "params": params})
            return {"turnId": params["expectedTurnId"]}
        if method == "turn/interrupt":
            return {}
        raise AssertionError(method)


class ProfileConnection:
    def __init__(self, cwd: Path, *, mismatch: bool = False) -> None:
        self.cwd = cwd
        self.mismatch = mismatch

    def request(self, method, params, **kwargs):
        assert method == "thread/resume"
        return {
            "cwd": str(self.cwd / "different") if self.mismatch else str(self.cwd),
            "approvalPolicy": "on-request",
            "approvalsReviewer": "user",
            # This is the thread default and deliberately differs from the
            # workspaceWrite policy supplied to turn/start.
            "sandbox": {"type": "dangerFullAccess"},
        }


def prepare(storage: RuntimeStorage, tmp_path: Path) -> str:
    storage.connection.execute(
        "INSERT INTO task_bindings(thread_id,project_root,chat_id,anchor_message_id,anchor_state,"
        "anchor_uuid,anchor_marker,current_binding_epoch,identity_binding_epoch,opted_in,updated_at) "
        "VALUES('thread',?,'chat','anchor','confirmed','u','m',1,1,1,?)",
        (str(tmp_path), utc_now()),
    )
    storage.connection.execute(
        "INSERT INTO identity_bindings(binding_key,tenant_key,app_id,owner_open_id,p2p_chat_id,"
        "binding_epoch,contract_hash,state,updated_at) VALUES('owner','tenant','app','owner','chat',"
        "1,'hash','active',?)",
        (utc_now(),),
    )
    storage.create_thread("thread", OwnershipState.BRIDGE_OWNED)
    storage.connection.execute(
        "UPDATE service_state SET process_state='running',fencing_token=1,updated_at=? WHERE singleton=1",
        (utc_now(),),
    )
    profile = ExecutionProfile(
        SandboxType.WORKSPACE_WRITE,
        tmp_path,
        network_access=False,
        writable_roots=(tmp_path,),
    )
    return ProfileController(storage, (tmp_path,), profile).persist("thread", profile)


def test_dispatch_persists_fence_before_send_and_tombstone_after_accept(tmp_path: Path) -> None:
    with RuntimeStorage(tmp_path / "db.sqlite") as storage:
        storage.initialize_runtime(sink_mode="control")
        profile_hash = prepare(storage, tmp_path)
        profile = ProfileController(
            storage,
            (tmp_path,),
            ExecutionProfile(SandboxType.READ_ONLY, tmp_path, network_access=False),
        ).load("thread")
        controller = CodexController(
            storage,
            FakeConnection(),
            schema_root=ROOT / "generated/codex/0.148.0/experimental",
            server_epoch="server",
            connection_epoch="connection",
        )
        result = controller.dispatch(
            ingress_message_id="message-1",
            thread_id="thread",
            text="work",
            profile=profile,
            profile_hash=profile_hash,
        )
        assert result.state == "accepted" and result.turn_id is None
        assert result.queued_submission_id == "queued-1"
        assert storage.connection.execute(
            "SELECT queued_submission_id FROM dispatch_records "
            "WHERE ingress_message_id='message-1'"
        ).fetchone()[0] == "queued-1"
        assert storage.connection.execute(
            "SELECT COUNT(*) FROM executed_command_tombstones WHERE tombstone_key='message-1'"
        ).fetchone()[0] == 1


def test_dispatch_write_failure_becomes_unknown_and_is_not_retried(tmp_path: Path) -> None:
    with RuntimeStorage(tmp_path / "db.sqlite") as storage:
        storage.initialize_runtime(sink_mode="control")
        profile_hash = prepare(storage, tmp_path)
        profile = ProfileController(
            storage,
            (tmp_path,),
            ExecutionProfile(SandboxType.READ_ONLY, tmp_path, network_access=False),
        ).load("thread")
        connection = FakeConnection(fail_queue=True)
        controller = CodexController(
            storage,
            connection,
            schema_root=ROOT / "generated/codex/0.148.0/experimental",
            server_epoch="server",
            connection_epoch="connection",
        )
        first = controller.dispatch(
            ingress_message_id="message-1",
            thread_id="thread",
            text="work",
            profile=profile,
            profile_hash=profile_hash,
        )
        second = controller.dispatch(
            ingress_message_id="message-1",
            thread_id="thread",
            text="work",
            profile=profile,
            profile_hash=profile_hash,
        )
        assert first.state == second.state == "outcome_unknown"
        assert [method for method, _ in connection.calls].count("thread/queue/add") == 1


def test_dispatch_timeout_is_reconciled_from_durable_queue(tmp_path: Path) -> None:
    with RuntimeStorage(tmp_path / "db.sqlite") as storage:
        storage.initialize_runtime(sink_mode="control")
        profile_hash = prepare(storage, tmp_path)
        profile = ProfileController(
            storage,
            (tmp_path,),
            ExecutionProfile(SandboxType.READ_ONLY, tmp_path, network_access=False),
        ).load("thread")
        connection = FakeConnection(fail_queue=True, queue_after_failure=True)
        controller = CodexController(
            storage,
            connection,
            schema_root=ROOT / "generated/codex/0.148.0/experimental",
            server_epoch="server",
            connection_epoch="connection",
        )
        result = controller.dispatch(
            ingress_message_id="message-timeout",
            thread_id="thread",
            text="work",
            profile=profile,
            profile_hash=profile_hash,
        )

        assert result.state == "accepted"
        assert result.queued_submission_id == "queued-after-timeout"
        row = storage.connection.execute(
            "SELECT state,queued_submission_id FROM dispatch_records"
        ).fetchone()
        assert tuple(row) == ("accepted", "queued-after-timeout")
        assert storage.connection.execute(
            "SELECT COUNT(*) FROM executed_command_tombstones"
        ).fetchone()[0] == 1


def test_dispatch_queues_into_active_thread_without_steering(tmp_path: Path) -> None:
    with RuntimeStorage(tmp_path / "db.sqlite") as storage:
        storage.initialize_runtime(sink_mode="control")
        profile_hash = prepare(storage, tmp_path)
        profile = ProfileController(
            storage,
            (tmp_path,),
            ExecutionProfile(SandboxType.READ_ONLY, tmp_path, network_access=False),
        ).load("thread")
        connection = FakeConnection(thread_status="active")
        controller = CodexController(
            storage,
            connection,
            schema_root=ROOT / "generated/codex/0.148.0/experimental",
            server_epoch="server",
            connection_epoch="connection",
        )

        result = controller.dispatch(
            ingress_message_id="message-steer",
            thread_id="thread",
            text="remote update",
            input_items=({"type": "text", "text": "remote update"},),
            profile=profile,
            profile_hash=profile_hash,
            active_turn_id="stale-rollout-turn",
        )

        assert result.state == "accepted" and result.turn_id is None
        queued = next(
            params for method, params in connection.calls if method == "thread/queue/add"
        )
        assert queued["input"] == [{"type": "text", "text": "remote update"}]
        assert not any(method == "turn/steer" for method, _ in connection.calls)
        record = storage.connection.execute(
            "SELECT state,turn_id,queued_submission_id FROM dispatch_records "
            "WHERE ingress_message_id='message-steer'"
        ).fetchone()
        assert tuple(record) == ("accepted", None, "queued-1")
        assert storage.connection.execute(
            "SELECT COUNT(*) FROM executed_command_tombstones "
            "WHERE tombstone_key='message-steer'"
        ).fetchone()[0] == 1


def test_dispatch_queue_does_not_depend_on_active_turn_count(
    tmp_path: Path,
) -> None:
    with RuntimeStorage(tmp_path / "db.sqlite") as storage:
        storage.initialize_runtime(sink_mode="control")
        profile_hash = prepare(storage, tmp_path)
        profile = ProfileController(
            storage,
            (tmp_path,),
            ExecutionProfile(SandboxType.READ_ONLY, tmp_path, network_access=False),
        ).load("thread")
        connection = FakeConnection(
            thread_status="active", active_turns=("turn-a", "turn-b")
        )
        controller = CodexController(
            storage,
            connection,
            schema_root=ROOT / "generated/codex/0.148.0/experimental",
            server_epoch="server",
            connection_epoch="connection",
        )

        result = controller.dispatch(
            ingress_message_id="message-ambiguous",
            thread_id="thread",
            text="remote update",
            profile=profile,
            profile_hash=profile_hash,
        )
        assert result.state == "accepted"
        assert result.queued_submission_id == "queued-1"


def test_profile_reconciliation_ignores_thread_default_sandbox_and_closes_breaker(
    tmp_path: Path,
) -> None:
    with RuntimeStorage(tmp_path / "db.sqlite") as storage:
        storage.initialize_runtime(sink_mode="control")
        prepare(storage, tmp_path)
        profile = ProfileController(
            storage,
            (tmp_path,),
            ExecutionProfile(SandboxType.READ_ONLY, tmp_path, network_access=False),
        ).load("thread")
        storage.connection.execute(
            "INSERT INTO circuit_breakers(breaker_name,state,reason,updated_at) "
            "VALUES('profile_reconciliation','open','thread:thread',?)",
            (utc_now(),),
        )
        controller = CodexController(
            storage,
            ProfileConnection(tmp_path),
            schema_root=ROOT / "generated/codex/0.148.0/experimental",
            server_epoch="server",
            connection_epoch="connection",
        )
        assert controller.reconcile_profile("thread", profile)
        row = storage.connection.execute(
            "SELECT state,reason FROM circuit_breakers "
            "WHERE breaker_name='profile_reconciliation'"
        ).fetchone()
        assert tuple(row) == ("closed", None)


def test_profile_reconciliation_still_fails_closed_on_resume_field_mismatch(
    tmp_path: Path,
) -> None:
    with RuntimeStorage(tmp_path / "db.sqlite") as storage:
        storage.initialize_runtime(sink_mode="control")
        prepare(storage, tmp_path)
        profile = ProfileController(
            storage,
            (tmp_path,),
            ExecutionProfile(SandboxType.READ_ONLY, tmp_path, network_access=False),
        ).load("thread")
        controller = CodexController(
            storage,
            ProfileConnection(tmp_path, mismatch=True),
            schema_root=ROOT / "generated/codex/0.148.0/experimental",
            server_epoch="server",
            connection_epoch="connection",
        )
        assert not controller.reconcile_profile("thread", profile)
        assert storage.connection.execute(
            "SELECT state FROM circuit_breakers "
            "WHERE breaker_name='profile_reconciliation'"
        ).fetchone()[0] == "open"


def test_explicit_remote_grant_allows_desktop_thread_image_input(tmp_path: Path) -> None:
    with RuntimeStorage(tmp_path / "db.sqlite") as storage:
        storage.initialize_runtime(sink_mode="control")
        profile_hash = prepare(storage, tmp_path)
        storage.connection.execute(
            "UPDATE thread_bindings SET ownership_state='desktop_mirror_only' WHERE thread_id='thread'"
        )
        capabilities = {"text": True, "image": True, "file": True, "controls": True}
        encoded = canonicalize(capabilities)
        storage.connection.execute(
            "INSERT INTO remote_task_grants(thread_id,project_root,chat_id,task_binding_epoch,"
            "identity_binding_epoch,service_fencing_token,capabilities_json,capabilities_hash,state,authorized_at,updated_at) "
            "VALUES('thread',?,'chat',1,1,1,?,?,'active',?,?)",
            (
                str(tmp_path),
                encoded.decode(),
                sha256(encoded).hexdigest(),
                utc_now(),
                utc_now(),
            ),
        )
        profile = ProfileController(
            storage,
            (tmp_path,),
            ExecutionProfile(SandboxType.READ_ONLY, tmp_path, network_access=False),
        ).load("thread")
        connection = FakeConnection()
        controller = CodexController(
            storage,
            connection,
            schema_root=ROOT / "generated/codex/0.148.0/experimental",
            server_epoch="server",
            connection_epoch="connection",
        )
        image_path = tmp_path / "image.png"
        image_path.write_bytes(b"fixture")
        controller.dispatch(
            ingress_message_id="remote-image",
            thread_id="thread",
            text="image",
            input_items=(
                {"type": "text", "text": "image"},
                {"type": "localImage", "path": str(image_path)},
            ),
            required_capability="image",
            profile=profile,
            profile_hash=profile_hash,
        )
        queue_call = next(
            params for method, params in connection.calls if method == "thread/queue/add"
        )
        assert queue_call["input"][1] == {"type": "localImage", "path": str(image_path)}

        storage.connection.execute(
            "UPDATE remote_task_grants SET service_fencing_token=0 WHERE thread_id='thread'"
        )
        try:
            controller.require_control_authorized("thread")
        except Exception as exc:
            assert "no longer matches" in str(exc)
        else:
            raise AssertionError("stale-fence remote grant was accepted")
