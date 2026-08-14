"""Worker-account relay for the isolated App Server subprocess."""

from __future__ import annotations

import json
import multiprocessing.connection
import os
import subprocess
import threading
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from ..security.windows_identity import current_principal, current_token_is_administrator
from ..security.windows_job import KillOnCloseJob
from .proxy_environment import (
    proxy_endpoint_reachable,
    validate_worker_proxy_environment,
)


class WorkerError(RuntimeError):
    pass


def run_worker(launch_file: Path) -> int:
    path = launch_file.resolve()
    with path.open(encoding="utf-8") as handle:
        request = json.load(handle)
    # The launch ticket is deliberately read-only to the worker principal.
    # Only the broker may remove it after the authenticated connection is
    # established, so a compromised worker cannot replace or consume a future
    # ticket.
    if request.get("schema") != 1:
        raise WorkerError("unsupported worker launch schema")
    expires = datetime.fromisoformat(str(request["expires_at"]).replace("Z", "+00:00"))
    if expires <= datetime.now(UTC):
        raise WorkerError("worker launch request expired")
    principal = current_principal()
    if principal.sid.casefold() != str(request["worker_sid"]).casefold():
        raise WorkerError("worker process runs under the wrong Windows principal")
    executable = Path(request["codex_executable"]).resolve()
    if not executable.is_file():
        raise WorkerError("Codex executable is unavailable to the worker")
    connection = multiprocessing.connection.Client(
        (request["host"], int(request["port"])),
        family="AF_INET",
        authkey=bytes.fromhex(request["auth_key_hex"]),
    )
    probes = request.get("forbidden_probes", [])
    if not isinstance(probes, list) or not all(isinstance(item, dict) for item in probes):
        raise WorkerError("worker forbidden-path probes are malformed")
    readable: dict[str, bool] = {}
    for item in probes:
        name = item.get("name")
        probe_path = item.get("path")
        if not isinstance(name, str) or not name or not isinstance(probe_path, str):
            raise WorkerError("worker forbidden-path probe is malformed")
        try:
            with Path(probe_path).open("rb") as handle:
                handle.read(1)
        except OSError:
            readable[name] = False
        else:
            readable[name] = True
    proxy_environment = validate_worker_proxy_environment(
        request.get("proxy_environment", {})
    )
    proxy_reachable = proxy_endpoint_reachable(proxy_environment)
    connection.send_bytes(
        json.dumps(
            {
                "schema": 1,
                "worker_sid": principal.sid,
                "effective_administrator": current_token_is_administrator(),
                "forbidden_path_readable": readable,
                "proxy_environment_keys": sorted(proxy_environment),
                "proxy_endpoint_reachable": proxy_reachable,
                "process_id": os.getpid(),
            },
            separators=(",", ":"),
        ).encode("utf-8")
    )
    if proxy_environment and not proxy_reachable:
        connection.close()
        raise WorkerError("configured loopback proxy is not reachable by worker")
    environment = {
        key: os.environ[key]
        for key in (
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
        if key in os.environ
    }
    environment["CODEX_HOME"] = str(Path(request["codex_home"]).resolve())
    environment.update(proxy_environment)
    process = subprocess.Popen(
        [str(executable), "app-server", "--listen", "stdio://"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=environment,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    job = KillOnCloseJob()
    job.assign(int(process._handle))
    assert process.stdin is not None and process.stdout is not None

    def broker_to_codex() -> None:
        try:
            while True:
                body = connection.recv_bytes()
                process.stdin.write(body)
                process.stdin.flush()
        except (EOFError, OSError):
            pass
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass

    relay = threading.Thread(target=broker_to_codex, daemon=True)
    relay.start()
    try:
        for line in iter(process.stdout.readline, b""):
            connection.send_bytes(line)
    finally:
        connection.close()
        job.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    return int(process.returncode or 0)
