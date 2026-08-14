import json
from hashlib import sha256
from pathlib import Path

from codex_feishu_bridge.codex.desktop_dispatch import DesktopCodexDispatcher
from codex_feishu_bridge.codex.desktop_gateway import DesktopGatewayResult
from codex_feishu_bridge.runtime_storage import RuntimeStorage


THREAD_ID = "019fff1b-d405-79b2-9cce-d9ed2c6c2853"
TURN_ID = "019fff28-c672-75a0-b5da-5b4ceee1b5b9"


class RecordingGateway:
    def __init__(self, rollout: Path, *, append: bool = True, wrap_attachments: bool = False) -> None:
        self.rollout = rollout
        self.append = append
        self.wrap_attachments = wrap_attachments
        self.calls = []

    def submit(self, thread_id, text, *, attachments=()):
        self.calls.append((thread_id, text, tuple(attachments)))
        if self.append:
            recorded_text = text
            if attachments and self.wrap_attachments:
                recorded_text = (
                    "\n# Files mentioned by the user:\n\n"
                    "## attachment.png: C:/tmp/attachment.png\n\n"
                    "## My request:\n"
                    f"{text}\n"
                )
            record = {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "id": "user-item",
                    "content": [{"type": "input_text", "text": recorded_text}],
                    "internal_chat_message_metadata_passthrough": {"turn_id": TURN_ID},
                },
            }
            with self.rollout.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return DesktopGatewayResult("submit", thread_id, True, False)


def _dispatcher(
    storage: RuntimeStorage,
    gateway: RecordingGateway,
    codex_home: Path,
    *,
    confirmation_seconds: float = 1.0,
) -> DesktopCodexDispatcher:
    return DesktopCodexDispatcher(
        storage,
        gateway,
        codex_home=codex_home,
        authorize=lambda thread_id, **kwargs: (2, 3, 4),
        server_epoch="desktop-server",
        connection_epoch="desktop-connection",
        rollout_confirmation_seconds=confirmation_seconds,
    )


def test_desktop_dispatch_is_accepted_only_after_new_rollout_user_turn(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    session_dir = codex_home / "sessions" / "2026" / "08" / "14"
    session_dir.mkdir(parents=True)
    rollout = session_dir / f"rollout-test-{THREAD_ID}.jsonl"
    rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")
    attachment = tmp_path / "attachment.png"
    attachment.write_bytes(b"image")
    gateway = RecordingGateway(rollout, wrap_attachments=True)
    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="control")
        result = _dispatcher(storage, gateway, codex_home).dispatch(
            ingress_message_id="feishu-message",
            thread_id=THREAD_ID,
            text="桌面输入",
            required_capability="images",
            attachment_paths=(attachment,),
        )
        assert result.state == "accepted" and result.turn_id == TURN_ID
        record = storage.connection.execute(
            "SELECT state,turn_id,user_item_id,request_id,profile_hash FROM dispatch_records"
        ).fetchone()
        assert tuple(record) == (
            "accepted",
            TURN_ID,
            "user-item",
            "desktop-ui-submitted",
            "desktop-host-managed",
        )
        assert storage.connection.execute(
            "SELECT COUNT(*) FROM executed_command_tombstones"
        ).fetchone()[0] == 1
    assert gateway.calls == [(THREAD_ID, "桌面输入", (attachment.resolve(),))]


def test_attachment_confirmation_rejects_unstructured_substring() -> None:
    assert not DesktopCodexDispatcher._matches_submitted_text(
        "unrelated 桌面输入 suffix",
        "桌面输入",
        has_attachments=True,
    )


def test_plain_confirmation_allows_one_codex_terminal_newline() -> None:
    assert DesktopCodexDispatcher._matches_submitted_text(
        "桌面输入\n",
        "桌面输入",
        has_attachments=False,
    )


def test_plain_confirmation_rejects_extra_blank_line_or_body_change() -> None:
    assert not DesktopCodexDispatcher._matches_submitted_text(
        "桌面输入\n\n",
        "桌面输入",
        has_attachments=False,
    )
    assert not DesktopCodexDispatcher._matches_submitted_text(
        "桌面 输入\n",
        "桌面输入",
        has_attachments=False,
    )


def test_desktop_dispatch_reports_ui_submission_while_rollout_confirmation_is_pending(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    session_dir = codex_home / "sessions" / "2026" / "08" / "14"
    session_dir.mkdir(parents=True)
    rollout = session_dir / f"rollout-test-{THREAD_ID}.jsonl"
    rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")
    gateway = RecordingGateway(rollout, append=False)
    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="control")
        result = _dispatcher(
            storage,
            gateway,
            codex_home,
            confirmation_seconds=0.01,
        ).dispatch(
            ingress_message_id="unconfirmed",
            thread_id=THREAD_ID,
            text="not recorded",
            required_capability="text",
        )
        assert result.state == "submitted_unconfirmed" and result.turn_id is None
        record = storage.connection.execute(
            "SELECT state,request_id,submitted_text_hash,has_attachments FROM dispatch_records"
        ).fetchone()
        assert tuple(record) == (
            "outcome_unknown",
            "desktop-ui-submitted",
            sha256(b"not recorded").hexdigest(),
            0,
        )
        assert storage.connection.execute(
            "SELECT COUNT(*) FROM executed_command_tombstones"
        ).fetchone()[0] == 0
