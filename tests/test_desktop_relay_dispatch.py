import html
import json
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_feishu_bridge.codex.controller import DispatchBusy
from codex_feishu_bridge.codex.desktop_gateway import DesktopGatewayError
from codex_feishu_bridge.codex.desktop_gateway import DesktopGatewayResult
from codex_feishu_bridge.codex.desktop_relay_dispatch import (
    DesktopRelayCodexDispatcher,
    matches_desktop_relay_submission,
    relay_target_text,
)
from codex_feishu_bridge.runtime_storage import RuntimeStorage
from codex_feishu_bridge.service import BridgeService


TARGET_THREAD_ID = "019fff1b-d405-79b2-9cce-d9ed2c6c2853"
RELAY_THREAD_ID = "01a00efd-3472-70c2-8a71-fb86b7d8a85c"
TURN_ID = "019fff28-c672-75a0-b5da-5b4ceee1b5b9"


class RecordingRelayGateway:
    def __init__(self, rollout: Path, relay_rollout: Path, target_text: str) -> None:
        self.rollout = rollout
        self.relay_rollout = relay_rollout
        self.target_text = target_text
        self.calls = []

    def submit(self, thread_id, text, *, attachments=()):
        self.calls.append((thread_id, text, tuple(attachments)))
        relay_record = {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "id": "relay-user-item",
                "content": [{"type": "input_text", "text": text + "\n"}],
                "internal_chat_message_metadata_passthrough": {
                    "turn_id": "relay-turn"
                },
            },
        }
        with self.relay_rollout.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(relay_record, ensure_ascii=False) + "\n")
        delegated = (
            "<codex_delegation>\n"
            f"  <source_thread_id>{RELAY_THREAD_ID}</source_thread_id>\n"
            f"  <input>{html.escape(self.target_text, quote=False)}</input>\n"
            "</codex_delegation>"
        )
        record = {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "id": "delegated-user-item",
                "content": [{"type": "input_text", "text": delegated}],
                "internal_chat_message_metadata_passthrough": {"turn_id": TURN_ID},
            },
        }
        with self.rollout.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return DesktopGatewayResult("submit", thread_id, True, False)


def test_desktop_relay_accepts_only_exact_delegated_target_item(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    session_dir = codex_home / "sessions" / "2026" / "08" / "17"
    session_dir.mkdir(parents=True)
    rollout = session_dir / f"rollout-test-{TARGET_THREAD_ID}.jsonl"
    rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")
    relay_rollout = session_dir / f"rollout-test-{RELAY_THREAD_ID}.jsonl"
    relay_rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")
    target_text = relay_target_text("来自飞书的文字")
    gateway = RecordingRelayGateway(rollout, relay_rollout, target_text)

    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="control")
        dispatcher = DesktopRelayCodexDispatcher(
            storage,
            gateway,
            codex_home=codex_home,
            authorize=lambda thread_id, **kwargs: (2, 3, 4),
            server_epoch="desktop-server",
            connection_epoch="desktop-connection",
            relay_thread_id=RELAY_THREAD_ID,
            rollout_confirmation_seconds=1.0,
        )
        result = dispatcher.dispatch(
            ingress_message_id="feishu-relay-message",
            thread_id=TARGET_THREAD_ID,
            text="来自飞书的文字",
            required_capability="text",
        )

        assert result.state == "accepted" and result.turn_id == TURN_ID
        record = storage.connection.execute(
            "SELECT state,thread_id,turn_id,user_item_id,request_id,profile_hash,"
            "submitted_text_hash FROM dispatch_records"
        ).fetchone()
        assert tuple(record) == (
            "accepted",
            TARGET_THREAD_ID,
            TURN_ID,
            "delegated-user-item",
            "desktop-relay-submitted",
            "desktop-relay:" + RELAY_THREAD_ID,
            sha256(target_text.encode("utf-8")).hexdigest(),
        )
        assert storage.connection.execute(
            "SELECT COUNT(*) FROM executed_command_tombstones"
        ).fetchone()[0] == 1

    assert len(gateway.calls) == 1
    assert gateway.calls[0][0] == RELAY_THREAD_ID
    assert TARGET_THREAD_ID in gateway.calls[0][1]
    assert "来自飞书的文字" in gateway.calls[0][1]
    assert gateway.calls[0][2] == ()


