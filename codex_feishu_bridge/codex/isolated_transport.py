"""Authenticated loopback transport to a separately-principalled App Server worker."""

from __future__ import annotations

import json
import multiprocessing.connection
import os
import secrets
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..security.windows_identity import current_principal, require_distinct_principals
from .app_server_client import AppServerProtocol, HandshakeResult, ProtocolError, ProtocolState
from .proxy_environment import loopback_proxy_environment


class IsolatedTransportError(RuntimeError):
    pass


class IsolatedAppServerTransport:
    """Broker side of a one-shot authenticated AF_INET loopback relay.

    The launch file is ACLed to only SYSTEM, the broker SID, and worker SID. The
    worker learns the random auth key, connects once, and the listener closes.
    It never receives the broker database, provider credential, or approval key.
    """

    def __init__(
        self,
        *,
        protocol: AppServerProtocol,
        executable: Path,
        worker_codex_home: Path,
        worker_sid: str,
        scheduled_task_name: str,
        launch_file: Path,
        forbidden_worker_paths: dict[str, Path] | None = None,
    ) -> None:
        broker = current_principal()
        require_distinct_principals(broker.sid, worker_sid)
        self.protocol = protocol
        self.executable = executable.resolve()
        self.worker_codex_home = worker_codex_home.resolve()
        self.worker_sid = worker_sid
        self.scheduled_task_name = scheduled_task_name
        self.launch_file = launch_file.resolve()
        self.forbidden_worker_paths = {
            name: path.resolve()
            for name, path in (forbidden_worker_paths or {}).items()
        }
        if any(not name or len(name) > 64 for name in self.forbidden_worker_paths):
            raise IsolatedTransportError("worker probe names must contain 1..64 characters")
        self.listener: multiprocessing.connection.Listener | None = None
        self.connection: multiprocessing.connection.Connection | None = None
        self._broker_sid = broker.sid
        self.last_attestation: dict[str, Any] | None = None
        self.proxy_environment = loopback_proxy_environment(os.environ)

    def start(self) -> None:
        if self.connection is not None:
            raise IsolatedTransportError("isolated worker is already connected")
        auth_key = secrets.token_bytes(32)
        self.listener = multiprocessing.connection.Listener(
            ("127.0.0.1", 0), family="AF_INET", authkey=auth_key
        )
        host, port = self.listener.address
        request = {
            "schema": 1,
            "host": host,
            "port": port,
            "auth_key_hex": auth_key.hex(),
            "expires_at": (datetime.now(UTC) + timedelta(seconds=60)).isoformat().replace(
                "+00:00", "Z"
            ),
            "worker_sid": self.worker_sid,
            "codex_executable": str(self.executable),
            "codex_home": str(self.worker_codex_home),
            "proxy_environment": self.proxy_environment,
            "forbidden_probes": [
                {"name": name, "path": str(path)}
                for name, path in sorted(self.forbidden_worker_paths.items())
            ],
        }
        self.launch_file.parent.mkdir(parents=True, exist_ok=True)
        if self.launch_file.exists():
            raise IsolatedTransportError("stale isolated-worker launch file exists")
        with self.launch_file.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(request, handle, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _restrict_launch_acl(self.launch_file, self._broker_sid, self.worker_sid)
        try:
            subprocess.run(
                ["schtasks.exe", "/Run", "/TN", self.scheduled_task_name],
                check=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            socket = self.listener._listener._socket
            socket.settimeout(60)
            self.connection = self.listener.accept()
            self.last_attestation = self._receive_worker_attestation()
        except IsolatedTransportError:
            if self.connection is not None:
                self.connection.close()
                self.connection = None
            raise
        except Exception as exc:
            if self.connection is not None:
                self.connection.close()
                self.connection = None
            raise IsolatedTransportError("isolated worker did not connect before timeout") from exc
        finally:
            self.listener.close()
            self.listener = None
            self._remove_launch_ticket()

    def _receive_worker_attestation(self) -> dict[str, Any]:
        assert self.connection is not None
        if not self.connection.poll(10):
            raise IsolatedTransportError("isolated worker identity attestation timed out")
        try:
            value = json.loads(self.connection.recv_bytes())
        except (EOFError, OSError, json.JSONDecodeError) as exc:
            raise IsolatedTransportError("isolated worker identity attestation is invalid") from exc
        expected_probes = set(self.forbidden_worker_paths)
        expected_proxy_keys = sorted(self.proxy_environment)
        if (
            not isinstance(value, dict)
            or value.get("schema") != 1
            or str(value.get("worker_sid", "")).casefold() != self.worker_sid.casefold()
            or value.get("effective_administrator") is not False
            or not isinstance(value.get("forbidden_path_readable"), dict)
            or set(value["forbidden_path_readable"]) != expected_probes
            or any(result is not False for result in value["forbidden_path_readable"].values())
            or value.get("proxy_environment_keys") != expected_proxy_keys
            or (
                bool(self.proxy_environment)
                and value.get("proxy_endpoint_reachable") is not True
            )
        ):
            raise IsolatedTransportError("isolated worker failed its identity/ACL attestation")
        return value

    def send(self, message: dict[str, Any]) -> None:
        if self.connection is None:
            raise IsolatedTransportError("isolated worker is not connected")
        self.connection.send_bytes(self.protocol.encode(message))

    def receive(self, timeout: float = 10.0) -> dict[str, Any]:
        if self.connection is None:
            raise IsolatedTransportError("isolated worker is not connected")
        if not self.connection.poll(timeout):
            raise ProtocolError("timed out waiting for App Server")
        line = self.connection.recv_bytes()
        message = self.protocol.decode(line)
        self.protocol.observe_incoming(message)
        return message

    def handshake(self, timeout: float = 15.0) -> HandshakeResult:
        self.start()
        self.send(self.protocol.build_initialize())
        response: dict[str, Any] = {}
        while self.protocol.state is not ProtocolState.INITIALIZE_ACKED:
            response = self.receive(timeout)
        self.send(self.protocol.build_initialized())
        return HandshakeResult(response, ())

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self.listener is not None:
            self.listener.close()
            self.listener = None
        self._remove_launch_ticket()
        self.protocol.state = ProtocolState.CLOSED

    def _remove_launch_ticket(self) -> None:
        try:
            self.launch_file.unlink(missing_ok=True)
        except OSError as exc:
            raise IsolatedTransportError("broker could not remove isolated-worker launch file") from exc


def _restrict_launch_acl(path: Path, broker_sid: str, worker_sid: str) -> None:
    commands = [
        ["icacls.exe", str(path), "/inheritance:r"],
        [
            "icacls.exe",
            str(path),
            "/grant:r",
            f"*{broker_sid}:(R,W)",
            f"*{worker_sid}:(R)",
            "*S-1-5-18:(F)",
        ],
    ]
    for command in commands:
        subprocess.run(command, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
