import json
import subprocess
from pathlib import Path

import pytest

from codex_feishu_bridge.feishu.client import ProviderOutcome
from codex_feishu_bridge.feishu.user_cli import LarkCliUserSender
from codex_feishu_bridge.feishu import user_cli


def _executable(tmp_path: Path) -> Path:
    path = tmp_path / "lark-cli.cmd"
    path.write_text("@echo off\n", encoding="utf-8")
    return path


def test_lark_cli_user_sender_uses_user_identity_and_thread_reply(tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict]] = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "identity": "user",
                    "data": {"message_id": "om_reply", "chat_id": "oc_chat"},
                },
                indent=2,
            ),
            stderr="",
        )

    sender = LarkCliUserSender(
        _executable(tmp_path),
        profile="profile-one",
        runner=runner,
    )
    result = sender.reply_text(
        message_id="om_anchor",
        text="用户发言",
        reply_in_thread=True,
        idempotency_key="stable-key",
    )

    assert result.outcome is ProviderOutcome.CONFIRMED
    assert result.message_id == "om_reply"
    command = calls[0][0]
    assert command[1:5] == ["--profile", "profile-one", "im", "+messages-reply"]
    assert command[command.index("--as") + 1] == "user"
    assert command[command.index("--text") + 1] == "用户发言"
    assert "--reply-in-thread" in command
    assert calls[0][1]["env"]["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] == "1"


def test_lark_cli_discovery_bypasses_windows_npm_cmd_shim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    npm_root = tmp_path / "npm"
    shim = npm_root / "lark-cli.cmd"
    script = npm_root / "node_modules" / "@larksuite" / "cli" / "scripts" / "run.js"
    node = tmp_path / "node.exe"
    script.parent.mkdir(parents=True)
    shim.write_text("@echo off\n", encoding="utf-8")
    script.write_text("// official entry\n", encoding="utf-8")
    node.write_bytes(b"node")

    commands = {
        "lark-cli.cmd": str(shim),
        "lark-cli": None,
        "node.exe": str(node),
        "node": None,
    }
    monkeypatch.setattr(user_cli.shutil, "which", lambda name: commands.get(name))

    sender = LarkCliUserSender.discover(profile="profile-one")

    assert sender.executable == node.resolve()
    assert sender.launcher_arguments == (str(script.resolve()),)
    assert sender._command("--profile", "profile-one") == [
        str(node.resolve()),
        str(script.resolve()),
        "--profile",
        "profile-one",
    ]


def test_lark_cli_discovery_rejects_cmd_shim_without_node_entrypoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shim = tmp_path / "lark-cli.cmd"
    shim.write_text("@echo off\n", encoding="utf-8")
    commands = {
        "lark-cli.cmd": str(shim),
        "lark-cli": None,
        "node.exe": None,
        "node": None,
    }
    monkeypatch.setattr(user_cli.shutil, "which", lambda name: commands.get(name))

    with pytest.raises(RuntimeError, match="Node entrypoint is unavailable"):
        LarkCliUserSender.discover(profile="profile-one")


def test_lark_cli_user_sender_verifies_ready_identity(tmp_path: Path) -> None:
    def runner(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "verified": True,
                    "identities": {
                        "user": {
                            "status": "ready",
                            "available": True,
                            "verified": True,
                            "tokenStatus": "valid",
                            "openId": "ou_owner",
                            "userName": "项目所有者",
                        }
                    },
                },
                ensure_ascii=False,
            ),
            stderr="",
        )

    sender = LarkCliUserSender(
        _executable(tmp_path),
        profile="profile-one",
        runner=runner,
    )

    assert sender.verify_ready() == "项目所有者"


def test_lark_cli_user_sender_accepts_any_ready_authorized_user(tmp_path: Path) -> None:
    def runner(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "verified": True,
                    "identities": {
                        "user": {
                            "status": "ready",
                            "available": True,
                            "verified": True,
                            "tokenStatus": "valid",
                            "openId": "ou_other",
                        }
                    },
                }
            ),
            stderr="",
        )

    sender = LarkCliUserSender(
        _executable(tmp_path),
        profile="profile-one",
        runner=runner,
    )

    assert sender.verify_ready() == ""


def test_lark_cli_user_sender_reports_expired_authorization_as_permanent(
    tmp_path: Path,
) -> None:
    def runner(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr=json.dumps(
                {
                    "ok": False,
                    "identity": "user",
                    "error": {
                        "type": "authorization",
                        "subtype": "token_expired",
                    },
                },
                indent=2,
            ),
        )

    sender = LarkCliUserSender(
        _executable(tmp_path),
        profile="profile-one",
        runner=runner,
    )
    result = sender.reply_text(
        message_id="om_anchor",
        text="用户发言",
        reply_in_thread=False,
        idempotency_key="stable-key",
    )

    assert result.outcome is ProviderOutcome.PERMANENT
    assert result.code == "user_cli_token_expired"


def test_lark_cli_user_sender_treats_timeout_as_unknown(tmp_path: Path) -> None:
    def runner(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    sender = LarkCliUserSender(
        _executable(tmp_path),
        profile="profile-one",
        runner=runner,
    )
    result = sender.reply_text(
        message_id="om_anchor",
        text="用户发言",
        reply_in_thread=False,
        idempotency_key="stable-key",
    )

    assert result.outcome is ProviderOutcome.UNKNOWN
    assert result.code == "user_cli_timeout"
