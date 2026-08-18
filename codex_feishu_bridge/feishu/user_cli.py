"""Official lark-cli user-identity sender for Codex-authored user messages."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .client import ProviderOutcome, ProviderResult


class LarkCliUnavailable(RuntimeError):
    """Raised when user-identity delivery is configured without the CLI."""


class LarkCliUserSender:
    """Reply in a Feishu thread with an authorized end-user access token."""

    def __init__(
        self,
        executable: Path,
        *,
        profile: str,
        launcher_arguments: tuple[Path, ...] = (),
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        timeout_seconds: float = 30.0,
    ) -> None:
        resolved = executable.resolve(strict=True)
        if not resolved.is_file():
            raise LarkCliUnavailable("lark-cli executable is not a file")
        if not profile.strip():
            raise ValueError("lark-cli profile must not be empty")
        self.executable = resolved
        self.launcher_arguments = tuple(
            str(argument.resolve(strict=True)) for argument in launcher_arguments
        )
        self.profile = profile.strip()
        self.runner = runner
        self.timeout_seconds = timeout_seconds

    @classmethod
    def discover(cls, *, profile: str) -> LarkCliUserSender:
        command = shutil.which("lark-cli.cmd") or shutil.which("lark-cli")
        if not command:
            raise LarkCliUnavailable("official lark-cli is not available on PATH")
        shim = Path(command).resolve(strict=True)
        if shim.suffix.casefold() == ".cmd":
            # npm's Windows batch shim expands `%*` through cmd.exe. Literal
            # `<` and `>` in a user message are then parsed as redirection
            # before the official CLI receives them. Invoke the same packaged
            # JavaScript entrypoint with Node directly so every argument stays
            # an opaque CreateProcess argument.
            script = shim.parent / "node_modules" / "@larksuite" / "cli" / "scripts" / "run.js"
            node = shutil.which("node.exe") or shutil.which("node")
            if not node or not script.is_file():
                raise LarkCliUnavailable(
                    "official lark-cli Node entrypoint is unavailable"
                )
            return cls(
                Path(node),
                profile=profile,
                launcher_arguments=(script,),
            )
        return cls(shim, profile=profile)

    def _command(self, *arguments: str) -> list[str]:
        return [str(self.executable), *self.launcher_arguments, *arguments]

    @staticmethod
    def _json_envelope(stdout: str, stderr: str) -> dict[str, Any] | None:
        for stream in (stdout, stderr):
            try:
                whole = json.loads(stream)
            except json.JSONDecodeError:
                whole = None
            if isinstance(whole, dict):
                return whole
            lines = [line for line in stream.splitlines() if line.strip()]
            for line in reversed(lines):
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    return value
        return None

    def verify_ready(self) -> str:
        """Require only a usable authorized user session.

        Display names and account choices are intentionally mutable in the
        single-user local bridge. Stable chat/message IDs handle routing, so a
        CLI login change must not stop the whole service at startup.
        """
        command = self._command(
            "--profile",
            self.profile,
            "auth",
            "status",
            "--json",
            "--verify",
        )
        environment = os.environ.copy()
        environment["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
        environment["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            completed = self.runner(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                creationflags=creationflags,
                env=environment,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LarkCliUnavailable(
                "unable to verify the authorized lark-cli user identity"
            ) from exc
        envelope = self._json_envelope(completed.stdout, completed.stderr)
        identities = envelope.get("identities") if isinstance(envelope, dict) else None
        user = identities.get("user") if isinstance(identities, dict) else None
        ready = (
            completed.returncode == 0
            and envelope is not None
            and envelope.get("verified") is True
            and isinstance(user, dict)
            and user.get("status") == "ready"
            and user.get("available") is True
            and user.get("verified") is True
            and user.get("tokenStatus") == "valid"
        )
        if not ready:
            raise LarkCliUnavailable(
                "authorized lark-cli user identity is unavailable or unverified"
            )
        user_name = user.get("userName")
        return str(user_name) if isinstance(user_name, str) else ""

    def reply_text(
        self,
        *,
        message_id: str,
        text: str,
        reply_in_thread: bool,
        idempotency_key: str,
    ) -> ProviderResult:
        if not re.fullmatch(r"om_[A-Za-z0-9_-]{1,512}", message_id or ""):
            return ProviderResult(ProviderOutcome.PERMANENT, "user_cli_invalid_message_id")
        if not text:
            return ProviderResult(ProviderOutcome.PERMANENT, "user_cli_empty_text")
        if not 1 <= len(idempotency_key) <= 50:
            return ProviderResult(ProviderOutcome.PERMANENT, "user_cli_invalid_idempotency_key")
        command = self._command(
            "--profile",
            self.profile,
            "im",
            "+messages-reply",
            "--as",
            "user",
            "--message-id",
            message_id,
            "--text",
            text,
            "--idempotency-key",
            idempotency_key,
            "--json",
        )
        if reply_in_thread:
            command.append("--reply-in-thread")
        environment = os.environ.copy()
        environment["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
        environment["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            completed = self.runner(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                creationflags=creationflags,
                env=environment,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ProviderResult(ProviderOutcome.UNKNOWN, "user_cli_timeout")
        except OSError:
            return ProviderResult(ProviderOutcome.PERMANENT, "user_cli_unavailable")
        envelope = self._json_envelope(completed.stdout, completed.stderr)
        if completed.returncode == 0 and envelope and envelope.get("ok") is True:
            data = envelope.get("data")
            message = data if isinstance(data, dict) else envelope
            provider_message_id = message.get("message_id")
            if not isinstance(provider_message_id, str) or not provider_message_id:
                return ProviderResult(
                    ProviderOutcome.PERMANENT,
                    "user_cli_missing_message_id",
                    response=envelope,
                )
            return ProviderResult(
                ProviderOutcome.CONFIRMED,
                "0",
                message_id=provider_message_id,
                chat_id=(
                    str(message["chat_id"])
                    if isinstance(message.get("chat_id"), str)
                    else None
                ),
                response=envelope,
            )
        error = envelope.get("error") if isinstance(envelope, dict) else None
        error_type = str(error.get("type")) if isinstance(error, dict) else ""
        error_subtype = str(error.get("subtype")) if isinstance(error, dict) else ""
        code = "user_cli_" + (error_subtype or error_type or f"exit_{completed.returncode}")
        if error_type in {"authorization", "validation", "configuration"}:
            outcome = ProviderOutcome.PERMANENT
        elif completed.returncode == 10:
            outcome = ProviderOutcome.PERMANENT
        else:
            outcome = ProviderOutcome.RETRYABLE
        return ProviderResult(outcome, code, response=envelope)
