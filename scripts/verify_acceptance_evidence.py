"""Verify full acceptance evidence and its exact current source set."""

from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path

from generate_acceptance_evidence import EVIDENCE_ROOT, sha256_file, source_manifest


def verify(path: Path) -> dict[str, object]:
    target = path.resolve()
    if not target.is_relative_to(EVIDENCE_ROOT.resolve()) or target == EVIDENCE_ROOT.resolve():
        raise ValueError("evidence path must be a child of evidence/FINAL")
    if (target / "INVALID.md").exists():
        raise ValueError("evidence directory is explicitly INVALID")
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("local_verdict") != "PASS":
        raise ValueError("local acceptance verdict is not PASS")
    if manifest.get("external_verdict") != "BLOCKED_EXTERNAL":
        raise ValueError("external gate verdict is missing or unsafe")
    if manifest.get("forbidden_legacy_notifier_hits"):
        raise ValueError("forbidden legacy notifier scan is not clean")
    external = json.loads((target / "external-gates.json").read_text(encoding="utf-8"))
    if external.get("overall") != "BLOCKED_EXTERNAL":
        raise ValueError("external-gates artifact does not preserve unresolved status")
    junit_root = ET.parse(target / "test-results.xml").getroot()
    suite = junit_root.find("testsuite") if junit_root.tag == "testsuites" else junit_root
    if suite is None:
        raise ValueError("JUnit output has no testsuite")
    junit_summary = {
        key: int(suite.attrib.get(key, "0"))
        for key in ("tests", "failures", "errors", "skipped")
    }
    if any(junit_summary[key] for key in ("failures", "errors", "skipped")):
        raise ValueError("JUnit output is not an all-pass, no-skip run")
    if junit_summary != manifest.get("test_summary"):
        raise ValueError("JUnit summary differs from manifest")
    listed = {}
    for line in (target / "artifacts.sha256").read_text(encoding="utf-8").splitlines():
        digest, relative = line.split("  ", 1)
        artifact = (target / relative).resolve()
        if not artifact.is_relative_to(target) or artifact == target:
            raise ValueError(f"artifact path escapes evidence directory: {relative}")
        if relative in listed:
            raise ValueError(f"duplicate artifact entry: {relative}")
        listed[relative] = digest
        if sha256_file(artifact) != digest:
            raise ValueError(f"artifact hash mismatch: {relative}")
    actual = {
        item.relative_to(target).as_posix()
        for item in target.rglob("*")
        if item.is_file() and item.name != "artifacts.sha256"
    }
    if actual != set(listed):
        raise ValueError("artifact set mismatch")
    current = source_manifest()
    if current != manifest.get("source_files", {}):
        raise ValueError("current source set/hash differs from sealed acceptance baseline")
    return {
        "verified": True,
        "run_id": manifest["run_id"],
        "tests": manifest["test_summary"]["tests"],
        "artifacts": len(listed),
        "source_files": len(current),
        "external_verdict": manifest["external_verdict"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.path), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
