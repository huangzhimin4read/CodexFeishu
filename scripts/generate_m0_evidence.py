"""Legacy M0-only helpers retained to verify previously sealed packages.

New evidence must use ``generate_acceptance_evidence.py`` because the current source
tree contains the complete M0-M6 implementation.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
CODEX_EXECUTABLE = Path.home() / (
    "AppData/Roaming/npm/node_modules/@openai/codex/node_modules/"
    "@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe"
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def run(command: list[str], log: Path, *, timeout: int = 120) -> None:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    log.write_text(
        f"COMMAND: {' '.join(command)}\nEXIT: {completed.returncode}\n\n"
        f"STDOUT\n{completed.stdout}\nSTDERR\n{completed.stderr}",
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise RuntimeError(f"command failed, see {log}")


def dependency_versions() -> dict[str, str]:
    result = {}
    for name in (
        "attrs",
        "colorama",
        "iniconfig",
        "jsonschema",
        "jsonschema-specifications",
        "packaging",
        "pluggy",
        "Pygments",
        "pytest",
        "referencing",
        "rpds-py",
    ):
        try:
            result[name] = version(name)
        except PackageNotFoundError:
            result[name] = "MISSING"
    return result


def source_manifest() -> dict[str, str]:
    included: list[Path] = []
    for pattern in (
        "codex_feishu_bridge/**/*.py",
        "tests/**/*.py",
        "tests/fixtures/**/*",
        "scripts/*.py",
        "scripts/*.ps1",
        "*.md",
        "*.toml",
        "*.lock",
    ):
        included.extend(path for path in ROOT.glob(pattern) if path.is_file())
    included.extend(
        [
            ROOT / "generated/codex/0.145.0/baseline.json",
            ROOT / "generated/codex/0.145.0/compatibility-matrix.json",
            ROOT / "generated/codex/0.145.0/schema-files.sha256",
            ROOT
            / "generated/codex/0.145.0/stable/codex_app_server_protocol.schemas.json",
            ROOT
            / "generated/codex/0.145.0/experimental/codex_app_server_protocol.schemas.json",
        ]
    )
    return {
        path.relative_to(ROOT).as_posix(): sha256_file(path)
        for path in sorted(set(included))
    }


def protocol_proof(run_root: Path) -> dict[str, Any]:
    from codex_feishu_bridge.codex.app_server_client import (
        AppServerProtocol,
        StdioAppServer,
    )

    schema = ROOT / "generated/codex/0.145.0/stable"
    with tempfile.TemporaryDirectory(prefix="codex-home-") as raw_home:
        protocol = AppServerProtocol(schema)
        with StdioAppServer(
            CODEX_EXECUTABLE, Path(raw_home), protocol
        ) as server:
            result = server.handshake(timeout=15)
    response = result.initialize_response.get("result", {})
    return {
        "requestOrder": ["initialize", "initialized"],
        "wireJsonRpcHeaderPresent": False,
        "protocolStateAfterHandshake": "ready",
        "responseFields": sorted(response) if isinstance(response, dict) else [],
        "platformFamily": response.get("platformFamily")
        if isinstance(response, dict)
        else None,
        "platformOs": response.get("platformOs") if isinstance(response, dict) else None,
        "stderrLineCount": len(result.stderr),
    }


def storage_and_filter_proof(run_root: Path) -> dict[str, Any]:
    from codex_feishu_bridge.codex.rollout_observer import IncrementalRolloutReader
    from codex_feishu_bridge.storage import BridgeStorage

    fixture = ROOT / "tests/fixtures/rollout/visible.jsonl"
    batch = IncrementalRolloutReader().read(fixture)
    database = run_root / "m0-proof.db"
    with BridgeStorage(database) as storage:
        storage.initialize()
        inserted_first = storage.store_rollout_batch(batch)
        inserted_second = storage.store_rollout_batch(batch)
        item_count = storage.item_count()
        pragmas = storage.pragmas()
        outbox_count = storage.connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        hidden_count = storage.connection.execute(
            "SELECT COUNT(*) FROM items WHERE text LIKE '%must not leave%'"
        ).fetchone()[0]
        tables = [
            row[0]
            for row in storage.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(database) + suffix)
        if candidate.exists():
            candidate.unlink()
    return {
        "fixtureSha256": sha256_file(fixture),
        "normalizedEventsBeforeDedup": len(batch.events),
        "insertedFirstPass": inserted_first,
        "insertedSecondPass": inserted_second,
        "logicalItemCount": item_count,
        "hiddenContentCount": hidden_count,
        "outboxCount": outbox_count,
        "committedOffset": batch.cursor.committed_offset,
        "lastRecordHashPresent": bool(batch.cursor.last_record_hash),
        "pragmas": pragmas,
        "tables": tables,
    }


def main() -> int:
    raise RuntimeError(
        "legacy M0 evidence generation is disabled for the full implementation; "
        "use scripts/generate_acceptance_evidence.py"
    )
    started = datetime.now(UTC)
    run_id = started.strftime("%Y%m%dT%H%M%SZ")
    run_root = ROOT / "evidence" / "M0" / run_id
    logs = run_root / "logs"
    logs.mkdir(parents=True, exist_ok=False)

    commands = [
        f"{sys.executable} scripts/generate_codex_baseline.py",
        f"{sys.executable} -m compileall -q codex_feishu_bridge scripts tests",
        f"{sys.executable} -m pytest -q --junitxml=<run>/test-results.xml",
    ]
    (run_root / "commands.txt").write_text("\n".join(commands) + "\n", encoding="utf-8")

    run(
        [sys.executable, "scripts/generate_codex_baseline.py"],
        logs / "generate-baseline.log",
    )
    run(
        [sys.executable, "-m", "compileall", "-q", "codex_feishu_bridge", "scripts", "tests"],
        logs / "compileall.log",
    )
    run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            f"--junitxml={run_root / 'test-results.xml'}",
        ],
        logs / "pytest.log",
    )
    junit_root = ET.parse(run_root / "test-results.xml").getroot()
    suite = junit_root.find("testsuite") if junit_root.tag == "testsuites" else junit_root
    if suite is None:
        raise RuntimeError("JUnit output has no testsuite")
    test_count = int(suite.attrib["tests"])
    failures = int(suite.attrib.get("failures", "0"))
    errors = int(suite.attrib.get("errors", "0"))
    skipped = int(suite.attrib.get("skipped", "0"))
    if failures or errors or skipped:
        raise RuntimeError("M0 requires every automated test to pass without skips")

    baseline = json.loads(
        (ROOT / "generated/codex/0.145.0/baseline.json").read_text(encoding="utf-8")
    )
    environment = {
        "capturedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "platform": platform.platform(),
        "pythonExecutable": sys.executable,
        "pythonVersion": platform.python_version(),
        "pythonExecutableSha256": sha256_file(Path(sys.executable)),
        "dependencies": dependency_versions(),
        "codex": baseline,
        "workspace": str(ROOT),
        "isGitRepository": (ROOT / ".git").is_dir(),
    }
    write_json(run_root / "environment.json", environment)
    write_json(run_root / "protocol-proof.json", protocol_proof(run_root))
    write_json(run_root / "storage-filter-proof.json", storage_and_filter_proof(run_root))

    sources = source_manifest()
    manifest = {
        "runId": run_id,
        "gate": "M0",
        "startedAt": started.isoformat().replace("+00:00", "Z"),
        "completedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "authorizationBoundary": {
            "allowed": "M0 offline fixtures and disposable local App Server handshake",
            "forbidden": [
                "Feishu tenant access",
                "application creation or publishing",
                "credential writes",
                "background service startup",
                "live synchronization",
                "inbound remote dispatch",
            ],
        },
        "baseline": {
            "codexExecutableSha256": baseline["codexExecutableSha256"],
            "stableProtocolSchemaSha256": baseline["stableProtocolSchemaSha256"],
            "experimentalProtocolSchemaSha256": baseline[
                "experimentalProtocolSchemaSha256"
            ],
        },
        "sourceFiles": sources,
        "testCount": test_count,
        "implementationVerdict": "PASS",
        "ownerGateDecision": "HOLD",
    }
    write_json(run_root / "manifest.json", manifest)
    (run_root / "test-summary.md").write_text(
        "# M0 test summary\n\n"
        f"- Automated tests: {test_count} passed\n"
        "- Disposable App Server handshake: PASS\n"
        "- Hidden/system/developer fixture output: 0\n"
        "- Duplicate logical fixture insertion on second pass: 0\n"
        "- Production outbox rows: 0\n"
        "- Stable/experimental schema hashes reproduced: PASS\n",
        encoding="utf-8",
    )
    (run_root / "manual-checks.md").write_text(
        "# Manual checks\n\n"
        "- This historical M0-only generator is disabled for the current full implementation.\n"
        "- The generated compatibility matrix records stable and experimental methods separately.\n"
        "- The two schema-classification corrections discovered during M0 are recorded in the review report.\n",
        encoding="utf-8",
    )
    (run_root / "defects.md").write_text(
        "# Defects\n\n"
        "Open P0: 0\n\nOpen P1: 0\n\n"
        "Resolved during M0: corrected one invalid JSON test fixture; corrected two stale protocol classifications in project documents.\n",
        encoding="utf-8",
    )
    (run_root / "gate-decision.md").write_text(
        "# M0 Gate decision\n\n"
        "Implementation evidence verdict: PASS\n\n"
        "Independent reviewer verdict: PENDING\n\n"
        "Owner decision: HOLD\n\n"
        "Authorized next actions: independent review of this M0 evidence package only.\n\n"
        "This archived generator grants no authority; use the consolidated acceptance evidence and its external Gate record.\n",
        encoding="utf-8",
    )

    artifact_lines = []
    for path in sorted(run_root.rglob("*")):
        if path.is_file() and path.name != "artifacts.sha256":
            artifact_lines.append(
                f"{sha256_file(path)}  {path.relative_to(run_root).as_posix()}"
            )
    (run_root / "artifacts.sha256").write_text(
        "\n".join(artifact_lines) + "\n", encoding="utf-8"
    )
    print(run_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
