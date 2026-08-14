"""SQLite persistence and compare-and-swap state transitions."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from .models import (
    ApprovalState,
    DeliveryState,
    DispatchState,
    OwnershipState,
    RolloutBatch,
    SourceCursor,
    TurnState,
)


SCHEMA_VERSION = 1


class StorageError(RuntimeError):
    """Base class for persistence failures."""


class InvalidTransition(StorageError):
    """Raised when a state transition is not permitted or loses a CAS race."""


_DISPATCH_TRANSITIONS: dict[DispatchState, frozenset[DispatchState]] = {
    DispatchState.RECEIVED: frozenset({DispatchState.CLAIMED}),
    DispatchState.CLAIMED: frozenset({DispatchState.DISPATCHING}),
    DispatchState.DISPATCHING: frozenset(
        {DispatchState.ACCEPTED, DispatchState.OUTCOME_UNKNOWN}
    ),
    DispatchState.ACCEPTED: frozenset(
        {DispatchState.COMPLETED, DispatchState.OUTCOME_UNKNOWN}
    ),
    DispatchState.OUTCOME_UNKNOWN: frozenset({DispatchState.COMPLETED}),
    DispatchState.COMPLETED: frozenset(),
}

_APPROVAL_TRANSITIONS: dict[ApprovalState, frozenset[ApprovalState]] = {
    ApprovalState.ISSUED: frozenset(
        {
            ApprovalState.ACTION_COMMITTED,
            ApprovalState.REJECTED,
            ApprovalState.EXPIRED,
            ApprovalState.CANCELLED,
        }
    ),
    ApprovalState.ACTION_COMMITTED: frozenset(
        {ApprovalState.RESPONSE_SENDING, ApprovalState.CANCELLED}
    ),
    ApprovalState.RESPONSE_SENDING: frozenset(
        {
            ApprovalState.RESOLVED,
            ApprovalState.REJECTED,
            ApprovalState.OUTCOME_UNKNOWN,
        }
    ),
    ApprovalState.OUTCOME_UNKNOWN: frozenset(
        {ApprovalState.RESOLVED, ApprovalState.REJECTED}
    ),
    ApprovalState.RESOLVED: frozenset(),
    ApprovalState.REJECTED: frozenset(),
    ApprovalState.EXPIRED: frozenset(),
    ApprovalState.CANCELLED: frozenset(),
}

_OWNERSHIP_TRANSITIONS: dict[OwnershipState, frozenset[OwnershipState]] = {
    OwnershipState.DESKTOP_MIRROR_ONLY: frozenset(),
    OwnershipState.BRIDGE_IDLE: frozenset(
        {OwnershipState.BRIDGE_OWNED, OwnershipState.UNKNOWN}
    ),
    OwnershipState.BRIDGE_OWNED: frozenset(
        {OwnershipState.BRIDGE_IDLE, OwnershipState.UNKNOWN}
    ),
    OwnershipState.UNKNOWN: frozenset({OwnershipState.BRIDGE_IDLE}),
}

_TURN_TRANSITIONS: dict[TurnState, frozenset[TurnState]] = {
    TurnState.CREATED: frozenset({TurnState.RUNNING, TurnState.OUTCOME_UNKNOWN}),
    TurnState.RUNNING: frozenset(
        {
            TurnState.COMPLETED,
            TurnState.FAILED,
            TurnState.INTERRUPTED,
            TurnState.OUTCOME_UNKNOWN,
        }
    ),
    TurnState.OUTCOME_UNKNOWN: frozenset(
        {TurnState.RUNNING, TurnState.COMPLETED, TurnState.FAILED, TurnState.INTERRUPTED}
    ),
    TurnState.COMPLETED: frozenset(),
    TurnState.FAILED: frozenset(),
    TurnState.INTERRUPTED: frozenset(),
}

_DELIVERY_TRANSITIONS: dict[DeliveryState, frozenset[DeliveryState]] = {
    DeliveryState.PENDING: frozenset(
        {
            DeliveryState.CONFIRMED,
            DeliveryState.RETRYABLE,
            DeliveryState.PERMANENT,
            DeliveryState.UNKNOWN,
            DeliveryState.FINAL_UNDELIVERED,
        }
    ),
    DeliveryState.RETRYABLE: frozenset(
        {
            DeliveryState.CONFIRMED,
            DeliveryState.RETRYABLE,
            DeliveryState.PERMANENT,
            DeliveryState.UNKNOWN,
            DeliveryState.FINAL_UNDELIVERED,
        }
    ),
    DeliveryState.UNKNOWN: frozenset(
        {DeliveryState.CONFIRMED, DeliveryState.PERMANENT, DeliveryState.FINAL_UNDELIVERED}
    ),
    DeliveryState.CONFIRMED: frozenset(),
    DeliveryState.PERMANENT: frozenset(),
    DeliveryState.FINAL_UNDELIVERED: frozenset(),
}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class _LockedConnection:
    """Serialize one SQLite connection across provider callback threads."""

    def __init__(self, connection: sqlite3.Connection, lock: threading.RLock) -> None:
        self._connection = connection
        self._lock = lock

    def execute(self, *args: object, **kwargs: object) -> sqlite3.Cursor:
        with self._lock:
            return self._connection.execute(*args, **kwargs)

    def executescript(self, script: str) -> sqlite3.Cursor:
        with self._lock:
            return self._connection.executescript(script)

    def backup(self, target: object, *args: object, **kwargs: object) -> None:
        destination = (
            target._connection if isinstance(target, _LockedConnection) else target
        )
        with self._lock:
            self._connection.backup(destination, *args, **kwargs)

    @property
    def in_transaction(self) -> bool:
        with self._lock:
            return self._connection.in_transaction

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __getattr__(self, name: str) -> object:
        return getattr(self._connection, name)


class BridgeStorage:
    """Owns the M0 database and enforces fail-closed state transitions."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection_lock = threading.RLock()
        raw_connection = sqlite3.connect(
            self.path, isolation_level=None, check_same_thread=False
        )
        raw_connection.row_factory = sqlite3.Row
        self.connection = _LockedConnection(raw_connection, self._connection_lock)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> BridgeStorage:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection_lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
                self.connection.execute("COMMIT")
            except BaseException:
                if self.connection.in_transaction:
                    self.connection.execute("ROLLBACK")
                raise

    def execute_schema_migration(self, script: str) -> None:
        """Execute a DDL migration atomically, including injected DDL faults."""
        with self._connection_lock:
            try:
                self.connection.executescript("BEGIN IMMEDIATE;\n" + script + "\nCOMMIT;")
            except BaseException:
                if self.connection.in_transaction:
                    self.connection.execute("ROLLBACK")
                raise

    def initialize(self) -> None:
        self.execute_schema_migration(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS thread_bindings (
                thread_id TEXT PRIMARY KEY,
                ownership_state TEXT NOT NULL CHECK (ownership_state IN (
                    'desktop_mirror_only','bridge_idle','bridge_owned','unknown'
                )),
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS turns (
                turn_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL REFERENCES thread_bindings(thread_id),
                turn_state TEXT NOT NULL CHECK (turn_state IN (
                    'created','running','completed','failed','interrupted','outcome_unknown'
                )),
                delivery_state TEXT NOT NULL CHECK (delivery_state IN (
                    'pending','confirmed','retryable','permanent','unknown','final_undelivered'
                )),
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS items (
                logical_key TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                event_kind TEXT NOT NULL CHECK (event_kind IN ('commentary','final_answer')),
                revision INTEGER NOT NULL CHECK (revision >= 0),
                content_hash TEXT NOT NULL,
                text TEXT NOT NULL,
                source_type TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS source_cursors (
                source_path TEXT PRIMARY KEY,
                file_id TEXT NOT NULL,
                committed_offset INTEGER NOT NULL CHECK (committed_offset >= 0),
                last_record_hash TEXT,
                schema_version TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS feishu_messages (
                logical_message_id TEXT PRIMARY KEY,
                message_id TEXT UNIQUE,
                delivery_state TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS inbound_commands (
                command_id TEXT PRIMARY KEY,
                message_id TEXT UNIQUE NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS outbox (
                outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
                logical_key TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN (
                    'pending','confirmed','retryable','permanent','unknown','final_undelivered'
                )),
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS processed_feishu_events (
                tenant_key TEXT NOT NULL,
                app_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                PRIMARY KEY (tenant_key, app_id, message_id)
            );

            CREATE TABLE IF NOT EXISTS thread_leases (
                thread_id TEXT PRIMARY KEY,
                instance_id TEXT NOT NULL,
                fencing_token INTEGER NOT NULL,
                expires_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS dead_letters (
                dead_letter_id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                record_hash TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS dispatch_attempts (
                dispatch_attempt_id TEXT PRIMARY KEY,
                state TEXT NOT NULL CHECK (state IN (
                    'received','claimed','dispatching','accepted','outcome_unknown','completed'
                )),
                response_hash TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS execution_profiles (
                profile_id TEXT PRIMARY KEY,
                profile_hash TEXT UNIQUE NOT NULL,
                canonical_json BLOB NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS circuit_breakers (
                breaker_name TEXT PRIMARY KEY,
                state TEXT NOT NULL CHECK (state IN ('closed','open')),
                reason TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS approval_requests (
                approval_id TEXT PRIMARY KEY,
                state TEXT NOT NULL CHECK (state IN (
                    'issued','action_committed','response_sending','resolved','rejected',
                    'expired','cancelled','outcome_unknown'
                )),
                request_hash TEXT NOT NULL,
                response_hash TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS active_grants (
                grant_id TEXT PRIMARY KEY,
                server_epoch TEXT NOT NULL,
                connection_epoch TEXT NOT NULL,
                session_id TEXT NOT NULL,
                kill_generation INTEGER NOT NULL,
                expires_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS executed_command_tombstones (
                tombstone_key TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                target_thread_id TEXT NOT NULL,
                dispatch_attempt_id TEXT NOT NULL,
                retain_until TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_chain (
                sequence INTEGER PRIMARY KEY,
                previous_hmac TEXT,
                record_hash TEXT NOT NULL,
                record_hmac TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TRIGGER IF NOT EXISTS prevent_offline_outbox
            BEFORE INSERT ON outbox
            WHEN (SELECT value FROM metadata WHERE key='sink_mode')='offline_fixture'
            BEGIN
                SELECT RAISE(ABORT, 'offline_fixture sink cannot create outbox rows');
            END;
            CREATE TRIGGER IF NOT EXISTS protect_approval_response_hash
            BEFORE UPDATE OF response_hash ON approval_requests
            WHEN OLD.response_hash IS NOT NULL AND NEW.response_hash IS NOT OLD.response_hash
            BEGIN
                SELECT RAISE(ABORT, 'approval response hash is immutable');
            END;
            """
        )
        with self.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                (SCHEMA_VERSION, _utc_now()),
            )
            connection.executemany(
                "INSERT INTO metadata(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (
                    ("schema_version", str(SCHEMA_VERSION)),
                    ("sink_mode", "offline_fixture"),
                    ("sync_enabled", "0"),
                ),
            )

    def pragmas(self) -> dict[str, object]:
        return {
            "journal_mode": self.connection.execute("PRAGMA journal_mode").fetchone()[0],
            "synchronous": self.connection.execute("PRAGMA synchronous").fetchone()[0],
            "foreign_keys": self.connection.execute("PRAGMA foreign_keys").fetchone()[0],
            "busy_timeout": self.connection.execute("PRAGMA busy_timeout").fetchone()[0],
        }

    def cursor_for(self, source_path: str) -> SourceCursor | None:
        row = self.connection.execute(
            "SELECT source_path,file_id,committed_offset,last_record_hash,schema_version "
            "FROM source_cursors WHERE source_path=?",
            (source_path,),
        ).fetchone()
        if row is None:
            return None
        return SourceCursor(**dict(row))

    def store_rollout_batch(self, batch: RolloutBatch) -> int:
        inserted = 0
        with self.transaction() as connection:
            current = connection.execute(
                "SELECT file_id,committed_offset,last_record_hash,schema_version "
                "FROM source_cursors WHERE source_path=?",
                (batch.cursor.source_path,),
            ).fetchone()
            if current is not None:
                if current["file_id"] != batch.cursor.file_id:
                    raise StorageError("source file identity changed")
                if batch.cursor.committed_offset < current["committed_offset"]:
                    raise StorageError("source cursor cannot move backwards")
                if batch.cursor.schema_version != current["schema_version"]:
                    raise StorageError("source schema version cannot change in place")
                if (
                    batch.cursor.committed_offset == current["committed_offset"]
                    and batch.cursor.last_record_hash != current["last_record_hash"]
                ):
                    raise StorageError("same source offset has a conflicting record hash")
            if batch.cursor.committed_offset > 0 and not batch.cursor.last_record_hash:
                raise StorageError("positive source offset requires a record hash")
            for event in batch.events:
                result = connection.execute(
                    "INSERT OR IGNORE INTO items("
                    "logical_key,thread_id,turn_id,item_id,event_kind,revision,content_hash,text,source_type,created_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        event.logical_key,
                        event.thread_id,
                        event.turn_id,
                        event.item_id,
                        event.kind.value,
                        event.revision,
                        event.content_hash,
                        event.text,
                        event.source_type,
                        _utc_now(),
                    ),
                )
                if result.rowcount == 1:
                    inserted += 1
                else:
                    existing = connection.execute(
                        "SELECT thread_id,turn_id,item_id,event_kind,revision,content_hash "
                        "FROM items WHERE logical_key=?",
                        (event.logical_key,),
                    ).fetchone()
                    expected = (
                        event.thread_id,
                        event.turn_id,
                        event.item_id,
                        event.kind.value,
                        event.revision,
                        event.content_hash,
                    )
                    actual = tuple(existing) if existing is not None else None
                    if actual != expected:
                        raise StorageError(
                            "logical event key conflicts with persisted content or identity"
                        )
            connection.execute(
                "INSERT INTO source_cursors("
                "source_path,file_id,committed_offset,last_record_hash,schema_version,updated_at"
                ") VALUES(?,?,?,?,?,?) ON CONFLICT(source_path) DO UPDATE SET "
                "file_id=excluded.file_id,committed_offset=excluded.committed_offset,"
                "last_record_hash=excluded.last_record_hash,schema_version=excluded.schema_version,"
                "updated_at=excluded.updated_at",
                (
                    batch.cursor.source_path,
                    batch.cursor.file_id,
                    batch.cursor.committed_offset,
                    batch.cursor.last_record_hash,
                    batch.cursor.schema_version,
                    _utc_now(),
                ),
            )
        return inserted

    def item_count(self) -> int:
        return self.connection.execute("SELECT COUNT(*) FROM items").fetchone()[0]

    def create_thread(self, thread_id: str, ownership: OwnershipState) -> None:
        self.connection.execute(
            "INSERT INTO thread_bindings(thread_id,ownership_state,updated_at) VALUES(?,?,?)",
            (thread_id, ownership.value, _utc_now()),
        )

    def transition_ownership(
        self, thread_id: str, expected: OwnershipState, target: OwnershipState
    ) -> None:
        if target not in _OWNERSHIP_TRANSITIONS[expected]:
            raise InvalidTransition(f"ownership {expected.value} -> {target.value} is forbidden")
        result = self.connection.execute(
            "UPDATE thread_bindings SET ownership_state=?,updated_at=? "
            "WHERE thread_id=? AND ownership_state=?",
            (target.value, _utc_now(), thread_id, expected.value),
        )
        if result.rowcount != 1:
            raise InvalidTransition("ownership transition lost compare-and-swap")

    def create_turn(self, turn_id: str, thread_id: str) -> None:
        self.connection.execute(
            "INSERT INTO turns(turn_id,thread_id,turn_state,delivery_state,updated_at) "
            "VALUES(?,?,?,?,?)",
            (
                turn_id,
                thread_id,
                TurnState.CREATED.value,
                DeliveryState.PENDING.value,
                _utc_now(),
            ),
        )

    def transition_turn(
        self, turn_id: str, expected: TurnState, target: TurnState
    ) -> None:
        if target not in _TURN_TRANSITIONS[expected]:
            raise InvalidTransition(f"turn {expected.value} -> {target.value} is forbidden")
        result = self.connection.execute(
            "UPDATE turns SET turn_state=?,updated_at=? WHERE turn_id=? AND turn_state=?",
            (target.value, _utc_now(), turn_id, expected.value),
        )
        if result.rowcount != 1:
            raise InvalidTransition("turn transition lost compare-and-swap")

    def transition_delivery(
        self, turn_id: str, expected: DeliveryState, target: DeliveryState
    ) -> None:
        if target not in _DELIVERY_TRANSITIONS[expected]:
            raise InvalidTransition(f"delivery {expected.value} -> {target.value} is forbidden")
        result = self.connection.execute(
            "UPDATE turns SET delivery_state=?,updated_at=? "
            "WHERE turn_id=? AND delivery_state=?",
            (target.value, _utc_now(), turn_id, expected.value),
        )
        if result.rowcount != 1:
            raise InvalidTransition("delivery transition lost compare-and-swap")

    def create_dispatch_attempt(self, attempt_id: str) -> None:
        self.connection.execute(
            "INSERT INTO dispatch_attempts(dispatch_attempt_id,state,updated_at) VALUES(?,?,?)",
            (attempt_id, DispatchState.RECEIVED.value, _utc_now()),
        )

    def transition_dispatch(
        self, attempt_id: str, expected: DispatchState, target: DispatchState
    ) -> None:
        if target not in _DISPATCH_TRANSITIONS[expected]:
            raise InvalidTransition(f"dispatch {expected.value} -> {target.value} is forbidden")
        result = self.connection.execute(
            "UPDATE dispatch_attempts SET state=?,updated_at=? "
            "WHERE dispatch_attempt_id=? AND state=?",
            (target.value, _utc_now(), attempt_id, expected.value),
        )
        if result.rowcount != 1:
            raise InvalidTransition("dispatch transition lost compare-and-swap")

    def create_approval(self, approval_id: str, request_hash: str) -> None:
        self.connection.execute(
            "INSERT INTO approval_requests(approval_id,state,request_hash,updated_at) "
            "VALUES(?,?,?,?)",
            (approval_id, ApprovalState.ISSUED.value, request_hash, _utc_now()),
        )

    def transition_approval(
        self,
        approval_id: str,
        expected: ApprovalState,
        target: ApprovalState,
        *,
        response_hash: str | None = None,
    ) -> None:
        if target not in _APPROVAL_TRANSITIONS[expected]:
            raise InvalidTransition(f"approval {expected.value} -> {target.value} is forbidden")
        if target is ApprovalState.RESPONSE_SENDING:
            if expected is not ApprovalState.ACTION_COMMITTED or not response_hash:
                raise InvalidTransition("exact response hash must be committed immediately before sending")
            result = self.connection.execute(
                "UPDATE approval_requests SET state=?,response_hash=?,updated_at=? "
                "WHERE approval_id=? AND state=? AND response_hash IS NULL",
                (target.value, response_hash, _utc_now(), approval_id, expected.value),
            )
        else:
            if response_hash is not None:
                raise InvalidTransition("approval response hash cannot be changed after send preparation")
            result = self.connection.execute(
                "UPDATE approval_requests SET state=?,updated_at=? WHERE approval_id=? AND state=?",
                (target.value, _utc_now(), approval_id, expected.value),
            )
        if result.rowcount != 1:
            raise InvalidTransition("approval transition lost compare-and-swap")

    def transition_outbox(self, outbox_id: int, expected: str, target: str) -> None:
        transitions = {
            "pending": {"confirmed", "retryable", "permanent", "unknown", "final_undelivered"},
            "retryable": {"confirmed", "retryable", "permanent", "unknown", "final_undelivered"},
            "unknown": {"confirmed", "permanent", "final_undelivered"},
        }
        if target not in transitions.get(expected, set()):
            raise InvalidTransition(f"outbox {expected} -> {target} is forbidden")
        result = self.connection.execute(
            "UPDATE outbox SET state=? WHERE outbox_id=? AND state=?",
            (target, outbox_id, expected),
        )
        if result.rowcount != 1:
            raise InvalidTransition("outbox transition lost compare-and-swap")
