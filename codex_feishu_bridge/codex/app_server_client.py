"""Schema-validated Codex App Server JSONL client primitives."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from jsonschema import Draft7Validator

from .compatibility import CompatibilityMatrix, CompatibilityError


class ProtocolError(RuntimeError):
    """A message violates the pinned protocol or handshake state."""


class ProtocolState(StrEnum):
    NEW = "new"
    INITIALIZE_SENT = "initialize_sent"
    INITIALIZE_ACKED = "initialize_acked"
    READY = "ready"
    CLOSED = "closed"


class SchemaSet:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        with (self.root / "ClientRequest.json").open(encoding="utf-8") as handle:
            self.client_request = Draft7Validator(json.load(handle))
        with (self.root / "ClientNotification.json").open(encoding="utf-8") as handle:
            self.client_notification = Draft7Validator(json.load(handle))
        with (self.root / "ServerRequest.json").open(encoding="utf-8") as handle:
            self.server_request = Draft7Validator(json.load(handle))
        with (self.root / "ServerNotification.json").open(encoding="utf-8") as handle:
            self.server_notification = Draft7Validator(json.load(handle))
        with (self.root / "JSONRPCResponse.json").open(encoding="utf-8") as handle:
            self.response = Draft7Validator(json.load(handle))
        with (self.root / "JSONRPCError.json").open(encoding="utf-8") as handle:
            self.error = Draft7Validator(json.load(handle))
        self.typed_responses: dict[str, Draft7Validator] = {}
        response_files = {
            "initialize": "v1/InitializeResponse.json",
            "configRequirements/read": "v2/ConfigRequirementsReadResponse.json",
            "thread/read": "v2/ThreadReadResponse.json",
            "thread/resume": "v2/ThreadResumeResponse.json",
            "turn/start": "v2/TurnStartResponse.json",
            "turn/steer": "v2/TurnSteerResponse.json",
            "turn/interrupt": "v2/TurnInterruptResponse.json",
        }
        for method, relative in response_files.items():
            path = self.root / relative
            if path.is_file():
                with path.open(encoding="utf-8") as handle:
                    self.typed_responses[method] = Draft7Validator(json.load(handle))

    @staticmethod
    def _raise_first(validator: Draft7Validator, message: dict[str, Any]) -> None:
        errors = sorted(validator.iter_errors(message), key=lambda item: list(item.path))
        if errors:
            raise ProtocolError(f"schema validation failed: {errors[0].message}")

    def validate_request(self, message: dict[str, Any]) -> None:
        self._raise_first(self.client_request, message)

    def validate_notification(self, message: dict[str, Any]) -> None:
        self._raise_first(self.client_notification, message)

    def validate_incoming(self, message: dict[str, Any]) -> None:
        if "method" in message and "id" in message:
            self._raise_first(self.server_request, message)
        elif "method" in message:
            self._raise_first(self.server_notification, message)
        elif "result" in message:
            self._raise_first(self.response, message)
        elif "error" in message:
            self._raise_first(self.error, message)
        else:
            raise ProtocolError("unclassifiable App Server message")

    def validate_typed_response(self, method: str, result: Any) -> None:
        validator = self.typed_responses.get(method)
        if validator is not None:
            errors = sorted(validator.iter_errors(result), key=lambda item: list(item.path))
            if errors:
                raise ProtocolError(
                    f"typed response validation failed for {method}: {errors[0].message}"
                )


class AppServerProtocol:
    """Enforces initialize/initialized ordering and generated client schemas."""

    def __init__(
        self,
        stable_schema_root: Path,
        *,
        experimental_schema_root: Path | None = None,
        experimental_api: bool = False,
        compatibility_matrix: CompatibilityMatrix | None = None,
        approved_experimental_client_methods: frozenset[str] = frozenset(),
        approved_experimental_server_methods: frozenset[str] = frozenset(),
        approved_experimental_server_fields: frozenset[tuple[str, str]] = frozenset(),
    ) -> None:
        self.stable = SchemaSet(stable_schema_root)
        self.experimental = (
            SchemaSet(experimental_schema_root) if experimental_schema_root else None
        )
        self.experimental_api = experimental_api
        self.compatibility_matrix = compatibility_matrix
        self.approved_experimental_client_methods = approved_experimental_client_methods
        self.approved_experimental_server_methods = approved_experimental_server_methods
        self.approved_experimental_server_fields = approved_experimental_server_fields
        if experimental_api and self.experimental is None:
            raise ProtocolError("experimental API requires a pinned experimental schema")
        self.state = ProtocolState.NEW
        self._initialize_id: int | str | None = None
        self._next_id = 1
        self._outstanding: dict[int | str, str] = {}

    def build_initialize(
        self,
        *,
        name: str = "codex_feishu_bridge",
        title: str = "Codex Feishu Bridge",
        version: str = "0.1.0",
    ) -> dict[str, Any]:
        if self.state is not ProtocolState.NEW:
            raise ProtocolError("initialize must be sent exactly once")
        request_id = 0
        capabilities: dict[str, Any] = {"experimentalApi": self.experimental_api}
        message = {
            "method": "initialize",
            "id": request_id,
            "params": {
                "clientInfo": {"name": name, "title": title, "version": version},
                "capabilities": capabilities,
            },
        }
        self.stable.validate_request(message)
        self._initialize_id = request_id
        self._outstanding[request_id] = "initialize"
        self.state = ProtocolState.INITIALIZE_SENT
        return message

    def observe_incoming(self, message: dict[str, Any]) -> None:
        if not isinstance(message, dict):
            raise ProtocolError("server message must be an object")
        if "jsonrpc" in message:
            raise ProtocolError("App Server wire messages omit the jsonrpc header")
        validator = self.experimental if self.experimental_api else self.stable
        assert validator is not None
        validator.validate_incoming(message)
        if "method" in message:
            self._gate_incoming_method(message)
        if "id" in message and "method" not in message:
            request_id = message.get("id")
            if request_id not in self._outstanding:
                raise ProtocolError("response id is unknown or already resolved")
            method = self._outstanding[request_id]
            if "error" in message:
                del self._outstanding[request_id]
                if method == "initialize":
                    raise ProtocolError(f"initialize failed: {message['error']}")
                return
            if "result" not in message:
                raise ProtocolError("response has neither result nor error")
            validator.validate_typed_response(method, message["result"])
            del self._outstanding[request_id]
            if method == "initialize":
                if self.state is not ProtocolState.INITIALIZE_SENT:
                    raise ProtocolError("initialize response arrived in the wrong state")
                self.state = ProtocolState.INITIALIZE_ACKED

    def _gate_incoming_method(self, message: dict[str, Any]) -> None:
        method = str(message["method"])
        if self.compatibility_matrix is None:
            return
        direction = "ServerRequest" if "id" in message else "ServerNotification"
        classification = self.compatibility_matrix.classify_method(direction, method)
        if classification is None:
            raise ProtocolError(f"unsupported incoming method: {method}")
        if classification == "experimental" and (
            not self.experimental_api or method not in self.approved_experimental_server_methods
        ):
            raise ProtocolError(f"experimental incoming method is not individually approved: {method}")
        params = message.get("params")
        if isinstance(params, dict):
            for field in self.compatibility_matrix.experimental_request_fields(method):
                if field in params and (
                    not self.experimental_api
                    or (method, field) not in self.approved_experimental_server_fields
                ):
                    raise ProtocolError(
                        "experimental incoming field is not individually approved: "
                        f"{method}.{field}"
                    )

    def build_initialized(self) -> dict[str, Any]:
        if self.state is not ProtocolState.INITIALIZE_ACKED:
            raise ProtocolError("initialized requires a successful initialize response")
        message = {"method": "initialized", "params": {}}
        self.stable.validate_notification(message)
        self.state = ProtocolState.READY
        return message

    def build_request(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if self.state is not ProtocolState.READY:
            raise ProtocolError("business requests require a completed handshake")
        if self.compatibility_matrix is not None:
            classification = self.compatibility_matrix.classify_method("ClientRequest", method)
            if classification is None:
                raise ProtocolError(f"unsupported client method: {method}")
            if classification == "experimental" and (
                not self.experimental_api or method not in self.approved_experimental_client_methods
            ):
                raise ProtocolError(
                    f"experimental client method is not individually approved: {method}"
                )
        request_id = self._next_id
        self._next_id += 1
        message: dict[str, Any] = {"method": method, "id": request_id}
        if params is not None:
            message["params"] = params
        validator = self.experimental if self.experimental_api else self.stable
        assert validator is not None
        validator.validate_request(message)
        self._outstanding[request_id] = method
        return message

    def build_response(self, request_id: int | str, result: dict[str, Any]) -> dict[str, Any]:
        message = {"id": request_id, "result": result}
        validator = self.experimental if self.experimental_api else self.stable
        assert validator is not None
        validator._raise_first(validator.response, message)
        return message

    def build_error_response(
        self,
        request_id: int | str,
        *,
        code: int,
        message: str,
    ) -> dict[str, Any]:
        response = {
            "id": request_id,
            "error": {"code": code, "message": message},
        }
        validator = self.experimental if self.experimental_api else self.stable
        assert validator is not None
        validator._raise_first(validator.error, response)
        return response

    @staticmethod
    def encode(message: dict[str, Any]) -> bytes:
        if "jsonrpc" in message:
            raise ProtocolError("App Server wire messages omit the jsonrpc header")
        return json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"

    @staticmethod
    def decode(line: bytes) -> dict[str, Any]:
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError("invalid App Server JSONL message") from exc
        if not isinstance(message, dict):
            raise ProtocolError("App Server message must be an object")
        if "jsonrpc" in message:
            raise ProtocolError("App Server wire messages omit the jsonrpc header")
        return message


@dataclass(slots=True)
class HandshakeResult:
    initialize_response: dict[str, Any]
    stderr: tuple[str, ...]


class StdioAppServer:
    """Disposable stdio transport used by the M0 protocol proof."""

    def __init__(
        self,
        executable: Path,
        codex_home: Path,
        protocol: AppServerProtocol,
    ) -> None:
        self.executable = executable.resolve()
        self.codex_home = codex_home.resolve()
        self.protocol = protocol
        self.process: subprocess.Popen[bytes] | None = None
        self._stdout: queue.Queue[bytes | None] = queue.Queue()
        self._stderr: list[str] = []
        self._job: object | None = None
        self.notifications: list[dict[str, Any]] = []

    def _environment(self) -> dict[str, str]:
        allowed = (
            "COMSPEC",
            "LOCALAPPDATA",
            "PATH",
            "PATHEXT",
            "SYSTEMDRIVE",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "USERPROFILE",
            "WINDIR",
        )
        environment = {key: os.environ[key] for key in allowed if key in os.environ}
        environment["CODEX_HOME"] = str(self.codex_home)
        return environment

    def start(self) -> None:
        if self.process is not None:
            raise ProtocolError("App Server process already started")
        self.codex_home.mkdir(parents=True, exist_ok=True)
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.process = subprocess.Popen(
            [str(self.executable), "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._environment(),
            creationflags=creationflags,
        )
        if os.name == "nt":
            from ..security.windows_job import KillOnCloseJob

            self._job = KillOnCloseJob()
            self._job.assign(int(self.process._handle))
        assert self.process.stdout is not None
        assert self.process.stderr is not None
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        for line in iter(self.process.stdout.readline, b""):
            self._stdout.put(line)
        self._stdout.put(None)

    def _read_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        for line in iter(self.process.stderr.readline, b""):
            self._stderr.append(line.decode("utf-8", errors="replace").rstrip())

    def send(self, message: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise ProtocolError("App Server process is not running")
        self.process.stdin.write(self.protocol.encode(message))
        self.process.stdin.flush()

    def receive(self, timeout: float = 10.0) -> dict[str, Any]:
        try:
            line = self._stdout.get(timeout=timeout)
        except queue.Empty as exc:
            raise ProtocolError("timed out waiting for App Server") from exc
        if line is None:
            code = self.process.poll() if self.process else None
            raise ProtocolError(f"App Server stdout closed (exit={code})")
        message = self.protocol.decode(line)
        self.protocol.observe_incoming(message)
        return message

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 30.0,
        on_server_request: object | None = None,
    ) -> dict[str, Any]:
        request = self.protocol.build_request(method, params)
        request_id = request["id"]
        self.send(request)
        while True:
            message = self.receive(timeout=timeout)
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    raise ProtocolError(f"{method} failed: {message['error']}")
                return message["result"]
            if "method" in message and "id" in message:
                if on_server_request is None or not callable(on_server_request):
                    raise ProtocolError("server request arrived without an approved handler")
                result = on_server_request(message)
                if not isinstance(result, dict):
                    raise ProtocolError("server request handler must return an object")
                self.send(self.protocol.build_response(message["id"], result))
            else:
                self.notifications.append(message)

    def handshake(self, timeout: float = 10.0) -> HandshakeResult:
        self.start()
        initialize = self.protocol.build_initialize()
        self.send(initialize)
        while self.protocol.state is not ProtocolState.INITIALIZE_ACKED:
            response = self.receive(timeout=timeout)
        self.send(self.protocol.build_initialized())
        return HandshakeResult(initialize_response=response, stderr=tuple(self._stderr))

    def close(self) -> None:
        if self.process is None:
            return
        if self.process.stdin:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.protocol.state = ProtocolState.CLOSED
        if self._job is not None:
            self._job.close()
            self._job = None

    def __enter__(self) -> StdioAppServer:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
