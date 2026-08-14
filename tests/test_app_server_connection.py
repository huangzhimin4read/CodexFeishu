from __future__ import annotations

import queue
import threading
import time
from typing import Any

from codex_feishu_bridge.codex.app_server_client import ProtocolError
from codex_feishu_bridge.codex.connection import AppServerConnection


class _Protocol:
    def __init__(self) -> None:
        self.next_id = 1

    def build_request(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        return {"id": request_id, "method": method, "params": params}


class _IdleThenResponsiveTransport:
    def __init__(self) -> None:
        self.protocol = _Protocol()
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self.first_receive = threading.Event()
        self.closed = False

    def handshake(self) -> None:
        return None

    def receive(self, timeout: float) -> dict[str, Any]:
        if not self.first_receive.is_set():
            self.first_receive.set()
            raise ProtocolError("timed out waiting for App Server")
        try:
            return self.messages.get(timeout=timeout)
        except queue.Empty as exc:
            raise ProtocolError("timed out waiting for App Server") from exc

    def send(self, message: dict[str, Any]) -> None:
        self.messages.put({"id": message["id"], "result": {"ok": True}})

    def close(self) -> None:
        self.closed = True


class _LateResponseTransport(_IdleThenResponsiveTransport):
    def __init__(self) -> None:
        super().__init__()
        self.sent = 0
        self.first_receive.set()

    def send(self, message: dict[str, Any]) -> None:
        self.sent += 1
        if self.sent == 1:
            threading.Thread(
                target=lambda: (
                    time.sleep(0.05),
                    self.messages.put({"id": message["id"], "result": {"late": True}}),
                ),
                daemon=True,
            ).start()
        else:
            self.messages.put({"id": message["id"], "result": {"ok": True}})


def test_idle_receive_timeout_keeps_reader_available_for_next_request() -> None:
    transport = _IdleThenResponsiveTransport()
    connection = AppServerConnection(transport)  # type: ignore[arg-type]
    connection.start()
    assert transport.first_receive.wait(timeout=1)
    time.sleep(0.05)

    assert connection.request("thread/read", {"threadId": "thread-1"}, timeout=1) == {
        "ok": True
    }
    assert connection._reader is not None and connection._reader.is_alive()

    connection.close()


def test_late_response_after_request_timeout_does_not_poison_connection() -> None:
    transport = _LateResponseTransport()
    connection = AppServerConnection(transport)  # type: ignore[arg-type]
    connection.start()

    try:
        connection.request("thread/read", {"threadId": "slow"}, timeout=0.01)
    except ProtocolError as exc:
        assert "timed out waiting for thread/read" in str(exc)
    else:
        raise AssertionError("first request must time out")

    time.sleep(0.1)
    assert connection.is_healthy
    assert connection.request("thread/read", {"threadId": "next"}, timeout=1) == {
        "ok": True
    }
    connection.close()
