import base64
import json
import subprocess
from pathlib import Path

import pytest

from codex_feishu_bridge.codex.desktop_gateway import (
    CodexDesktopGateway,
    DesktopGatewayError,
)


THREAD_ID = "019fff1b-d405-79b2-9cce-d9ed2c6c2853"


def _files(tmp_path: Path) -> tuple[Path, Path]:
    script = tmp_path / "helper.ps1"
    executable = tmp_path / "powershell.exe"
    script.write_text("# fixture", encoding="ascii")
    executable.write_bytes(b"fixture")
    return script, executable


def test_gateway_encodes_text_and_attachment_without_shell_interpolation(tmp_path: Path) -> None:
    script, executable = _files(tmp_path)
    attachment = tmp_path / "图像 one.png"
    attachment.write_bytes(b"image")
    captured: list[list[str]] = []

    def runner(command, **kwargs):
        captured.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "ok": True,
                    "action": "submit",
                    "threadId": THREAD_ID,
                    "submitted": True,
                    "usedForegroundFallback": False,
                }
            ),
            "",
        )

    result = CodexDesktopGateway(
        script_path=script,
        powershell_executable=executable,
        runner=runner,
    ).submit(THREAD_ID, "中文 $() ` text", attachments=(attachment,))
    assert result.submitted and not result.used_foreground_fallback
    command = captured[0]
    text_value = command[command.index("-TextBase64") + 1]
    assert base64.b64decode(text_value).decode("utf-8") == "中文 $() ` text"
    attachment_value = command[command.index("-AttachmentsBase64") + 1]
    assert json.loads(base64.b64decode(attachment_value).decode("utf-8")) == [
        str(attachment.resolve())
    ]
    assert "中文 $() ` text" not in command


def test_gateway_rejects_noncanonical_thread_and_helper_failure(tmp_path: Path) -> None:
    script, executable = _files(tmp_path)
    gateway = CodexDesktopGateway(
        script_path=script,
        powershell_executable=executable,
        runner=lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            1,
            json.dumps({"ok": False, "error": "composer unavailable"}),
            "",
        ),
    )
    with pytest.raises(DesktopGatewayError, match="thread id"):
        gateway.submit("not-a-thread", "text")
    with pytest.raises(DesktopGatewayError, match="composer unavailable"):
        gateway.submit(THREAD_ID, "text")


def test_desktop_helper_uses_focused_enter_after_send_readiness() -> None:
    script = (
        Path(__file__).resolve().parents[1]
        / "codex_feishu_bridge"
        / "windows"
        / "codex_desktop_input.ps1"
    ).read_text(encoding="utf-8")

    assert "$send.accDoDefaultAction" not in script
    assert "Test-AccessibleActionable $candidate" in script
    assert "Clipboard]::SetText" in script
    assert "[CodexDesktopNative]::SendPaste()" in script
    assert "Set-AccessibleValue $composer $text" not in script
    assert "[CodexDesktopNative]::SendEnter()" in script
