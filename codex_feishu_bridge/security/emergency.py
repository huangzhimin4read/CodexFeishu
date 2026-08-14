"""Durable soft-quiesce and hard-stop control plane."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..runtime_storage import RuntimeStorage, utc_now


@dataclass(frozen=True, slots=True)
class StopResult:
    kill_generation: int
    grants_revoked: int
    approvals_cancelled: int


class EmergencyController:
    def __init__(self, storage: RuntimeStorage, terminate_workers: Callable[[], None]) -> None:
        self.storage = storage
        self.terminate_workers = terminate_workers

    def soft_quiesce(self, reason: str) -> None:
        with self.storage.immediate() as connection:
            connection.execute(
                "UPDATE service_state SET process_state='quiescing',updated_at=? WHERE singleton=1",
                (utc_now(),),
            )
            connection.execute(
                "INSERT INTO circuit_breakers(breaker_name,state,reason,updated_at) "
                "VALUES('ingress','open',?,?) ON CONFLICT(breaker_name) DO UPDATE SET "
                "state='open',reason=excluded.reason,updated_at=excluded.updated_at",
                (reason, utc_now()),
            )

    def hard_stop(self, reason: str) -> StopResult:
        with self.storage.immediate() as connection:
            row = connection.execute(
                "SELECT kill_generation FROM service_state WHERE singleton=1"
            ).fetchone()
            generation = int(row[0]) + 1
            connection.execute(
                "UPDATE service_state SET process_state='hard_stopping',kill_generation=?,updated_at=? "
                "WHERE singleton=1",
                (generation, utc_now()),
            )
            grants = connection.execute("DELETE FROM active_grants").rowcount
            connection.execute(
                "UPDATE remote_task_grants SET state='revoked',updated_at=? WHERE state='active'",
                (utc_now(),),
            )
            approvals = connection.execute(
                "UPDATE approval_requests SET state='cancelled',updated_at=? "
                "WHERE state IN ('issued','action_committed')",
                (utc_now(),),
            ).rowcount
            connection.execute(
                "UPDATE approval_actions SET consumed_at=COALESCE(consumed_at,?) WHERE consumed_at IS NULL",
                (utc_now(),),
            )
            connection.execute(
                "INSERT INTO circuit_breakers(breaker_name,state,reason,updated_at) "
                "VALUES('global','open',?,?) ON CONFLICT(breaker_name) DO UPDATE SET "
                "state='open',reason=excluded.reason,updated_at=excluded.updated_at",
                (reason, utc_now()),
            )
        # Process termination is deliberately outside the DB transaction. The
        # generation fence is durable before workers receive the kill signal.
        self.terminate_workers()
        self.storage.connection.execute(
            "UPDATE service_state SET process_state='stopped',updated_at=? WHERE singleton=1",
            (utc_now(),),
        )
        return StopResult(generation, grants, approvals)
