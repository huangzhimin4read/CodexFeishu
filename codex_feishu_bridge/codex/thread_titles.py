"""Asynchronous, schema-validated Codex task-title discovery."""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from typing import Callable, Protocol

from .app_server_client import AppServerProtocol, StdioAppServer


class _TitleTransport(Protocol):
    def handshake(self, timeout: float = 10.0) -> object: ...

    def request(
        self,
        method: str,
        params: dict[str, object] | None = None,
        *,
        timeout: float = 30.0,
        on_server_request: object | None = None,
    ) -> dict[str, object]: ...

    def close(self) -> None: ...


class CodexThreadTitleReader:
    """Keep task names fresh without blocking message delivery.

    Codex App Server is the authority for the user-facing ``Thread.name``.
    The SQLite ``threads.title`` column is only the initial prompt-derived
    title and can remain stale after a desktop rename. Only the stable,
    schema-validated ``thread/read`` method is issued by this reader.
    """

    def __init__(
        self,
        *,
        executable: Path,
        codex_home: Path,
        stable_schema_root: Path,
        refresh_seconds: float = 10.0,
        transport_factory: Callable[[], _TitleTransport] | None = None,
    ) -> None:
        if refresh_seconds <= 0:
            raise ValueError("title refresh interval must be positive")
        self.refresh_seconds = refresh_seconds
        if transport_factory is None:
            self._transport_factory = lambda: StdioAppServer(
                executable,
                codex_home,
                AppServerProtocol(stable_schema_root),
            )
        else:
            self._transport_factory = transport_factory
        self._requests: queue.Queue[tuple[str, Path]] = queue.Queue()
        self._lock = threading.Lock()
        self._queued: set[str] = set()
        self._cache: dict[str, tuple[str, Path, float]] = {}
        self._last_attempt: dict[str, float] = {}
        self._completed_attempts: set[str] = set()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._transport: _TitleTransport | None = None
        self.last_error: str | None = None

    def start(self) -> None:
        if self._worker is not None:
            return
        self._worker = threading.Thread(
            target=self._run,
            name="codex-task-title-reader",
            daemon=True,
        )
        self._worker.start()

    def request_title(self, thread_id: str, project_root: Path) -> None:
        if not thread_id:
            raise ValueError("thread id must not be empty")
        expected_root = project_root.resolve()
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(thread_id)
            if cached is not None and cached[1] == expected_root and now - cached[2] < self.refresh_seconds:
                return
            attempted_at = self._last_attempt.get(thread_id)
            if attempted_at is not None and now - attempted_at < self.refresh_seconds:
                return
            if thread_id in self._queued:
                return
            self._queued.add(thread_id)
            self._last_attempt[thread_id] = now
        self._requests.put((thread_id, expected_root))

    def title_for(self, thread_id: str, project_root: Path) -> str | None:
        expected_root = project_root.resolve()
        with self._lock:
            cached = self._cache.get(thread_id)
            if cached is None or cached[1] != expected_root:
                return None
            return cached[0]

    def completed_attempt(self, thread_id: str) -> bool:
        with self._lock:
            return thread_id in self._completed_attempts

    def close(self) -> None:
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=6.0)
        self._close_transport()

    def _ensure_transport(self) -> _TitleTransport:
        if self._transport is None:
            transport = self._transport_factory()
            transport.handshake(timeout=10.0)
            self._transport = transport
        return self._transport

    def _close_transport(self) -> None:
        transport, self._transport = self._transport, None
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                thread_id, expected_root = self._requests.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                result = self._ensure_transport().request(
                    "thread/read",
                    {"threadId": thread_id, "includeTurns": False},
                    timeout=5.0,
                )
                thread = result.get("thread")
                if not isinstance(thread, dict) or thread.get("id") != thread_id:
                    raise ValueError("thread/read returned another task")
                raw_cwd = thread.get("cwd")
                if not isinstance(raw_cwd, str) or Path(raw_cwd).resolve() != expected_root:
                    raise ValueError("thread/read task cwd disagrees with discovery")
                raw_name = thread.get("name")
                if not isinstance(raw_name, str) or not raw_name.strip():
                    raise ValueError("thread/read task name is absent")
                with self._lock:
                    self._cache[thread_id] = (raw_name, expected_root, time.monotonic())
                    self.last_error = None
            except Exception as exc:
                with self._lock:
                    self.last_error = type(exc).__name__
                self._close_transport()
            finally:
                with self._lock:
                    self._queued.discard(thread_id)
                    self._completed_attempts.add(thread_id)