def test_relay_match_requires_configured_source_and_exact_input() -> None:
    target_text = relay_target_text("A < B & C")
    expected_hash = sha256(target_text.encode("utf-8")).hexdigest()
    wrapped = (
        "<codex_delegation>\n"
        f"<source_thread_id>{RELAY_THREAD_ID}</source_thread_id>\n"
        f"<input>{html.escape(target_text, quote=False)}</input>\n"
        "</codex_delegation>"
    )
    assert matches_desktop_relay_submission(
        wrapped,
        expected_hash,
        relay_thread_id=RELAY_THREAD_ID,
    )
    assert not matches_desktop_relay_submission(
        wrapped.replace(RELAY_THREAD_ID, TARGET_THREAD_ID),
        expected_hash,
        relay_thread_id=RELAY_THREAD_ID,
    )
    assert not matches_desktop_relay_submission(
        wrapped.replace("A &lt; B", "A &lt;= B"),
        expected_hash,
        relay_thread_id=RELAY_THREAD_ID,
    )


def test_relay_background_rejection_releases_attempt_for_durable_retry(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()

    class MissingBackgroundWindowGateway:
        @staticmethod
        def submit(*args, **kwargs):
            raise DesktopGatewayError("background relay window unavailable")

    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="control")
        dispatcher = DesktopRelayCodexDispatcher(
            storage,
            MissingBackgroundWindowGateway(),
            codex_home=codex_home,
            authorize=lambda thread_id, **kwargs: (2, 3, 4),
            server_epoch="desktop-server",
            connection_epoch="desktop-connection",
            relay_thread_id=RELAY_THREAD_ID,
            rollout_confirmation_seconds=0.01,
        )
        with pytest.raises(DispatchBusy, match="no confirmed Codex user item"):
            dispatcher.dispatch(
                ingress_message_id="feishu-background-missing",
                thread_id=TARGET_THREAD_ID,
                text="稍后重试",
                required_capability="text",
            )
        assert storage.connection.execute(
            "SELECT COUNT(*) FROM dispatch_records"
        ).fetchone()[0] == 0
        assert storage.connection.execute(
            "SELECT COUNT(*) FROM dispatch_attempts"
        ).fetchone()[0] == 0


def test_relay_target_text_exposes_materialized_attachment_to_target(tmp_path: Path) -> None:
    attachment = tmp_path / "用户图片.png"
    attachment.write_bytes(b"image")
    result = relay_target_text(
        "请查看图片",
        attachment_paths=(attachment,),
        attachment_kind="images",
    )
    assert "请查看图片" in result
    assert "图片" in result
    assert str(attachment.resolve()) in result


def test_internal_relay_task_is_retired_before_provider_delivery(tmp_path: Path) -> None:
    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="control")
        storage.connection.execute(
            "INSERT INTO task_bindings(thread_id,project_root,chat_id,anchor_state,"
            "opted_in,updated_at) VALUES(?,?,?,'confirmed',1,'now')",
            (RELAY_THREAD_ID, str(tmp_path), "chat"),
        )
        storage.connection.execute(
            "INSERT INTO provider_outbox(logical_message_id,thread_id,operation,endpoint_name,"
            "stable_uuid,marker,body_json,body_hash,priority,state,next_attempt_at,created_at,updated_at) "
            "VALUES('relay-output',?,'commentary','reply_message','uuid','marker','{}','hash',"
            "10,'pending','now','now','now')",
            (RELAY_THREAD_ID,),
        )
        service = object.__new__(BridgeService)
        service.config = SimpleNamespace(
            internal_thread_ids=frozenset({RELAY_THREAD_ID})
        )
        service.storage = storage
        service._suppress_internal_task_bindings()

        binding = storage.connection.execute(
            "SELECT opted_in,lifecycle_state FROM task_bindings WHERE thread_id=?",
            (RELAY_THREAD_ID,),
        ).fetchone()
        outbox = storage.connection.execute(
            "SELECT state,last_error_code FROM provider_outbox WHERE thread_id=?",
            (RELAY_THREAD_ID,),
        ).fetchone()
        assert tuple(binding) == (0, "archived")
        assert tuple(outbox) == ("permanent", "internal_thread_suppressed")
