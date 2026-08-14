import sqlite3
from pathlib import Path

import pytest

from codex_feishu_bridge.models import RolloutBatch, SourceCursor
from codex_feishu_bridge.storage import BridgeStorage, StorageError


def test_same_cursor_offset_cannot_change_hash_or_schema(tmp_path: Path) -> None:
    with BridgeStorage(tmp_path / "db.sqlite") as storage:
        storage.initialize()
        first = SourceCursor("source", "file", 10, "hash-a", "1")
        storage.store_rollout_batch(RolloutBatch((), first))
        with pytest.raises(StorageError, match="schema version"):
            storage.store_rollout_batch(
                RolloutBatch((), SourceCursor("source", "file", 10, "hash-a", "999"))
            )
        with pytest.raises(StorageError, match="conflicting record hash"):
            storage.store_rollout_batch(
                RolloutBatch((), SourceCursor("source", "file", 10, "hash-b", "1"))
            )


def test_commit_authorizer_failure_rolls_back_active_transaction(tmp_path: Path) -> None:
    with BridgeStorage(tmp_path / "db.sqlite") as storage:
        storage.initialize()

        def authorizer(action, arg1, arg2, database, trigger):
            if action == sqlite3.SQLITE_TRANSACTION and arg1 == "COMMIT":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        storage.connection.set_authorizer(authorizer)
        with pytest.raises(sqlite3.DatabaseError):
            with storage.transaction() as connection:
                connection.execute(
                    "INSERT INTO dead_letters(category,record_hash,reason,created_at) "
                    "VALUES('fixture','hash','reason','2026-01-01T00:00:00Z')"
                )
        storage.connection.set_authorizer(None)
        assert not storage.connection.in_transaction
        assert storage.connection.execute("SELECT COUNT(*) FROM dead_letters").fetchone()[0] == 0


def test_positive_cursor_requires_record_hash(tmp_path: Path) -> None:
    with BridgeStorage(tmp_path / "db.sqlite") as storage:
        storage.initialize()
        with pytest.raises(StorageError, match="requires a record hash"):
            storage.store_rollout_batch(
                RolloutBatch((), SourceCursor("source", "file", 1, None, "1"))
            )
