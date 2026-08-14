"""Configuration with fail-closed M0 defaults."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class ConfigurationError(ValueError):
    """Raised when a configuration would cross the authorized milestone."""


@dataclass(frozen=True, slots=True)
class BridgeSettings:
    """Settings available in the M0 implementation.

    M0 deliberately has no tenant, application, credential, network endpoint,
    or background-start fields. Adding those requires a later milestone.
    """

    workspace_root: Path
    database_path: Path
    schema_root: Path
    sync_enabled: bool = False
    sink_mode: str = "offline_fixture"

    def __post_init__(self) -> None:
        root = self.workspace_root.resolve()
        database = self.database_path.resolve()
        schema = self.schema_root.resolve()
        object.__setattr__(self, "workspace_root", root)
        object.__setattr__(self, "database_path", database)
        object.__setattr__(self, "schema_root", schema)

        if self.sync_enabled:
            raise ConfigurationError("M0 forbids cloud synchronization")
        if self.sink_mode != "offline_fixture":
            raise ConfigurationError("M0 supports only the offline_fixture sink")
        if not database.is_relative_to(root):
            raise ConfigurationError("M0 database must stay inside the workspace")
        if not schema.is_relative_to(root):
            raise ConfigurationError("generated schemas must stay inside the workspace")
