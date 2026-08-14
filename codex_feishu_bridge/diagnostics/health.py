"""Atomic body-free health/status snapshots."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..runtime_storage import RuntimeStorage, utc_now


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    generated_at: str
    process_state: str
    fencing_token: int
    kill_generation: int
    outbox: dict[str, int]
    ingress_indeterminate: int
    dead_letters: int
    open_breakers: tuple[str, ...]
    source_count: int
    remote_connection_state: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "process_state": self.process_state,
            "fencing_token": self.fencing_token,
            "kill_generation": self.kill_generation,
            "outbox": self.outbox,
            "ingress_indeterminate": self.ingress_indeterminate,
            "dead_letters": self.dead_letters,
            "open_breakers": list(self.open_breakers),
            "source_count": self.source_count,
            "remote_connection_state": self.remote_connection_state,
        }


def capture_health(
    storage: RuntimeStorage, *, remote_connection_state: str | None = None
) -> HealthSnapshot:
    state = storage.connection.execute(
        "SELECT process_state,fencing_token,kill_generation FROM service_state WHERE singleton=1"
    ).fetchone()
    outbox = {
        row["state"]: int(row["count"])
        for row in storage.connection.execute(
            "SELECT state,COUNT(*) AS count FROM provider_outbox GROUP BY state"
        )
    }
    breakers = tuple(
        row[0]
        for row in storage.connection.execute(
            "SELECT breaker_name FROM circuit_breakers WHERE state='open' ORDER BY breaker_name"
        )
    )
    return HealthSnapshot(
        generated_at=utc_now(),
        process_state=str(state["process_state"]),
        fencing_token=int(state["fencing_token"]),
        kill_generation=int(state["kill_generation"]),
        outbox=outbox,
        ingress_indeterminate=int(
            storage.connection.execute(
                "SELECT COUNT(*) FROM ingress_messages WHERE routing_state='routing_indeterminate'"
            ).fetchone()[0]
        ),
        dead_letters=int(storage.connection.execute("SELECT COUNT(*) FROM dead_letters").fetchone()[0]),
        open_breakers=breakers,
        source_count=int(storage.connection.execute("SELECT COUNT(*) FROM source_cursors").fetchone()[0]),
        remote_connection_state=remote_connection_state,
    )


def write_status_atomic(path: Path, snapshot: HealthSnapshot) -> None:
    destination = path.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + f".{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(snapshot.as_dict(), handle, ensure_ascii=False, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, destination)
