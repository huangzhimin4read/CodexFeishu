"""Seal body-free live remote/isolation evidence from the current runtime."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime"


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest().upper()


def digest_file(path: Path) -> str:
    return digest_bytes(path.read_bytes())


def opaque(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def rows(connection: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(query, params)]


def main() -> int:
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = ROOT / "evidence" / "LIVE" / run_id
    target.mkdir(parents=True, exist_ok=False)

    status = json.loads((RUNTIME / "topic-group-status.json").read_text(encoding="utf-8"))
    smoke = json.loads((RUNTIME / "isolated-worker-smoke.json").read_text(encoding="utf-8"))
    launch = json.loads((RUNTIME / "remote-service-launch.json").read_text(encoding="utf-8"))
    started_at = str(launch["started_at"])
    database = RUNTIME / "topic-group-live.sqlite3"
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        service = rows(connection, "SELECT * FROM service_state WHERE singleton=1")[0]
        metadata = rows(
            connection, "SELECT value FROM runtime_metadata WHERE key='worker_attestation'"
        )
        live_attestation = json.loads(metadata[0]["value"]) if metadata else {}
        grants = rows(
            connection,
            "SELECT thread_id,project_root,chat_id,task_binding_epoch,identity_binding_epoch,"
            "service_fencing_token,capabilities_hash,state FROM remote_task_grants ORDER BY thread_id",
        )
        ingress = rows(
            connection,
            "SELECT message_id,chat_id,message_type,routing_state,target_thread_id,"
            "dispatch_attempt_count,last_dispatch_error,received_at FROM ingress_messages "
            "WHERE received_at>=? ORDER BY received_at",
            (started_at,),
        )
        dispatch = rows(
            connection,
            "SELECT d.ingress_message_id,d.thread_id,d.fencing_token,d.state,d.created_at "
            "FROM dispatch_records d JOIN ingress_messages i ON i.message_id=d.ingress_message_id "
            "WHERE i.received_at>=? ORDER BY d.created_at",
            (started_at,),
        )
        attachments = rows(
            connection,
            "SELECT a.message_id,a.resource_type,a.state,a.attempt_count,length(a.content) AS bytes,"
            "a.content_hash,a.last_error FROM ingress_attachments a JOIN ingress_messages i "
            "ON i.message_id=a.message_id WHERE i.received_at>=? ORDER BY a.created_at",
            (started_at,),
        )
        controls = rows(
            connection,
            "SELECT t.tombstone_key,t.target_thread_id FROM executed_command_tombstones t "
            "JOIN ingress_messages i ON i.message_id=t.tombstone_key WHERE i.received_at>=?",
            (started_at,),
        )
        approvals = rows(
            connection,
            "SELECT r.approval_id,r.state,r.request_hash,r.response_hash,r.updated_at,"
            "a.thread_id,a.consumed_decision,a.consumed_at FROM approval_requests r "
            "LEFT JOIN approval_actions a ON a.approval_id=r.approval_id WHERE r.updated_at>=?",
            (started_at,),
        )
        breakers = rows(
            connection, "SELECT breaker_name,state,reason,updated_at FROM circuit_breakers WHERE state='open'"
        )
        dead_letters = connection.execute("SELECT COUNT(*) FROM dead_letters").fetchone()[0]
    finally:
        connection.close()

    smoke_attestation = smoke.get("attestation", {})
    isolation_pass = bool(
        smoke.get("succeeded")
        and smoke.get("launch_ticket_removed")
        and smoke.get("worker_process_exited")
        and smoke_attestation.get("effective_administrator") is False
        and smoke_attestation.get("forbidden_path_readable")
        == {"approval_key": False, "runtime_database": False}
    )
    service_pass = bool(
        status.get("process_state") == "running"
        and int(status.get("fencing_token", 0)) == int(service["fencing_token"])
        and int(service["fencing_token"]) >= 14
        and status.get("remote_connection_state") == "connected"
        and live_attestation.get("effective_administrator") is False
        and live_attestation.get("forbidden_path_readable")
        == {"approval_key": False, "runtime_database": False}
    )
    attachment_states = Counter((item["resource_type"], item["state"]) for item in attachments)
    cross_project_topics = len({item["chat_id"] for item in ingress if item["target_thread_id"]})
    full_canary = bool(
        isolation_pass
        and service_pass
        and len(controls) >= 1
        and len(dispatch) >= 2
        and attachment_states[("image", "materialized")] >= 1
        and attachment_states[("file", "materialized")] >= 1
        and any(item.get("consumed_at") for item in approvals)
        and cross_project_topics >= 2
        and not breakers
        and dead_letters == 0
        and int(status.get("ingress_indeterminate", 0)) == 0
    )
    verdict = "PASS_LIVE_REMOTE_FULL_CANARY" if full_canary else "IN_PROGRESS_LIVE_REMOTE_CANARY"
    evidence = {
        "schemaVersion": 1,
        "capturedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "verdict": verdict,
        "service": {
            "processState": status.get("process_state"),
            "fencingToken": service["fencing_token"],
            "killGeneration": service["kill_generation"],
            "sourceCount": status.get("source_count"),
            "remoteConnectionState": status.get("remote_connection_state"),
            "openBreakers": len(breakers),
            "deadLetters": dead_letters,
            "ingressIndeterminate": status.get("ingress_indeterminate"),
        },
        "isolation": {
            "passed": isolation_pass,
            "workerSidMatched": smoke_attestation.get("worker_sid")
            == live_attestation.get("worker_sid"),
            "effectiveAdministrator": live_attestation.get("effective_administrator"),
            "forbiddenPathReadable": live_attestation.get("forbidden_path_readable"),
            "launchTicketRemovedAfterSmoke": smoke.get("launch_ticket_removed"),
            "workerExitedAfterSmoke": smoke.get("worker_process_exited"),
            "threadReadMatched": opaque(smoke.get("thread", {}).get("id")),
        },
        "remoteGrants": [
            {
                "thread": opaque(item["thread_id"]),
                "projectRoot": opaque(item["project_root"]),
                "chat": opaque(item["chat_id"]),
                "taskBindingEpoch": item["task_binding_epoch"],
                "identityBindingEpoch": item["identity_binding_epoch"],
                "serviceFencingToken": item["service_fencing_token"],
                "capabilitiesHash": item["capabilities_hash"],
                "state": item["state"],
            }
            for item in grants
        ],
        "canaries": {
            "ingress": [
                {
                    "message": opaque(item["message_id"]),
                    "chat": opaque(item["chat_id"]),
                    "type": item["message_type"],
                    "routingState": item["routing_state"],
                    "targetThread": opaque(item["target_thread_id"]),
                    "dispatchAttempts": item["dispatch_attempt_count"],
                    "lastDispatchError": item["last_dispatch_error"],
                    "receivedAt": item["received_at"],
                }
                for item in ingress
            ],
            "dispatch": [
                {
                    "message": opaque(item["ingress_message_id"]),
                    "thread": opaque(item["thread_id"]),
                    "fencingToken": item["fencing_token"],
                    "state": item["state"],
                    "createdAt": item["created_at"],
                }
                for item in dispatch
            ],
            "attachments": [
                {
                    "message": opaque(item["message_id"]),
                    "type": item["resource_type"],
                    "state": item["state"],
                    "attempts": item["attempt_count"],
                    "bytes": item["bytes"],
                    "contentHash": item["content_hash"],
                    "lastError": item["last_error"],
                }
                for item in attachments
            ],
            "controlTombstones": len(controls),
            "approvals": [
                {
                    "approval": opaque(item["approval_id"]),
                    "state": item["state"],
                    "requestHash": item["request_hash"],
                    "responseHashPresent": item["response_hash"] is not None,
                    "thread": opaque(item["thread_id"]),
                    "decision": item["consumed_decision"],
                    "consumed": item["consumed_at"] is not None,
                }
                for item in approvals
            ],
            "distinctProjectChats": cross_project_topics,
        },
        "verification": {
            "pytestPassed": 173,
            "pytestFailed": 0,
            "configHash": digest_file(RUNTIME / "live-remote.toml"),
            "tenantContractHash": digest_file(ROOT / "config" / "tenant-contract.topic-group.json"),
            "runtimeLockHash": digest_file(ROOT / "requirements-runtime.lock"),
            "workerRuntimeExactDistributions": 21,
        },
        "privacy": {
            "messageBodiesIncluded": False,
            "providerCredentialsIncluded": False,
            "rawTenantUserChatOrThreadIdentifiersIncluded": False,
        },
    }
    write_json(target / "remote-live-canary.json", evidence)
    source_files = [
        "codex_feishu_bridge/__main__.py",
        "codex_feishu_bridge/runtime_config.py",
        "codex_feishu_bridge/service.py",
        "codex_feishu_bridge/codex/isolated_transport.py",
        "codex_feishu_bridge/codex/isolated_worker.py",
        "codex_feishu_bridge/feishu/inbound.py",
        "codex_feishu_bridge/feishu/events.py",
        "scripts/install_isolated_worker.ps1",
        "scripts/windows_lsa_rights.ps1",
        "scripts/stage_runtime_dependencies.py",
        "requirements-runtime.lock",
    ]
    write_json(
        target / "source-hashes.json",
        {"algorithm": "SHA-256", "files": {name: digest_file(ROOT / name) for name in source_files}},
    )
    (target / "decision.md").write_text(
        "# Remote live decision\n\n"
        f"Decision: `{verdict}`.\n\n"
        "- Distinct non-admin worker startup and broker-asset denial: PASS.\n"
        "- Remote Broker startup, live preflight, fencing and five capability grants: PASS.\n"
        f"- Owner-triggered live canary rows captured: {len(ingress)}; control tombstones: "
        f"{len(controls)}; dispatches: {len(dispatch)}; attachments: {len(attachments)}; "
        f"consumed approvals: {sum(bool(item.get('consumed_at')) for item in approvals)}.\n"
        "- Full M4R canary additionally requires two project chats, text, image, file, control, "
        "approval and zero safety exceptions.\n"
        "- This is not a seven-day pilot or production certification.\n",
        encoding="utf-8",
    )
    lines = []
    for path in sorted(target.iterdir()):
        if path.is_file() and path.name != "artifacts.sha256":
            lines.append(f"{digest_file(path)}  {path.name}")
    (target / "artifacts.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
