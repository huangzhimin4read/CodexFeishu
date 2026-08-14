"""Verify the active interpreter matches the complete project lock exactly."""

from __future__ import annotations

import json
import re
import tomllib
from importlib.metadata import PackageNotFoundError, distributions, version
from pathlib import Path

from packaging.utils import canonicalize_name


ROOT = Path(__file__).resolve().parents[1]
PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s]+)$")


def exact_pin(value: str) -> tuple[str, str]:
    match = PIN.fullmatch(value)
    if match is None:
        raise RuntimeError(f"dependency is not an exact pin: {value}")
    return canonicalize_name(match.group(1)), match.group(2)


def main() -> int:
    locked: dict[str, str] = {}
    for raw in (ROOT / "requirements-dev.lock").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, expected = exact_pin(line)
        if name in locked:
            raise RuntimeError(f"duplicate lock entry: {name}")
        locked[name] = expected
    with (ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]
    runtime_locked: dict[str, str] = {}
    for raw in (ROOT / "requirements-runtime.lock").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, expected = exact_pin(line)
        if name in runtime_locked:
            raise RuntimeError(f"duplicate runtime lock entry: {name}")
        if locked.get(name) != expected:
            raise RuntimeError(f"runtime dependency is not identically dev-locked: {name}")
        runtime_locked[name] = expected
    runtime_declared = list(project.get("dependencies", []))
    for declaration in runtime_declared:
        name, expected = exact_pin(declaration)
        if runtime_locked.get(name) != expected:
            raise RuntimeError(f"declared runtime dependency is not runtime-locked: {name}")
    declared = list(runtime_declared)
    declared.extend(project.get("optional-dependencies", {}).get("test", []))
    for declaration in declared:
        name, expected = exact_pin(declaration)
        if locked.get(name) != expected:
            raise RuntimeError(f"declared dependency is not identically locked: {name}")
    mismatches: dict[str, dict[str, str]] = {}
    for name, expected in locked.items():
        try:
            actual = version(name)
        except PackageNotFoundError:
            actual = "NOT_INSTALLED"
        if actual != expected:
            mismatches[name] = {"actual": actual, "expected": expected}
    if mismatches:
        raise RuntimeError(f"active environment differs from lock: {mismatches}")
    installed = {
        canonicalize_name(item.metadata["Name"])
        for item in distributions()
        if item.metadata.get("Name")
    }
    bootstrap_tools = {"pip", "setuptools", "wheel"}
    unexpected = sorted(installed - set(locked) - bootstrap_tools)
    if unexpected:
        raise RuntimeError(f"active environment contains unlocked distributions: {unexpected}")
    print(
        json.dumps(
            {
                "declared_dependencies": len(declared),
                "locked_distributions": len(locked),
                "runtime_locked_distributions": len(runtime_locked),
                "unexpected_distributions": 0,
                "status": "PASS",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
