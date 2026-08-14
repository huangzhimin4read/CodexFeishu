import io
import json
from pathlib import Path

import pytest

from codex_feishu_bridge.codex.cli_dispatch import (
    CodexCliDispatcher,
    matches_cli_submission,
)
from codex_feishu_bridge.codex.cli_gateway import (
    CodexCliActiveWriter,
    CodexCliGateway,
    CodexCliGatewayResult,
)
from codex_feishu_bridge.codex.controller import DispatchBusy
from codex_feishu_bridge.codex.desktop_dispatch import desktop_submission_text_hash
from codex_feishu_bridge.runtime_storage import RuntimeStorage, utc_now


THREAD_ID = "019fff1b-d405-79b2-9cce-d9ed2c6c2853"
TURN_ID = "019fff28-c672-75a0-b5da-5b4ceee1b5b9"


class RecordingGateway:
    def __init__(
        self,
        rollout: Path,
        *,
        append_context: bool = True,
        item_id: str | None = "cli-user-item",
    ) -> None:
        self.rollout = rollout
        self.append_context = append_context
        self.item_id = item_id
        self.calls = []

    def submit(self, thread_id, text, *, image_paths=(), cwd):
        self.calls.append((thread_id, text, tuple(image_paths), cwd))
        records = [
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                    "internal_chat_message_metadata_passthrough": {"turn_id": TURN_ID},
                },
            },
            {
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": TURN_ID},
            },
        ]
        if self.append_context:
            records.extend(
                [
                    {"type": "turn_context", "payload": {"turn_id": TURN_ID}},
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "id": self.item_id,
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": (
                                        '<image name=[Image #1] path="C:\\safe\\photo.png">'
                                        "</image>" + text
                                        if image_paths
                                        else text
                                    ),
                                }
                            ],
                            "internal_chat_message_metadata_passthrough": {
                                "turn_id": TURN_ID
                            },
                        },
                    },
                ]
            )
        with self.rollout.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return CodexCliGatewayResult(thread_id)


def _dispatcher(
    storage: RuntimeStorage,
    gateway: RecordingGateway,
    codex_home: Path,
    *,
    confirmation_seconds: float = 1.0,
) -> CodexCliDispatcher:
    return CodexCliDispatcher(
        storage,
        gateway,
        codex_home=codex_home,
        authorize=lambda thread_id, **kwargs: (2, 3, 4),
        server_epoch="cli-server",
        connection_epoch="cli-connection",
        rollout_confirmation_seconds=confirmation_seconds,
    )


def _task_binding(storage: RuntimeStorage, project_root: Path) -> None:
    storage.connection.execute(
        "INSERT INTO task_bindings(thread_id,project_root,chat_id,anchor_message_id,anchor_state,"
        "anchor_uuid,anchor_marker,opted_in,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (THREAD_ID, str(project_root), "chat", "anchor", "confirmed", "uuid", "marker", 1, utc_now()),
    )


def test_cli_dispatch_is_accepted_only_after_new_contextualized_user_turn(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    session_dir = codex_home / "sessions" / "2026" / "08" / "14"
    session_dir.mkdir(parents=True)
    rollout = session_dir / f"rollout-test-{THREAD_ID}.jsonl"
    rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")
    image = tmp_path / "photo.png"
    image.write_bytes(b"image")
    gateway = RecordingGateway(rollout)
    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="control")
        _task_binding(storage, tmp_path)
        result = _dispatcher(storage, gateway, codex_home).dispatch(
            ingress_message_id="feishu-cli-image",
            thread_id=THREAD_ID,
            text="飞书图片输入",
            required_capability="image",
            image_paths=(image,),
        )
        assert result.state == "accepted" and result.turn_id == TURN_ID
        record = storage.connection.execute(
            "SELECT state,turn_id,user_item_id,request_id,profile_hash FROM dispatch_records"
        ).fetchone()
        assert tuple(record) == (
            "accepted",
            TURN_ID,
            "cli-user-item",
            "codex-cli-started",
            "cli-host-managed",
        )
        assert storage.connection.execute(
            "SELECT COUNT(*) FROM executed_command_tombstones"
        ).fetchone()[0] == 1
    assert gateway.calls == [(THREAD_ID, "飞书图片输入", (image.resolve(),), tmp_path.resolve())]


