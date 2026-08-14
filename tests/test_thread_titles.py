import threading
import time
from pathlib import Path

from codex_feishu_bridge.codex.thread_titles import CodexThreadTitleReader


class FakeTransport:
    def __init__(self, cwd: Path, *, name: str = "主力开发") -> None:
        self.cwd = cwd
        self.name = name
        self.handshakes = 0
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.called = threading.Event()
        self.closed = False

    def handshake(self, timeout: float = 10.0) -> object:
        self.handshakes += 1
        return object()

    def request(
        self,
        method: str,
        params: dict[str, object] | None = None,
        *,
        timeout: float = 30.0,
        on_server_request: object | None = None,
    ) -> dict[str, object]:
        assert params is not None
        self.calls.append((method, params))
        self.called.set()
        return {
            "thread": {
                "id": params["threadId"],
                "cwd": str(self.cwd),
                "name": self.name,
            }
        }

    def close(self) -> None:
        self.closed = True


def test_thread_title_reader_uses_stable_read_without_blocking_caller(tmp_path: Path) -> None:
    transport = FakeTransport(tmp_path)
    reader = CodexThreadTitleReader(
        executable=tmp_path / "unused.exe",
        codex_home=tmp_path,
        stable_schema_root=tmp_path,
        refresh_seconds=60,
        transport_factory=lambda: transport,
    )
    reader.start()
    try:
        reader.request_title("thread", tmp_path)
        assert transport.called.wait(2)
        deadline = time.monotonic() + 2
        while reader.title_for("thread", tmp_path) is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert reader.title_for("thread", tmp_path) == "主力开发"
        assert transport.handshakes == 1
        assert transport.calls == [
            ("thread/read", {"threadId": "thread", "includeTurns": False})
        ]
        assert reader.completed_attempt("thread")
    finally:
        reader.close()
    assert transport.closed


def test_thread_title_reader_rejects_mismatched_task_cwd(tmp_path: Path) -> None:
    transport = FakeTransport(tmp_path / "other")
    reader = CodexThreadTitleReader(
        executable=tmp_path / "unused.exe",
        codex_home=tmp_path,
        stable_schema_root=tmp_path,
        refresh_seconds=60,
        transport_factory=lambda: transport,
    )
    reader.start()
    try:
        reader.request_title("thread", tmp_path)
        assert transport.called.wait(2)
        deadline = time.monotonic() + 2
        while not reader.completed_attempt("thread") and time.monotonic() < deadline:
            time.sleep(0.01)
        assert reader.title_for("thread", tmp_path) is None
        assert reader.last_error == "ValueError"
    finally:
        reader.close()
