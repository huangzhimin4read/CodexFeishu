"""Authority and uncertainty checks before promoting a restored database."""

from __future__ import annotations

from dataclasses import dataclass

from ..runtime_storage import RuntimeStorage, utc_now


class RecoveryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RecoveryProof:
    provider_history_reconciled: bool
    codex_history_reconciled: bool
    tombstones_verified: bool
    identity_binding_verified: bool


def promote_recovered_database(storage: RuntimeStorage, proof: RecoveryProof) -> int:
    if not all((
        proof.provider_history_reconciled,
        proof.codex_history_reconciled,
        proof.tombstones_verified,
        proof.identity_binding_verified,
    )):
        raise RecoveryError("recovery authority proof is incomplete")
    restore = storage.connection.execute(
        "SELECT value FROM runtime_metadata WHERE key='restore_mode'"
    ).fetchone()
    if restore is None or "reconciliation_only" not in str(restore[0]):
        raise RecoveryError("database is not in reconciliation-only restore mode")
    uncertain = sum(
        int(storage.connection.execute(query).fetchone()[0])
        for query in (
            "SELECT COUNT(*) FROM provider_outbox WHERE state IN ('leased','unknown','delivery_indeterminate')",
            "SELECT COUNT(*) FROM dispatch_records WHERE state='outcome_unknown'",
            "SELECT COUNT(*) FROM approval_requests WHERE state='outcome_unknown'",
        )
    )
    if uncertain:
        raise RecoveryError("unresolved uncertain work prevents restore promotion")
    with storage.immediate() as connection:
        connection.execute(
            "UPDATE runtime_metadata SET value='\"promoted\"' WHERE key='restore_mode'"
        )
        connection.execute(
            "UPDATE service_state SET fencing_token=fencing_token+1,kill_generation=kill_generation+1,"
            "process_state='stopped',updated_at=? WHERE singleton=1",
            (utc_now(),),
        )
        connection.execute(
            "UPDATE identity_bindings SET binding_epoch=binding_epoch+1,state='active',updated_at=?",
            (utc_now(),),
        )
        connection.execute(
            "UPDATE chat_sequences SET active_binding_epoch=active_binding_epoch+1,"
            "selection_state='selection_indeterminate',updated_at=?",
            (utc_now(),),
        )
        row = connection.execute(
            "SELECT fencing_token FROM service_state WHERE singleton=1"
        ).fetchone()
    return int(row[0])
