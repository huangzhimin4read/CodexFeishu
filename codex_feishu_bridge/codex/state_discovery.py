"""Read-only discovery of opted-in Codex rollout sources."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


class DiscoveryError(RuntimeError):
    """A persisted source cannot be identified without guessing."""


@dataclass(frozen=True, slots=True)
class RolloutSource:
    path: Path
    thread_id: str
    project_root: Path
    rollout_version: str
    modified_ns: int
    project_id: str | None = None
    project_name: str | None = None
    activity_ms: int | None = None
    task_title: str | None = None


def _session_metadata(path: Path) -> dict[str, object]:
    with path.open("rb") as handle:
        for line_number in range(1, 65):
            raw = handle.readline()
            if not raw:
                break
            if not raw.strip():
                continue
            try:
                record = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise DiscoveryError(f"invalid JSONL prefix in {path} at line {line_number}") from exc
            if record.get("type") == "session_meta":
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    raise DiscoveryError(f"invalid session metadata in {path}")
                return payload
    raise DiscoveryError(f"session metadata not found in {path}")


def discover_rollouts(
    codex_home: Path,
    *,
    project_allowlist: tuple[Path, ...],
    thread_allowlist: frozenset[str],
) -> tuple[RolloutSource, ...]:
    """Return only explicitly allowed thread/project sources.

    Discovery reads the small JSONL prefix required for source identity. Message
    bodies are not inspected here and no source file is opened for writing.
    """

    root = codex_home.resolve()
    allowed_projects = {path.resolve() for path in project_allowlist}
    found: list[RolloutSource] = []
    for path in root.glob("sessions/**/*.jsonl"):
        try:
            if not path.is_file():
                continue
            metadata = _session_metadata(path)
            thread_id = str(metadata.get("id") or metadata.get("thread_id") or "")
            cwd_value = metadata.get("cwd") or metadata.get("project_root")
            if not thread_id or not isinstance(cwd_value, str):
                continue
            project_root = Path(cwd_value).resolve()
            if thread_id not in thread_allowlist or project_root not in allowed_projects:
                continue
            version = str(metadata.get("rollout_version", metadata.get("version", "1")))
            stat = path.stat()
        except FileNotFoundError:
            # Codex may archive, move, or recreate a rollout while discovery is
            # enumerating the sessions tree.  The next service loop will read
            # the authoritative path; one vanished candidate must not stop the
            # entire bridge.  Other I/O and metadata errors remain fail closed.
            continue
        found.append(
            RolloutSource(
                path=path.resolve(),
                thread_id=thread_id,
                project_root=project_root,
                rollout_version=version,
                modified_ns=stat.st_mtime_ns,
            )
        )
    return tuple(sorted(found, key=lambda item: (item.modified_ns, str(item.path))))


def newest_per_thread(sources: tuple[RolloutSource, ...]) -> Iterator[RolloutSource]:
    latest: dict[str, RolloutSource] = {}
    for source in sources:
        current = latest.get(source.thread_id)
        if current is None or (source.modified_ns, str(source.path)) > (
            current.modified_ns,
            str(current.path),
        ):
            latest[source.thread_id] = source
    yield from sorted(latest.values(), key=lambda item: item.thread_id)
