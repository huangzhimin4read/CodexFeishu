import sqlite3
from pathlib import Path

import pytest

from codex_feishu_bridge.models import (
    ApprovalState,
    DeliveryState,
    DispatchState,
    EventKind,
    NormalizedEvent,
    OwnershipState,
    RolloutBatch,
    SourceCursor,
    TurnState,
)
from codex_feishu_bridge.storage import BridgeStorage, InvalidTransition, StorageError


def test_required_sqlite_pragmas_and_offline_outbox_guard(tmp_path: Path) -> None:
    with BridgeStorage(tmp_path / "m0.db") as storage:
        storage.initialize()
        pragmas = storage.pragmas()
        assert str(pragmas["journal_mode"]).lower() == "wal"
        assert pragmas["synchronous"] == 2
        assert pragmas["foreign_keys"] == 1
        assert int(pragmas["busy_timeout"]) == 5000
        with pytest.raises(sqlite3.IntegrityError, match="offline_fixture"):
            storage.connection.execute(
                "INSERT INTO outbox(logical_key,endpoint,state,created_at) VALUES(?,?,?,?)",
                ("k", "send", "pending", "2026-08-12T00:00:00Z"),
            )


def test_dispatch_transitions_are_compare_and_swap(tmp_path: Path) -> None:
    with BridgeStorage(tmp_path / "m0.db") as storage:
        storage.initialize()
        storage.create_dispatch_attempt("attempt-1")
        storage.transition_dispatch(
            "attempt-1", DispatchState.RECEIVED, DispatchState.CLAIMED
        )
        with pytest.raises(InvalidTransition, match="forbidden"):
            storage.transition_dispatch(
                "attempt-1", DispatchState.CLAIMED, DispatchState.COMPLETED
            )
        with pytest.raises(InvalidTransition, match="compare-and-swap"):
            storage.transition_dispatch(
                "attempt-1", DispatchState.RECEIVED, DispatchState.CLAIMED
            )


def test_approval_requires_response_hash_before_sending(tmp_path: Path) -> None:
    with BridgeStorage(tmp_path / "m0.db") as storage:
        storage.initialize()
        storage.create_approval("approval-1", "request-hash")
        storage.transition_approval(
            "approval-1", ApprovalState.ISSUED, ApprovalState.ACTION_COMMITTED
        )
        with pytest.raises(InvalidTransition, match="response hash"):
            storage.transition_approval(
                "approval-1",
                ApprovalState.ACTION_COMMITTED,
                ApprovalState.RESPONSE_SENDING,
            )
        storage.transition_approval(
            "approval-1",
            ApprovalState.ACTION_COMMITTED,
            ApprovalState.RESPONSE_SENDING,
            response_hash="response-hash",
        )
        with pytest.raises(InvalidTransition, match="cannot be changed"):
            storage.transition_approval(
                "approval-1",
                ApprovalState.RESPONSE_SENDING,
                ApprovalState.RESOLVED,
                response_hash="overwritten",
            )
        storage.transition_approval(
            "approval-1", ApprovalState.RESPONSE_SENDING, ApprovalState.RESOLVED
        )
        assert storage.connection.execute(
            "SELECT response_hash FROM approval_requests WHERE approval_id='approval-1'"
        ).fetchone()[0] == "response-hash"


def test_transaction_rolls_back_all_rows(tmp_path: Path) -> None:
    with BridgeStorage(tmp_path / "m0.db") as storage:
        storage.initialize()
        with pytest.raises(RuntimeError):
            with storage.transaction() as connection:
                connection.execute(
                    "INSERT INTO dead_letters(category,record_hash,reason,created_at) "
                    "VALUES('fixture','hash','reason','2026-08-12T00:00:00Z')"
                )
                raise RuntimeError("inject failure")
        assert storage.connection.execute("SELECT COUNT(*) FROM dead_letters").fetchone()[0] == 0


def test_logical_key_content_conflict_rolls_back_cursor(tmp_path: Path) -> None:
    first = NormalizedEvent(
        thread_id="t",
        turn_id="u",
        item_id="i",
        kind=EventKind.COMMENTARY,
        revision=0,
        text="first",
        source_type="response_item",
    )
    conflicting = NormalizedEvent(
        thread_id="t",
        turn_id="u",
        item_id="i",
        kind=EventKind.COMMENTARY,
        revision=0,
        text="changed",
        source_type="event_msg",
    )
    cursor_1 = SourceCursor("fixture", "file", 10, "hash-1")
    cursor_2 = SourceCursor("fixture", "file", 20, "hash-2")
    with BridgeStorage(tmp_path / "m0.db") as storage:
        storage.initialize()
        storage.store_rollout_batch(RolloutBatch((first,), cursor_1))
        with pytest.raises(StorageError, match="conflicts"):
            storage.store_rollout_batch(RolloutBatch((conflicting,), cursor_2))
        assert storage.cursor_for("fixture") == cursor_1
        assert storage.item_count() == 1


def test_ownership_turn_and_delivery_transitions_are_independent(tmp_path: Path) -> None:
    with BridgeStorage(tmp_path / "m0.db") as storage:
        storage.initialize()
        storage.create_thread("thread-1", OwnershipState.BRIDGE_IDLE)
        storage.create_turn("turn-1", "thread-1")
        storage.transition_ownership(
            "thread-1", OwnershipState.BRIDGE_IDLE, OwnershipState.BRIDGE_OWNED
        )
        storage.transition_turn("turn-1", TurnState.CREATED, TurnState.RUNNING)
        storage.transition_delivery(
            "turn-1", DeliveryState.PENDING, DeliveryState.RETRYABLE
        )
        with pytest.raises(InvalidTransition, match="ownership"):
            storage.transition_ownership(
                "thread-1",
                OwnershipState.BRIDGE_OWNED,
                OwnershipState.DESKTOP_MIRROR_ONLY,
            )
        with pytest.raises(InvalidTransition, match="turn"):
            storage.transition_turn("turn-1", TurnState.RUNNING, TurnState.CREATED)
        storage.transition_turn("turn-1", TurnState.RUNNING, TurnState.COMPLETED)
        storage.transition_delivery(
            "turn-1", DeliveryState.RETRYABLE, DeliveryState.CONFIRMED
        )


def test_desktop_mirror_ownership_is_terminal(tmp_path: Path) -> None:
    with BridgeStorage(tmp_path / "m0.db") as storage:
        storage.initialize()
        storage.create_thread("desktop", OwnershipState.DESKTOP_MIRROR_ONLY)
        with pytest.raises(InvalidTransition, match="forbidden"):
            storage.transition_ownership(
                "desktop",
                OwnershipState.DESKTOP_MIRROR_ONLY,
                OwnershipState.BRIDGE_IDLE,
            )
