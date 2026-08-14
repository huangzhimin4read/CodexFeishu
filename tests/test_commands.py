import subprocess
import sys
from pathlib import Path

from codex_feishu_bridge.commands import ControlCommand, parse_command


def test_full_text_command_grammar_accepts_only_canonical_plain_text() -> None:
    assert parse_command("text", "/tasks") == ControlCommand("tasks")
    assert parse_command("text", "/use task-1") == ControlCommand("use", "task-1")
    assert parse_command("text", "/network off") == ControlCommand("network", "off")
    assert parse_command("text", "/cwd D:\\Work Root") == ControlCommand("cwd", "D:\\Work Root")
    for value in (
        " /tasks",
        "/tasks ",
        "/tasks\n/stop",
        "`/stop`",
        "/use task one",
        "/network maybe",
        "/cwd %USERPROFILE%",
        "/tasks\u200b",
    ):
        assert parse_command("text", value) is None
    assert parse_command("post", "/stop") is None


def test_cli_help_does_not_load_runtime_transports() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "codex_feishu_bridge", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0
    assert "verify-config" in completed.stdout
    assert "pkg_resources" not in completed.stderr


def test_cli_verifies_shipped_offline_example() -> None:
    root = Path(__file__).parents[1]
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "codex_feishu_bridge",
            "verify-config",
            "--config",
            str(root / "config" / "offline.example.toml"),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0
    assert completed.stdout.strip() == '{"valid": true, "mode": "offline"}'
