"""Stage exact installed runtime distributions into a frozen application root."""

from __future__ import annotations

import argparse
import importlib.metadata
import re
import shutil
from pathlib import Path


REQUIREMENT = re.compile(r"^([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+!-]+)$")


def requirements(path: Path) -> tuple[tuple[str, str], ...]:
    values: list[tuple[str, str]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        match = REQUIREMENT.fullmatch(value)
        if match is None:
            raise RuntimeError(f"unsupported lock entry at line {number}: {value}")
        values.append((match.group(1), match.group(2)))
    return tuple(values)


def stage(lock: Path, target: Path) -> int:
    target = target.resolve()
    target.mkdir(parents=True, exist_ok=True)
    copied = 0
    for name, expected in requirements(lock):
        distribution = importlib.metadata.distribution(name)
        if distribution.version != expected:
            raise RuntimeError(
                f"installed {name} version {distribution.version} differs from lock {expected}"
            )
        base = Path(distribution.locate_file("")).resolve()
        files = distribution.files
        if files is None:
            raise RuntimeError(f"installed distribution has no file manifest: {name}")
        for entry in files:
            source = Path(distribution.locate_file(entry)).resolve()
            try:
                relative = source.relative_to(base)
            except ValueError:
                # Console entrypoints outside site-packages are not runtime imports.
                continue
            if not source.is_file():
                continue
            destination = (target / relative).resolve()
            try:
                destination.relative_to(target)
            except ValueError as exc:
                raise RuntimeError(f"distribution path escapes target: {name}") from exc
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied += 1
    return copied


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    args = parser.parse_args()
    copied = stage(args.lock.resolve(), args.target.resolve())
    print(f"staged_files={copied}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
