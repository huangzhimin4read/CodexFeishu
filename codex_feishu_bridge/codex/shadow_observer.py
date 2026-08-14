"""Permanent read-only shadow observer used by the M1 gate."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..runtime_storage import RuntimeStorage, utc_now
from .rollout_observer import IncrementalRolloutReader, RolloutReadError
from .state_discovery import RolloutSource, discover_rollouts


@dataclass(frozen=True, slots=True)
class ShadowObservation:
    sources_seen: int
    records_inserted: int
    ignored_records: int
    failures: tuple[str, ...]


class ShadowObserver:
    """Observe allowlisted rollout files without any provider dependency."""

    def __init__(self, storage: RuntimeStorage) -> None:
        if storage.sink_mode != "shadow_only":
            raise PermissionError("shadow observer requires a permanent shadow_only database")
        self.storage = storage
        self.reader = IncrementalRolloutReader()

    def observe_source(self, source: RolloutSource) -> tuple[int, int]:
        cursor = self.storage.cursor_for(str(source.path))
        batch = self.reader.read(
            source.path, cursor, expected_thread_id=source.thread_id
        )
        inserted = self.storage.store_rollout_batch(batch)
        return inserted, batch.ignored_records

    def observe_once(
        self,
        *,
        codex_home: Path,
        project_allowlist: tuple[Path, ...],
        thread_allowlist: frozenset[str],
    ) -> ShadowObservation:
        sources = discover_rollouts(
            codex_home,
            project_allowlist=project_allowlist,
            thread_allowlist=thread_allowlist,
        )
        inserted = 0
        ignored = 0
        failures: list[str] = []
        for source in sources:
            try:
                added, skipped = self.observe_source(source)
            except RolloutReadError as exc:
                # Do not advance the cursor. Store a body-free diagnostic only.
                failures.append(f"{source.path.name}: {type(exc).__name__}")
                self.storage.connection.execute(
                    "INSERT INTO dead_letters(category,record_hash,reason,created_at) VALUES(?,?,?,?)",
                    ("shadow_source", source.path.name, type(exc).__name__, utc_now()),
                )
                continue
            inserted += added
            ignored += skipped
        self.storage.upsert_runtime_metadata("shadow_last_observed_at", utc_now())
        self.storage.upsert_runtime_metadata("shadow_source_count", len(sources))
        return ShadowObservation(len(sources), inserted, ignored, tuple(failures))
