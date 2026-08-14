"""Versioned atomic migration runner with proof-copy support."""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..runtime_storage import RuntimeStorage


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Migration:
    from_version: int
    to_version: int
    sql: str

    def __post_init__(self) -> None:
        if self.to_version != self.from_version + 1:
            raise MigrationError("migrations must advance exactly one version")


class MigrationRunner:
    def __init__(self, migrations: tuple[Migration, ...]) -> None:
        self.migrations = {item.from_version: item for item in migrations}

    def apply(self, storage: RuntimeStorage, target_version: int) -> int:
        row = storage.connection.execute(
            "SELECT value FROM runtime_metadata WHERE key='runtime_schema_version'"
        ).fetchone()
        if row is None:
            raise MigrationError("runtime schema version is absent")
        current = int(row[0])
        while current < target_version:
            migration = self.migrations.get(current)
            if migration is None:
                raise MigrationError(f"no migration path from runtime schema {current}")
            script = (
                migration.sql
                + "\nUPDATE runtime_metadata SET value='"
                + str(migration.to_version)
                + "' WHERE key='runtime_schema_version';"
            )
            storage.execute_schema_migration(script)
            current = migration.to_version
        if current != target_version:
            raise MigrationError("database is newer than the supported runtime schema")
        return current

    def prove_on_copy(self, database: Path, target_version: int) -> None:
        with tempfile.TemporaryDirectory(prefix="codex-feishu-migration-") as raw:
            copy = Path(raw) / "proof.sqlite3"
            shutil.copy2(database.resolve(), copy)
            with RuntimeStorage(copy) as storage:
                self.apply(storage, target_version)
                result = storage.connection.execute("PRAGMA integrity_check").fetchone()[0]
                if result != "ok":
                    raise MigrationError(f"migration proof integrity check failed: {result}")
