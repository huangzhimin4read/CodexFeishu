from pathlib import Path

import pytest

from codex_feishu_bridge.config import BridgeSettings, ConfigurationError


def test_m0_configuration_is_offline_and_workspace_scoped(tmp_path: Path) -> None:
    settings = BridgeSettings(
        workspace_root=tmp_path,
        database_path=tmp_path / "runtime" / "m0.db",
        schema_root=tmp_path / "generated",
    )
    assert settings.sync_enabled is False
    assert settings.sink_mode == "offline_fixture"


def test_m0_rejects_sync_and_external_paths(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="forbids cloud"):
        BridgeSettings(
            workspace_root=tmp_path,
            database_path=tmp_path / "m0.db",
            schema_root=tmp_path / "generated",
            sync_enabled=True,
        )
    with pytest.raises(ConfigurationError, match="inside the workspace"):
        BridgeSettings(
            workspace_root=tmp_path / "workspace",
            database_path=tmp_path / "outside.db",
            schema_root=tmp_path / "workspace" / "generated",
        )
