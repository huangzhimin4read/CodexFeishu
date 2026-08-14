"""Seven-day owner-operated pilot measurement and objective decision."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..runtime_storage import RuntimeStorage, utc_now


@dataclass(frozen=True, slots=True)
class PilotDecision:
    passed: bool
    elapsed_days: float
    reasons: tuple[str, ...]


class PilotTracker:
    def __init__(self, storage: RuntimeStorage) -> None:
        self.storage = storage

    def start(self) -> str:
        now = utc_now()
        self.storage.connection.execute(
            "INSERT OR IGNORE INTO runtime_metadata(key,value) VALUES('pilot_started_at',?)",
            (json.dumps(now),),
        )
        row = self.storage.connection.execute(
            "SELECT value FROM runtime_metadata WHERE key='pilot_started_at'"
        ).fetchone()
        return str(json.loads(row[0]))

    def sample(self, latency_ms: int | None = None) -> None:
        counts = {
            "final_confirmed": "SELECT COUNT(*) FROM provider_outbox WHERE operation='final' AND state='confirmed'",
            "final_undelivered": "SELECT COUNT(*) FROM provider_outbox WHERE operation='final' AND state='final_undelivered'",
            "delivery_indeterminate": "SELECT COUNT(*) FROM provider_outbox WHERE state='delivery_indeterminate'",
            "dispatch_unknown": "SELECT COUNT(*) FROM dispatch_records WHERE state='outcome_unknown'",
            "approval_unknown": "SELECT COUNT(*) FROM approval_requests WHERE state='outcome_unknown'",
            "dead_letters": "SELECT COUNT(*) FROM dead_letters",
        }
        values = {
            name: int(self.storage.connection.execute(query).fetchone()[0])
            for name, query in counts.items()
        }
        self.storage.connection.execute(
            "INSERT INTO pilot_samples(captured_at,final_confirmed,final_undelivered,delivery_indeterminate,"
            "dispatch_unknown,approval_unknown,dead_letters,latency_ms) VALUES(?,?,?,?,?,?,?,?)",
            (
                utc_now(),
                values["final_confirmed"],
                values["final_undelivered"],
                values["delivery_indeterminate"],
                values["dispatch_unknown"],
                values["approval_unknown"],
                values["dead_letters"],
                latency_ms,
            ),
        )

    def decide(self) -> PilotDecision:
        row = self.storage.connection.execute(
            "SELECT value FROM runtime_metadata WHERE key='pilot_started_at'"
        ).fetchone()
        if row is None:
            return PilotDecision(False, 0.0, ("pilot_not_started",))
        started = datetime.fromisoformat(json.loads(row[0]).replace("Z", "+00:00"))
        elapsed = (datetime.now(UTC) - started).total_seconds() / 86400
        latest = self.storage.connection.execute(
            "SELECT * FROM pilot_samples ORDER BY sample_id DESC LIMIT 1"
        ).fetchone()
        reasons: list[str] = []
        if elapsed < 7:
            reasons.append("less_than_seven_days")
        if latest is None:
            reasons.append("no_pilot_samples")
        else:
            for field in (
                "final_undelivered",
                "delivery_indeterminate",
                "dispatch_unknown",
                "approval_unknown",
            ):
                if int(latest[field]):
                    reasons.append(field)
        return PilotDecision(not reasons, elapsed, tuple(reasons))
