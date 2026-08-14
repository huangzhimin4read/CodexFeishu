"""Live one-shot acceptance check for the isolated Codex App Server worker."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from codex_feishu_bridge.codex.app_server_client import AppServerProtocol
from codex_feishu_bridge.codex.compatibility import CompatibilityMatrix
from codex_feishu_bridge.codex.connection import AppServerConnection
from codex_feishu_bridge.codex.isolated_transport import IsolatedAppServerTransport
from codex_feishu_bridge.service import _load_or_create_local_key


def _pid_exists(process_id: int) -> bool:
    result = subprocess.run(
        ["tasklist.exe", "/FI", f"PID eq {process_id}", "/FO", "CSV", "/NH"],
        check=False,
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return str(process_id) in result.stdout


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--schema-root", type=Path, required=True)
    parser.add_argument("--codex-executable", type=Path, required=True)
    parser.add_argument("--codex-home", type=Path, required=True)
    parser.add_argument("--worker-sid", required=True)
    parser.add_argument("--task-name", required=True)
    parser.add_argument("--launch-file", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    workspace = args.workspace.resolve()
    schema_root = args.schema_root.resolve()
    approval_key = workspace / ".runtime" / "approval.key"
    _load_or_create_local_key(approval_key)
    matrix = CompatibilityMatrix.load(schema_root / "compatibility-matrix.json")
    protocol = AppServerProtocol(
        schema_root / "stable",
        experimental_schema_root=schema_root / "experimental",
        experimental_api=True,
        compatibility_matrix=matrix,
        approved_experimental_client_methods=frozenset(),
        approved_experimental_server_methods=frozenset(),
        approved_experimental_server_fields=frozenset(
            {
                ("item/commandExecution/requestApproval", "availableDecisions"),
                ("item/commandExecution/requestApproval", "additionalPermissions"),
            }
        ),
    )
    transport = IsolatedAppServerTransport(
        protocol=protocol,
        executable=args.codex_executable,
        worker_codex_home=args.codex_home,
        worker_sid=args.worker_sid,
        scheduled_task_name=args.task_name,
        launch_file=args.launch_file,
        forbidden_worker_paths={
            "runtime_database": args.database,
            "approval_key": approval_key,
        },
    )
    connection = AppServerConnection(transport)
    captured: dict[str, object] = {
        "captured_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "schema": 1,
        "succeeded": False,
    }
    try:
        connection.start()
        result = connection.request(
            "thread/read", {"threadId": args.thread_id, "includeTurns": False}
        )
        thread = result.get("thread") if isinstance(result, dict) else None
        if not isinstance(thread, dict) or thread.get("id") != args.thread_id:
            raise RuntimeError("thread/read did not return the requested task")
        captured.update(
            {
                "succeeded": True,
                "attestation": transport.last_attestation,
                "thread": {
                    "id": thread.get("id"),
                    "name": thread.get("name"),
                    "cwd": thread.get("cwd"),
                },
            }
        )
    except BaseException as exc:
        captured.update(
            {
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            }
        )
    finally:
        worker_pid = None
        if isinstance(transport.last_attestation, dict):
            value = transport.last_attestation.get("process_id")
            if isinstance(value, int):
                worker_pid = value
        connection.close()
        time.sleep(3)
        captured["launch_ticket_removed"] = not args.launch_file.exists()
        captured["worker_process_exited"] = (
            worker_pid is not None and not _pid_exists(worker_pid)
        )
        if not captured["launch_ticket_removed"] or not captured["worker_process_exited"]:
            captured["succeeded"] = False
        args.output.resolve().write_text(
            json.dumps(captured, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return 0 if captured["succeeded"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
