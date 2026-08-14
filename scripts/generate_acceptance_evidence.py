"""Run the consolidated local acceptance suite and seal full-tree evidence."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_ROOT = ROOT / "evidence" / "FINAL"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codex_feishu_bridge.runtime_storage import RUNTIME_SCHEMA_VERSION


_ACTIVE_TARGET: Path | None = None


def sha256_file(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest().upper()


def source_manifest() -> dict[str, str]:
    files: set[Path] = set()
    for pattern in (
        "codex_feishu_bridge/**/*.py",
        "scripts/*.py",
        "scripts/*.ps1",
        "tests/**/*.py",
        "tests/fixtures/**/*",
        "config/*",
        "*.md",
        "*.toml",
        "*.lock",
    ):
        files.update(path for path in ROOT.glob(pattern) if path.is_file())
    for relative in (
        "generated/codex/0.145.0/baseline.json",
        "generated/codex/0.145.0/compatibility-matrix.json",
        "generated/codex/0.145.0/schema-files.sha256",
        "generated/codex/0.145.0/stable/codex_app_server_protocol.schemas.json",
        "generated/codex/0.145.0/experimental/codex_app_server_protocol.schemas.json",
    ):
        files.add(ROOT / relative)
    return {
        path.relative_to(ROOT).as_posix(): sha256_file(path)
        for path in sorted(files)
    }


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def run(command: list[str], log: Path, *, timeout: int = 300) -> None:
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
        raise RuntimeError(f"acceptance command failed; see {log}")


def main() -> int:
    global _ACTIVE_TARGET
    started = datetime.now(UTC)
    run_id = started.strftime("%Y%m%dT%H%M%SZ")
    target = EVIDENCE_ROOT / run_id
    _ACTIVE_TARGET = target
    logs = target / "logs"
    logs.mkdir(parents=True, exist_ok=False)
    run([sys.executable, "scripts/generate_codex_baseline.py"], logs / "schema-baseline.log")
    run(
        [sys.executable, "-m", "compileall", "-q", "codex_feishu_bridge", "scripts", "tests"],
        logs / "compileall.log",
    )
    run(
        [sys.executable, "scripts/check_locked_environment.py"],
        logs / "locked-environment.log",
    )
    run(
        [sys.executable, "scripts/run_large_rollout_stress.py"],
        logs / "large-rollout-stress.log",
        timeout=600,
    )
    junit = target / "test-results.xml"
    run(
        [sys.executable, "-m", "pytest", "-q", f"--junitxml={junit}"],
        logs / "pytest.log",
        timeout=600,
    )
    suite_root = ET.parse(junit).getroot()
    suite = suite_root.find("testsuite") if suite_root.tag == "testsuites" else suite_root
    if suite is None:
        raise RuntimeError("JUnit output has no test suite")
    summary = {
        key: int(suite.attrib.get(key, "0"))
        for key in ("tests", "failures", "errors", "skipped")
    }
    if summary["failures"] or summary["errors"] or summary["skipped"]:
        raise RuntimeError("acceptance suite must pass without failures, errors, or skips")
    forbidden_hits: list[str] = []
    forbidden_term = bytes((66, 97, 114, 107)).decode("ascii")
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(
            part in {"evidence", ".runtime", ".pytest_cache", "__pycache__"}
            for part in path.parts
        ):
            continue
        if forbidden_term.casefold() in path.name.casefold():
            forbidden_hits.append(path.relative_to(ROOT).as_posix())
            continue
        if path.suffix.lower() in {".py", ".ps1", ".md", ".toml", ".json", ".lock"}:
            try:
                if forbidden_term.casefold() in path.read_text(encoding="utf-8").casefold():
                    forbidden_hits.append(path.relative_to(ROOT).as_posix())
            except UnicodeDecodeError:
                pass
    if forbidden_hits:
        raise RuntimeError(f"forbidden legacy notifier references remain: {forbidden_hits}")
    baseline = json.loads(
        (ROOT / "generated/codex/0.145.0/baseline.json").read_text(encoding="utf-8")
    )
    write_json(
        target / "environment.json",
        {
            "captured_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "local_timezone": str(datetime.now().astimezone().tzinfo),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "python_executable_sha256": sha256_file(Path(sys.executable)),
            "requirements_lock_sha256": sha256_file(ROOT / "requirements-dev.lock"),
            "runtime_requirements_lock_sha256": sha256_file(
                ROOT / "requirements-runtime.lock"
            ),
            "codex": baseline,
            "runtime_schema_version": RUNTIME_SCHEMA_VERSION,
        },
    )
    write_json(
        target / "acceptance-index.json",
        {
            "A0": {"status": "PASS_LOCAL", "evidence": ["test-results.xml", "logs/schema-baseline.log"]},
            "M1": {
                "status": "PASS_LOCAL",
                "tests": ["test_rollout_observer", "test_offline_network_boundary"],
                "stress_evidence": "logs/large-rollout-stress.log",
            },
            "M2": {
                "status": "PASS_CANARY_OUTBOUND_ONLY",
                "tests": [
                    "test_feishu_contract_client",
                    "test_feishu_images",
                    "test_image_reconciliation",
                    "test_runtime_storage_outbound",
                    "test_codex_project_catalog",
                    "test_project_groups",
                ],
                "live_evidence": [
                    "evidence/LIVE/20260812T110255Z",
                    "evidence/LIVE/20260813T043459Z",
                ],
                "remaining_gate": "at least twenty representative tasks across the opted-in projects",
            },
            "M3": {
                "status": "PASS_MOCK_WITH_ONE_LIVE_SAMPLE",
                "tests": ["test_runtime_storage_outbound"],
                "live_evidence": "evidence/LIVE/20260812T080634Z",
                "remaining_gate": "at least fifty transient samples plus limit and time-window boundaries",
            },
            "M2T": {
                "status": "PASS_CANARY_TOPIC_OUTBOUND_ONLY",
                "tests": [
                    "test_topic_group_provisioning",
                    "test_runtime_storage_outbound",
                    "test_inbound_routing",
                    "test_codex_project_catalog",
                    "test_project_groups",
                ],
                "live_evidence": [
                    "evidence/LIVE/20260812T110255Z",
                    "evidence/LIVE/20260813T073245Z",
                ],
                "remaining_gate": "owner final mobile visual confirmation and full M2 sample expansion",
            },
            "M2I": {
                "status": "PASS_CANARY_IMAGE_OUTBOUND_ONLY",
                "tests": [
                    "test_feishu_images",
                    "test_feishu_contract_client",
                    "test_formatter_rate_limit",
                    "test_runtime_storage_outbound",
                    "test_image_reconciliation",
                    "test_topic_group_provisioning",
                ],
                "live_evidence": [
                    "evidence/LIVE/20260813T043459Z",
                    "evidence/LIVE/20260813T073245Z",
                    "evidence/LIVE/20260813T080201Z",
                ],
                "remaining_gate": (
                    "covered project-local Markdown images and rollout-persisted visible tool-output "
                    "data images; remote URL fetching remains disabled"
                ),
            },
            "M3.5": {
                "status": "PASS_LIVE_ISOLATION_PENDING_HARD_STOP",
                "tests": ["test_approvals_audit_emergency", "test_approval_gateway_dynamic"],
                "live_evidence": "evidence/LIVE/20260813T123204Z",
                "remaining_gate": "owner-triggered live hard-stop and sealed installation evidence",
            },
            "M4": {"status": "PASS_LOCAL", "tests": ["test_inbound_routing", "test_codex_controller"]},
            "M4R": {
                "status": "PASS_LOCAL_AND_LIVE_SERVICE_PENDING_USER_CANARIES",
                "tests": [
                    "test_inbound_routing",
                    "test_inbound_attachments",
                    "test_feishu_contract_client",
                    "test_codex_controller",
                    "test_approval_gateway_multi_project",
                    "test_windows_security",
                ],
                "live_evidence": "evidence/LIVE/20260813T123204Z",
                "remaining_gate": (
                    "live text, image, file, control, approval, hard-stop, and cross-project "
                    "topic canaries"
                ),
            },
            "M5": {"status": "PASS_LOCAL", "tests": ["test_managed_requirements_controls", "test_windows_security", "test_protocol_gates"]},
            "M6": {"status": "PASS_LOCAL", "tests": ["test_operations"]},
        },
    )
    write_json(
        target / "external-gates.json",
        {
            "overall": "BLOCKED_EXTERNAL",
            "items": [
                "M2 expansion to at least twenty representative tasks across the opted-in projects",
                "M3 expansion to at least fifty real transient messages and boundary coverage",
                "live approval, hard-stop, inbound attachment/control routing, and execution-profile Gates",
                "seven continuous days of owner-operated pilot evidence",
            ],
            "completed_external_work": (
                "Authorized existing Feishu test application configuration, three-project/five-task "
                "outbound canary, topic-group cutover, image outbound, distinct non-admin Windows "
                "worker installation, ACL attestation, and remote live preflight; owner-triggered "
                "inbound/control/approval canaries remain separately evidenced"
            ),
        },
    )
    manifest = {
        "run_id": run_id,
        "started_at": started.isoformat().replace("+00:00", "Z"),
        "completed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "local_verdict": "PASS",
        "external_verdict": "BLOCKED_EXTERNAL",
        "test_summary": summary,
        "forbidden_legacy_notifier_hits": forbidden_hits,
        "source_files": source_manifest(),
    }
    write_json(target / "manifest.json", manifest)
    (target / "decision.md").write_text(
        "# Acceptance decision\n\nLocal implementation and mock/integration suite: PASS\n\n"
        "Three-project/five-task Feishu test-tenant outbound canary: PASS_CANARY_OUTBOUND_ONLY.\n\n"
        "Private topic-group outbound cutover: PASS_CANARY_TOPIC_OUTBOUND_ONLY.\n\n"
        "Project-local image forwarding: PASS_CANARY_IMAGE_OUTBOUND_ONLY.\n\n"
        "Distinct Windows worker installation and startup attestation: PASS_LIVE_ISOLATION.\n\n"
        "Full M2/M3 sample Gates, live inbound/approval/hard-stop canaries, Broker auto-start, and "
        "seven-day pilot: BLOCKED_EXTERNAL or in progress.\n\n"
        "This package does not claim a production release or a completed live pilot.\n",
        encoding="utf-8",
    )
    lines = []
    for path in sorted(target.rglob("*")):
        if path.is_file() and path.name != "artifacts.sha256":
            lines.append(f"{sha256_file(path)}  {path.relative_to(target).as_posix()}")
    (target / "artifacts.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(target)
    return 0


if __name__ == "__main__":
    try:
        exit_code = main()
    except BaseException as exc:
        if _ACTIVE_TARGET is not None and _ACTIVE_TARGET.is_dir():
            (_ACTIVE_TARGET / "INVALID.md").write_text(
                "# INVALID\n\n"
                f"Evidence generation did not complete: {type(exc).__name__}.\n"
                "This directory must not be used as PASS evidence.\n",
                encoding="utf-8",
            )
        raise
    raise SystemExit(exit_code)
