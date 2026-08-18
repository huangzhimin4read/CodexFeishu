"""Windows Codex desktop input through the app's accessibility surface."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


class DesktopGatewayError(RuntimeError):
    """The desktop app did not accept a requested accessibility operation."""


@dataclass(frozen=True, slots=True)
class DesktopGatewayResult:
    action: str
    thread_id: str
    submitted: bool
    used_foreground_fallback: bool


class CodexDesktopGateway:
    """Send input through the desktop-owned writer instead of a second App Server."""

    def __init__(
        self,
        *,
        script_path: Path | None = None,
        powershell_executable: Path | None = None,
        background_only: bool = False,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.script_path = (
            script_path
            or Path(__file__).resolve().parents[1]
            / "windows"
            / "codex_desktop_input.ps1"
        ).resolve()
        if powershell_executable is None:
            system_root = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
            powershell_executable = (
                system_root
                / "System32"
                / "WindowsPowerShell"
                / "v1.0"
                / "powershell.exe"
            )
        self.powershell_executable = powershell_executable.resolve()
        self.background_only = background_only
        self.runner = runner
        if not self.script_path.is_file():
            raise DesktopGatewayError("Codex desktop input helper is missing")
        if not self.powershell_executable.is_file():
            raise DesktopGatewayError("Windows PowerShell executable is missing")

    @staticmethod
    def _utf8_base64(value: str) -> str:
        return base64.b64encode(value.encode("utf-8")).decode("ascii")

    @staticmethod
    def _validate_thread_id(thread_id: str) -> str:
        try:
            parsed = uuid.UUID(thread_id)
        except (ValueError, AttributeError) as exc:
            raise DesktopGatewayError("invalid Codex thread id") from exc
        if str(parsed) != thread_id.casefold():
            raise DesktopGatewayError("Codex thread id is not canonical")
        return str(parsed)

    def invoke(
        self,
        action: str,
        thread_id: str,
        *,
        text: str = "",
        attachments: Sequence[Path] = (),
        timeout_seconds: int = 20,
    ) -> DesktopGatewayResult:
        if action not in {"draft", "clear", "submit", "stop"}:
            raise DesktopGatewayError("unsupported Codex desktop action")
        canonical_thread = self._validate_thread_id(thread_id)
        if self.background_only and action != "submit":
            raise DesktopGatewayError(
                "background relay supports submit only"
            )
        canonical_attachments: list[str] = []
        for attachment in attachments:
            resolved = Path(attachment).resolve(strict=True)
            if not resolved.is_file():
                raise DesktopGatewayError("desktop attachment is not a file")
            canonical_attachments.append(str(resolved))
        attachment_json = json.dumps(canonical_attachments, ensure_ascii=False)
        command = [
            str(self.powershell_executable),
            "-NoProfile",
            "-STA",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(self.script_path),
            "-Action",
            action,
            "-ThreadId",
            canonical_thread,
            "-TextBase64",
            self._utf8_base64(text),
            "-AttachmentsBase64",
            self._utf8_base64(attachment_json),
            "-TimeoutSeconds",
            str(timeout_seconds),
        ]
        if self.background_only:
            command.append("-BackgroundOnly")
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            completed = self.runner(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds + 15,
                creationflags=creationflags,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DesktopGatewayError("Codex desktop helper did not complete") from exc
        output_lines = [line for line in completed.stdout.splitlines() if line.strip()]
        payload: dict[str, object] | None = None
        if output_lines:
            try:
                candidate = json.loads(output_lines[-1])
            except json.JSONDecodeError:
                candidate = None
            if isinstance(candidate, dict):
                payload = candidate
        if completed.returncode != 0 or payload is None or payload.get("ok") is not True:
            detail = payload.get("error") if payload else None
            raise DesktopGatewayError(
                str(detail) if isinstance(detail, str) else "Codex desktop helper failed"
            )
        if payload.get("threadId") != canonical_thread or payload.get("action") != action:
            raise DesktopGatewayError("Codex desktop helper returned a mismatched result")
        return DesktopGatewayResult(
            action=action,
            thread_id=canonical_thread,
            submitted=payload.get("submitted") is True,
            used_foreground_fallback=payload.get("usedForegroundFallback") is True,
        )

    def submit(
        self,
        thread_id: str,
        text: str,
        *,
        attachments: Sequence[Path] = (),
        timeout_seconds: int = 20,
    ) -> DesktopGatewayResult:
        result = self.invoke(
            "submit",
            thread_id,
            text=text,
            attachments=attachments,
            timeout_seconds=timeout_seconds,
        )
        if not result.submitted:
            raise DesktopGatewayError("Codex desktop did not report input submission")
        return result

    def stop(self, thread_id: str, *, timeout_seconds: int = 20) -> DesktopGatewayResult:
        return self.invoke("stop", thread_id, timeout_seconds=timeout_seconds)
