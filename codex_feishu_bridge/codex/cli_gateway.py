"""Safe, non-interactive submission through ``codex exec resume``."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO


class CodexCliGatewayError(RuntimeError):
    """The CLI definitively failed before Codex accepted the turn."""


class CodexCliGatewayUnknown(CodexCliGatewayError):
    """The CLI may have submitted the turn, but acceptance was not confirmed."""


class CodexCliActiveWriter(CodexCliGatewayError):
    """The current Codex host definitively rejected a second task writer."""


@dataclass(frozen=True, slots=True)
class CodexCliGatewayResult:
    thread_id: str


class CodexCliGateway:
    """Start a persisted CLI turn and return after Codex reports ``turn.started``.

    The child continues in the background so the bridge can acknowledge
    acceptance without waiting for the model's complete response.  Its output
    pipes are drained until exit to prevent a verbose agent turn from blocking.
    """

    def __init__(
        self,
        executable: Path,
        codex_home: Path,
        *,
        start_timeout_seconds: float = 30.0,
        popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    ) -> None:
        self.executable = executable.resolve(strict=True)
        self.codex_home = codex_home.resolve(strict=True)
        self.start_timeout_seconds = start_timeout_seconds
        self._popen_factory = popen_factory

    @staticmethod
    def _drain(stream: TextIO, target: deque[str] | None = None) -> None:
        try:
            for line in stream:
                if target is not None:
                    target.append(line.rstrip("\r\n"))
        finally:
            stream.close()

    @staticmethod
    def _read_stdout(stream: TextIO, events: queue.Queue[str | None]) -> None:
        try:
            for line in stream:
                events.put(line)
        finally:
            events.put(None)
            stream.close()

    def submit(
        self,
        thread_id: str,
        text: str,
        *,
        image_paths: Sequence[Path] = (),
        cwd: Path,
    ) -> CodexCliGatewayResult:
        project_root = cwd.resolve(strict=True)
        if not project_root.is_dir():
            raise CodexCliGatewayError("Codex CLI working directory is not a directory")
        images = tuple(Path(path).resolve(strict=True) for path in image_paths)
        if any(not path.is_file() for path in images):
            raise CodexCliGatewayError("Codex CLI image attachment is not a file")

        command = [
            str(self.executable),
            "exec",
            "resume",
            "--json",
            "--skip-git-repo-check",
        ]
        for path in images:
            command.extend(("-i", str(path)))
        command.extend((thread_id, "-"))
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(self.codex_home)
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = self._popen_factory(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(project_root),
            env=environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise CodexCliGatewayError("Codex CLI did not expose its standard streams")

        stderr_tail: deque[str] = deque(maxlen=20)
        events: queue.Queue[str | None] = queue.Queue()
        stdout_thread = threading.Thread(
            target=self._read_stdout,
            args=(process.stdout, events),
            name="codex-cli-stdout",
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=self._drain,
            args=(process.stderr, stderr_tail),
            name="codex-cli-stderr",
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        try:
            process.stdin.write(text)
            process.stdin.close()
        except (BrokenPipeError, OSError) as exc:
            raise CodexCliGatewayError("Codex CLI closed stdin before receiving the prompt") from exc

        deadline = time.monotonic() + self.start_timeout_seconds
        matching_thread = False
        turn_started = False
        while time.monotonic() < deadline:
            try:
                line = events.get(timeout=min(0.2, max(0.01, deadline - time.monotonic())))
            except queue.Empty:
                continue
            if line is None:
                code = process.poll()
                stderr_thread.join(timeout=0.2)
                detail = "; ".join(stderr_tail)
                if (
                    "thread-store conflict" in detail
                    and "already has an active writer" in detail
                ):
                    raise CodexCliActiveWriter(
                        "Codex task already has an active writer"
                    )
                raise CodexCliGatewayError(
                    f"Codex CLI exited before turn start (exit={code}): {detail}".rstrip()
                )
            try:
                event: Any = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            if event_type == "thread.started":
                reported = event.get("thread_id")
                if reported != thread_id:
                    raise CodexCliGatewayUnknown(
                        "Codex CLI reported a different task identity"
                    )
                matching_thread = True
            elif event_type == "turn.started":
                turn_started = True
            if matching_thread and turn_started:
                return CodexCliGatewayResult(thread_id)

        raise CodexCliGatewayUnknown("Codex CLI turn-start acknowledgement timed out")