def test_cli_dispatch_does_not_accept_replay_user_record_before_turn_context(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    session_dir = codex_home / "sessions"
    session_dir.mkdir(parents=True)
    rollout = session_dir / f"rollout-test-{THREAD_ID}.jsonl"
    rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")
    gateway = RecordingGateway(rollout, append_context=False)
    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="control")
        _task_binding(storage, tmp_path)
        result = _dispatcher(
            storage, gateway, codex_home, confirmation_seconds=0.01
        ).dispatch(
            ingress_message_id="replay-only",
            thread_id=THREAD_ID,
            text="历史上下文中的相同文本",
            required_capability="text",
        )
        assert result.state == "submitted_unconfirmed" and result.turn_id is None
        assert storage.connection.execute(
            "SELECT state FROM dispatch_records"
        ).fetchone()[0] == "outcome_unknown"


def test_cli_dispatch_requires_stable_user_item_identity(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    session_dir = codex_home / "sessions"
    session_dir.mkdir(parents=True)
    rollout = session_dir / f"rollout-test-{THREAD_ID}.jsonl"
    rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")
    gateway = RecordingGateway(rollout, item_id=None)
    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="control")
        _task_binding(storage, tmp_path)
        result = _dispatcher(
            storage, gateway, codex_home, confirmation_seconds=0.01
        ).dispatch(
            ingress_message_id="idless-user",
            thread_id=THREAD_ID,
            text="没有稳定身份",
            required_capability="text",
        )
        assert result.state == "submitted_unconfirmed" and result.turn_id is None
        record = storage.connection.execute(
            "SELECT state,turn_id,user_item_id FROM dispatch_records"
        ).fetchone()
        assert tuple(record) == ("outcome_unknown", None, None)


def _abandoned_prestart(storage: RuntimeStorage, *, text: str) -> None:
    storage.connection.execute(
        "INSERT INTO ingress_messages(tenant_key,app_id,message_id,chat_id,sender_open_id,chat_type,"
        "message_type,content_hash,raw_hash,received_at,routing_state,target_thread_id) "
        "VALUES('tenant','app','abandoned','chat','owner','group','text','content','raw',"
        "'2000-01-01T00:00:00Z','routed_current',?)",
        (THREAD_ID,),
    )
    storage.connection.execute(
        "INSERT INTO dispatch_attempts(dispatch_attempt_id,state,updated_at) "
        "VALUES('abandoned-attempt','dispatching','2000-01-01T00:00:00Z')"
    )
    storage.connection.execute(
        "INSERT INTO dispatch_records(dispatch_attempt_id,ingress_message_id,thread_id,"
        "client_user_message_id,profile_hash,binding_epoch,identity_binding_epoch,fencing_token,"
        "server_epoch,connection_epoch,request_hash,request_id,submitted_text_hash,has_attachments,"
        "state,created_at,updated_at) VALUES('abandoned-attempt','abandoned',?,'client','cli',1,1,1,"
        "'server','connection','request','codex-cli',?,0,'bytes_sending',"
        "'2000-01-01T00:00:00Z','2000-01-01T00:00:00Z')",
        (THREAD_ID, desktop_submission_text_hash(text)),
    )


def test_abandoned_cli_prestart_without_rollout_is_released_for_retry(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    (codex_home / "sessions").mkdir(parents=True)
    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="control")
        _task_binding(storage, tmp_path)
        _abandoned_prestart(storage, text="未落盘")
        result = _dispatcher(
            storage, RecordingGateway(codex_home / "unused.jsonl"), codex_home
        ).recover_abandoned_prestarts(older_than_seconds=0)
        assert result == (0, 1)
        assert storage.connection.execute(
            "SELECT COUNT(*) FROM dispatch_records"
        ).fetchone()[0] == 0
        ingress = storage.connection.execute(
            "SELECT dispatch_not_before,last_dispatch_error FROM ingress_messages "
            "WHERE message_id='abandoned'"
        ).fetchone()
        assert tuple(ingress) == (None, "cli_prestart_recovered")


