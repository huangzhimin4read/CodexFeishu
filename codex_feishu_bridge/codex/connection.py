"""Concurrent App Server connection for turns and deferred approvals."""

from __future__ import annotations

import queue
import threading
from collections import deque
from collections.abc import Callable
from typing import Any

from .app_server_client import ProtocolError, StdioAppServer


class AppServerConnection:
    def __init__(self, transport: StdioAppServer) -> None:
        self.transport = transport
        self.responses: dict[int | str, queue.Queue[dict[str, Any]]] = {}
        self.server_requests: queue.Queue[dict[str, Any]] = queue.Queue()
        self.notifications: queue.Queue[dict[str, Any]] = queue.Queue()
        self._send_lock = threading.Lock()
        self._responses_lock = threading.Lock()
        self._expired_response_ids: set[int | str] = set()
        self._expired_response_order: deque[int | str] = deque()
        self._closed = threading.Event()
        self._failure: BaseException | None = None
        self._reader: threading.Thread | None = None

    def start(self) -> None:
        self.transport.handshake()
        self._reader = threading.Thread(target=self._read_loop, name="codex-app-server-reader", daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        try:
            while not self._closed.is_set():
                try:
                    message = self.transport.receive(timeout=1.0)
                except ProtocolError as exc:
                    # The transport uses a receive timeout so the reader can
                    # notice shutdown promptly.  An idle App Server is normal;
                    # it must not terminate the only response/notification
                    # reader and strand the next request until its own timeout.
                    if str(exc) == "timed out waiting for App Server":
                        continue
                    raise
                if "method" in message and "id" in message:
                    self.server_requests.put(message)
                elif "method" in message:
                    self.notifications.put(message)
                else:
                    request_id = message.get("id")
                    with self._responses_lock:
                        target = self.responses.get(request_id)
                        expired = request_id in self._expired_response_ids
                        if expired:
                            self._expired_response_ids.discard(request_id)
                    if target is None:
                        if expired:
                            # A request-level timeout does not prove the
                            # connection failed.  The App Server may complete
                            # it later, especially while another turn is busy.
                            # Ignore only IDs registered by this connection as
                            # expired; a genuinely unknown response still
                            # fails closed below.
                            continue
                        raise ProtocolError("response has no registered local waiter")
                    target.put(message)
        except ProtocolError as exc:
            if not self._closed.is_set():
                self._failure = exc
        except BaseException as exc:
            self._failure = exc
        finally:
            self._closed.set()

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 30.0,
        before_send: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        if self._failure is not None:
            raise ProtocolError(f"App Server reader failed: {type(self._failure).__name__}")
        if self._closed.is_set():
            raise ProtocolError("App Server connection is closed")
        message = self.transport.protocol.build_request(method, params)
        target: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._responses_lock:
            self.responses[message["id"]] = target
        try:
            if before_send is not None:
                before_send(message)
            with self._send_lock:
                self.transport.send(message)
            try:
                response = target.get(timeout=timeout)
            except queue.Empty as exc:
                with self._responses_lock:
                    self._expired_response_ids.add(message["id"])
                    self._expired_response_order.append(message["id"])
                    while len(self._expired_response_order) > 1024:
                        oldest = self._expired_response_order.popleft()
                        self._expired_response_ids.discard(oldest)
                raise ProtocolError(f"timed out waiting for {method}") from exc
            if "error" in response:
                raise ProtocolError(f"{method} failed: {response['error']}")
            return response["result"]
        finally:
            with self._responses_lock:
                self.responses.pop(message["id"], None)

    @property
    def failure(self) -> BaseException | None:
        return self._failure

    @property
    def is_healthy(self) -> bool:
        return self._failure is None and not self._closed.is_set()

    def respond(self, request_id: int | str, result: dict[str, Any]) -> None:
        response = self.transport.protocol.build_response(request_id, result)
        with self._send_lock:
            self.transport.send(response)

    def respond_error(self, request_id: int | str, *, code: int, message: str) -> None:
        response = self.transport.protocol.build_error_response(
            request_id,
            code=code,
            message=message,
        )
        with self._send_lock:
            self.transport.send(response)

    def close(self) -> None:
        self._closed.set()
        self.transport.close()
        if self._reader is not None:
            self._reader.join(timeout=3)
