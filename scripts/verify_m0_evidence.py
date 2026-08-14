"""Verify a sealed M0 evidence directory and its current source baseline."""

from __future__ import annotations

import argparse
import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path

from generate_m0_evidence import source_manifest


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_ROOT = (ROOT / "evidence" / "M0").resolve()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def verify(run: Path) -> dict[str, object]:
    resolved = run.resolve()
    if not resolved.is_relative_to(EVIDENCE_ROOT) or resolved == EVIDENCE_ROOT:
        raise ValueError("run must be a child of evidence/M0")
    if (resolved / "INVALID.md").exists():
        raise ValueError("evidence run is explicitly INVALID")

    manifest = json.loads((resolved / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("implementationVerdict") != "PASS":
        raise ValueError("implementation verdict is not PASS")

    listed: dict[str, str] = {}
    for line in (resolved / "artifacts.sha256").read_text(encoding="utf-8").splitlines():
        expected, relative = line.split("  ", 1)
        listed[relative] = expected
        actual = sha256_file(resolved / Path(relative))
        if actual != expected:
            raise ValueError(f"artifact hash mismatch: {relative}")
    actual_files = {
        path.relative_to(resolved).as_posix()
        for path in resolved.rglob("*")
        if path.is_file() and path.name != "artifacts.sha256"
    }
    if actual_files != set(listed):
        raise ValueError("artifact list does not exactly cover the run directory")

    junit_root = ET.parse(resolved / "test-results.xml").getroot()
    suite = junit_root.find("testsuite") if junit_root.tag == "testsuites" else junit_root
    if suite is None:
        raise ValueError("JUnit output has no testsuite")
    if any(int(suite.attrib.get(name, "0")) for name in ("failures", "errors", "skipped")):
        raise ValueError("JUnit output is not an all-pass run")
    tests = int(suite.attrib["tests"])
    if tests != manifest.get("testCount"):
        raise ValueError("JUnit test count differs from manifest")

    expected_sources = manifest.get("sourceFiles", {})
    current_sources = source_manifest()
    if set(current_sources) != set(expected_sources):
        added = sorted(set(current_sources) - set(expected_sources))
        removed = sorted(set(expected_sources) - set(current_sources))
        raise ValueError(f"source set changed after evidence run: added={added}, removed={removed}")
    stale = []
    for relative, expected in expected_sources.items():
        path = ROOT / Path(relative)
        if not path.is_file() or current_sources.get(relative) != expected:
            stale.append(relative)
    if stale:
        raise ValueError(f"source baseline changed after evidence run: {stale}")
    return {
        "runId": manifest["runId"],
        "tests": tests,
        "artifacts": len(listed),
        "sourceFiles": len(manifest.get("sourceFiles", {})),
        "verified": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.run), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