def test_abandoned_cli_prestart_claims_exact_persisted_user_item(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    session_dir = codex_home / "sessions"
    session_dir.mkdir(parents=True)
    rollout = session_dir / f"rollout-test-{THREAD_ID}.jsonl"
    records = [
        {
            "timestamp": "2000-01-01T00:00:01Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": TURN_ID},
        },
        {
            "timestamp": "2000-01-01T00:00:02Z",
            "type": "turn_context",
            "payload": {"turn_id": TURN_ID},
        },
        {
            "timestamp": "2000-01-01T00:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "id": "recovered-item",
                "content": [{"type": "input_text", "text": "已落盘"}],
                "internal_chat_message_metadata_passthrough": {"turn_id": TURN_ID},
            },
        },
    ]
    rollout.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="control")
        _task_binding(storage, tmp_path)
        _abandoned_prestart(storage, text="已落盘")
        result = _dispatcher(
            storage, RecordingGateway(rollout), codex_home
        ).recover_abandoned_prestarts(older_than_seconds=0)
        assert result == (1, 0)
        record = storage.connection.execute(
            "SELECT state,request_id,turn_id,user_item_id FROM dispatch_records"
        ).fetchone()
        assert tuple(record) == (
            "accepted",
            "codex-cli-started",
            TURN_ID,
            "recovered-item",
        )


def test_cli_active_writer_failure_rolls_back_only_prepared_attempt(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    (codex_home / "sessions").mkdir(parents=True)

    class BusyGateway:
        @staticmethod
        def submit(*args, **kwargs):
            raise CodexCliActiveWriter("active writer")

    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="control")
        _task_binding(storage, tmp_path)
        dispatcher = CodexCliDispatcher(
            storage,
            BusyGateway(),
            codex_home=codex_home,
            authorize=lambda thread_id, **kwargs: (2, 3, 4),
            server_epoch="cli-server",
            connection_epoch="cli-connection",
        )
        with pytest.raises(DispatchBusy, match="active writer"):
            dispatcher.dispatch(
                ingress_message_id="busy",
                thread_id=THREAD_ID,
                text="立刻追加",
                required_capability="text",
            )
        assert storage.connection.execute(
            "SELECT COUNT(*) FROM dispatch_records"
        ).fetchone()[0] == 0
        assert storage.connection.execute(
            "SELECT COUNT(*) FROM dispatch_attempts"
        ).fetchone()[0] == 0


def test_cli_image_match_requires_a_structured_leading_image_wrapper() -> None:
    expected = desktop_submission_text_hash("图片说明")
    assert matches_cli_submission(
        '<image name=[Image #1] path="C:\\safe\\photo.jpg"></image>图片说明',
        expected,
        has_images=True,
    )
    assert not matches_cli_submission(
        "unrelated 图片说明",
        expected,
        has_images=True,
    )


class _NonClosingInput(io.StringIO):
    def close(self) -> None:
        self.flush()


class _FakeProcess:
    def __init__(self, stdout: str, stderr: str = "", returncode: int = 0) -> None:
        self.stdin = _NonClosingInput()
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)
        self.returncode = returncode

    def poll(self) -> int:
        return self.returncode


def test_cli_gateway_uses_argv_stdin_exact_task_and_images(tmp_path: Path) -> None:
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"fixture")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    image = tmp_path / "image with spaces.png"
    image.write_bytes(b"image")
    captured = {}
    process = _FakeProcess(
        json.dumps({"type": "thread.started", "thread_id": THREAD_ID})
        + "\n"
        + json.dumps({"type": "turn.started"})
        + "\n"
    )

    def factory(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return process

    result = CodexCliGateway(
        executable,
        codex_home,
        start_timeout_seconds=1,
        popen_factory=factory,
    ).submit(
        THREAD_ID,
        '带引号 " 和 ; 的输入',
        image_paths=(image,),
        cwd=tmp_path,
    )

    assert result.thread_id == THREAD_ID
    assert captured["command"] == [
        str(executable.resolve()),
        "exec",
        "resume",
        "--json",
        "--skip-git-repo-check",
        "-i",
        str(image.resolve()),
        THREAD_ID,
        "-",
    ]
    assert process.stdin.getvalue() == '带引号 " 和 ; 的输入'
    assert captured["kwargs"].get("shell", False) is False
    assert captured["kwargs"]["env"]["CODEX_HOME"] == str(codex_home.resolve())


def test_cli_gateway_classifies_definitive_active_writer_conflict(tmp_path: Path) -> None:
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"fixture")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    process = _FakeProcess(
        "",
        "thread-store conflict: thread already has an active writer\n",
        returncode=1,
    )

    with pytest.raises(CodexCliActiveWriter):
        CodexCliGateway(
            executable,
            codex_home,
            start_timeout_seconds=1,
            popen_factory=lambda *args, **kwargs: process,
        ).submit(THREAD_ID, "输入", cwd=tmp_path)
