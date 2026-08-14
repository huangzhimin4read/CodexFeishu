import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from codex_feishu_bridge.operations.backup import create_backup, restore_backup
from codex_feishu_bridge.operations.migrations import Migration, MigrationRunner
from codex_feishu_bridge.operations.pilot import PilotTracker
from codex_feishu_bridge.operations.recovery import (
    RecoveryError,
    RecoveryProof,
    promote_recovered_database,
)
from codex_feishu_bridge.operations.update_gate import UpdateGate, UpdateGateError
from codex_feishu_bridge.operations.windows_task import (
    render_task_xml,
    render_worker_task_xml,
)
from codex_feishu_bridge.runtime_storage import RuntimeStorage


ROOT = Path(__file__).parents[1]


def test_atomic_migration_rolls_back_partial_ddl(tmp_path: Path) -> None:
    with RuntimeStorage(tmp_path / "db.sqlite") as storage:
        storage.initialize_runtime(sink_mode="control")
        with pytest.raises(sqlite3.DatabaseError):
            storage.execute_schema_migration(
                "CREATE TABLE migration_partial(id INTEGER); THIS IS INVALID SQL;"
            )
        assert storage.connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='migration_partial'"
        ).fetchone()[0] == 0


def test_versioned_migration_and_missing_path(tmp_path: Path) -> None:
    with RuntimeStorage(tmp_path / "db.sqlite") as storage:
        storage.initialize_runtime(sink_mode="control")
        storage.connection.execute(
            "UPDATE runtime_metadata SET value='2' WHERE key='runtime_schema_version'"
        )
        runner = MigrationRunner((Migration(2, 3, "CREATE TABLE v3_fixture(id INTEGER);"),))
        assert runner.apply(storage, 3) == 3
        with pytest.raises(Exception, match="newer"):
            runner.apply(storage, 2)


def test_backup_restore_stays_reconciliation_only_until_complete_proof(tmp_path: Path) -> None:
    schema = tmp_path / "schemas"
    schema.mkdir()
    (schema / "one.json").write_text("{}", encoding="utf-8")
    config = tmp_path / "runtime.toml"
    config.write_text("schema_version=1\n", encoding="utf-8")
    database = tmp_path / "runtime.db"
    with RuntimeStorage(database) as storage:
        storage.initialize_runtime(sink_mode="pilot")
        backup = create_backup(
            storage,
            backup_root=tmp_path / "backups",
            config_path=config,
            schema_root=schema,
        )
    restored = tmp_path / "restored.db"
    restore_backup(backup, restored)
    with RuntimeStorage(restored) as storage:
        with pytest.raises(RecoveryError, match="incomplete"):
            promote_recovered_database(storage, RecoveryProof(True, False, True, True))
        fencing = promote_recovered_database(storage, RecoveryProof(True, True, True, True))
        assert fencing >= 1


def test_update_gate_detects_executable_or_schema_drift(tmp_path: Path) -> None:
    gate = UpdateGate.load(ROOT / "generated/codex/0.145.0/baseline.json")
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"changed")
    with pytest.raises(UpdateGateError, match="executable changed"):
        gate.require_executable(executable)
    gate.require_schemas(ROOT / "generated/codex/0.145.0")


def test_pilot_cannot_pass_before_seven_days_or_with_unknown(tmp_path: Path) -> None:
    with RuntimeStorage(tmp_path / "db.sqlite") as storage:
        storage.initialize_runtime(sink_mode="pilot")
        tracker = PilotTracker(storage)
        tracker.start()
        tracker.sample(latency_ms=5)
        decision = tracker.decide()
        assert not decision.passed and "less_than_seven_days" in decision.reasons


def test_scheduled_task_templates_are_hidden_and_least_privilege(tmp_path: Path) -> None:
    service = render_task_xml(
        python_executable=Path("C:/Python/python.exe"),
        config_path=tmp_path / "runtime.toml",
        account="DOMAIN\\broker",
    )
    worker = render_worker_task_xml(
        python_executable=Path("C:/Python/python.exe"),
        launch_file=tmp_path / "launch.json",
        account="DOMAIN\\worker",
        diagnostic_file=tmp_path / "worker-error.json",
    )
    assert "LeastPrivilege" in service and "<Hidden>true</Hidden>" in service
    assert "isolated-worker" in worker and "HighestAvailable" not in worker
    assert "--diagnostic-file" in worker and "worker-error.json" in worker
