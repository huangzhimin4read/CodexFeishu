"""Consistent SQLite backup and reconciliation-only restore."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from ..runtime_storage import RuntimeStorage


def file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def create_backup(
    storage: RuntimeStorage,
    *,
    backup_root: Path,
    config_path: Path,
    schema_root: Path,
) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = backup_root.resolve() / timestamp
    if destination.exists():
        raise FileExistsError(destination)
    destination.mkdir(parents=True)
    database_copy = destination / "bridge.sqlite3"
    target = sqlite3.connect(database_copy)
    try:
        storage.connection.backup(target)
    finally:
        target.close()
    shutil.copy2(config_path.resolve(), destination / "runtime.toml")
    schema_hashes = {
        str(path.relative_to(schema_root.resolve())).replace("\\", "/"): file_sha256(path)
        for path in sorted(schema_root.resolve().rglob("*.json"))
    }
    manifest = {
        "schema": 1,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "database_sha256": file_sha256(database_copy),
        "config_sha256": file_sha256(destination / "runtime.toml"),
        "schema_files": schema_hashes,
        "restore_mode": "reconciliation_only",
    }
    with (destination / "manifest.json").open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    return destination


def restore_backup(backup: Path, destination: Path) -> None:
    source = backup.resolve()
    with (source / "manifest.json").open(encoding="utf-8") as handle:
        manifest = json.load(handle)
    database = source / "bridge.sqlite3"
    if manifest.get("restore_mode") != "reconciliation_only" or file_sha256(database) != manifest.get("database_sha256"):
        raise RuntimeError("backup manifest/database integrity mismatch")
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError("restore refuses to overwrite an existing database")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(database, destination)
    restored = sqlite3.connect(destination)
    try:
        restored.execute(
            "INSERT INTO runtime_metadata(key,value) VALUES('restore_mode','\"reconciliation_only\"') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )
        restored.execute(
            "UPDATE service_state SET process_state='reconciliation_only',fencing_token=fencing_token+1"
        )
        restored.commit()
    finally:
        restored.close()
