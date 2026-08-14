"""Versioned runtime configuration without plaintext credentials."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class RuntimeMode(StrEnum):
    OFFLINE = "offline"
    SHADOW = "shadow"
    OUTBOUND = "outbound"
    APPROVALS = "approvals"
    INBOUND = "inbound"
    CONTROLS = "controls"
    PILOT = "pilot"


_MODE_LEVEL = {mode: index for index, mode in enumerate(RuntimeMode)}


class ConversationMode(StrEnum):
    """Provider surface used for one runtime database."""

    P2P = "p2p"
    TOPIC_GROUP = "topic_group"


class RemoteDeliveryMode(StrEnum):
    """Writer that owns Feishu-originated Codex user turns."""

    APP_SERVER = "app_server"
    DESKTOP = "desktop"


@dataclass(frozen=True, slots=True)
class RemoteCapabilities:
    """Individually authorized Feishu-to-Codex capabilities.

    These flags are intentionally independent from ``RuntimeMode``.  The
    topic-group service must keep activity-triggered project provisioning while
    adding a narrowly auditable control plane; ordinal mode comparisons cannot
    represent that combination safely.
    """

    enabled: bool = False
    text: bool = False
    images: bool = False
    files: bool = False
    approvals: bool = False
    controls: bool = False
    auto_approve: bool = False
    delivery: RemoteDeliveryMode = RemoteDeliveryMode.APP_SERVER
    max_image_bytes: int = 10 * 1024 * 1024
    max_file_bytes: int = 25 * 1024 * 1024

    def __post_init__(self) -> None:
        try:
            delivery = RemoteDeliveryMode(self.delivery)
        except ValueError as exc:
            raise RuntimeConfigurationError("unsupported remote delivery mode") from exc
        object.__setattr__(self, "delivery", delivery)
        selected = self.text or self.images or self.files or self.approvals or self.controls
        if self.enabled != selected:
            raise RuntimeConfigurationError(
                "remote.enabled must be true exactly when at least one remote capability is enabled"
            )
        if self.max_image_bytes <= 0 or self.max_image_bytes > 10 * 1024 * 1024:
            raise RuntimeConfigurationError("remote image limit must be within 1..10 MiB")
        if self.max_file_bytes <= 0 or self.max_file_bytes > 100 * 1024 * 1024:
            raise RuntimeConfigurationError("remote file limit must be within 1..100 MiB")
        if self.auto_approve and not self.approvals:
            raise RuntimeConfigurationError(
                "remote.auto_approve requires the approvals capability"
            )

    @property
    def receives_messages(self) -> bool:
        return self.enabled and (self.text or self.images or self.files or self.controls)

    @property
    def needs_control_plane(self) -> bool:
        return self.enabled and (self.receives_messages or self.approvals)

    @property
    def uses_desktop(self) -> bool:
        return self.enabled and self.receives_messages and self.delivery is RemoteDeliveryMode.DESKTOP


class RuntimeConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CodexProjectAutomation:
    """Activity-triggered project enrollment sourced from Codex app state."""

    enabled: bool
    activity_after_ms: int
    primary_project_id: str

    def __post_init__(self) -> None:
        if self.enabled:
            if self.activity_after_ms <= 0:
                raise RuntimeConfigurationError("codex project activity cutoff must be positive")
            if not self.primary_project_id.strip():
                raise RuntimeConfigurationError("primary Codex project id must not be empty")


@dataclass(frozen=True, slots=True)
class FeishuBinding:
    tenant_key: str
    app_id: str
    owner_open_id: str
    p2p_chat_id: str
    credential_target: str
    endpoint_contract: Path
    conversation_mode: ConversationMode = ConversationMode.P2P
    topic_chat_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("tenant_key", "app_id", "owner_open_id", "p2p_chat_id"):
            if not getattr(self, name).strip():
                raise RuntimeConfigurationError(f"{name} must not be empty")
        if not self.credential_target.strip():
            raise RuntimeConfigurationError("credential_target must not be empty")
        try:
            mode = ConversationMode(self.conversation_mode)
        except ValueError as exc:
            raise RuntimeConfigurationError("unsupported Feishu conversation_mode") from exc
        object.__setattr__(self, "conversation_mode", mode)
        if mode is ConversationMode.TOPIC_GROUP and not (
            isinstance(self.topic_chat_id, str) and self.topic_chat_id.strip()
        ):
            raise RuntimeConfigurationError("topic_group mode requires topic_chat_id")

    @property
    def target_chat_id(self) -> str:
        if self.conversation_mode is ConversationMode.TOPIC_GROUP:
            assert self.topic_chat_id is not None
            return self.topic_chat_id
        return self.p2p_chat_id

    @property
    def target_chat_type(self) -> str:
        return "group" if self.conversation_mode is ConversationMode.TOPIC_GROUP else "p2p"

    @property
    def reply_in_thread(self) -> bool:
        return self.conversation_mode is ConversationMode.TOPIC_GROUP


@dataclass(frozen=True, slots=True)
class WorkerIsolation:
    worker_sid: str
    scheduled_task_name: str
    launch_file: Path
    worker_codex_home: Path

    def __post_init__(self) -> None:
        if not self.worker_sid.startswith("S-1-"):
            raise RuntimeConfigurationError("worker_sid must be a Windows SID")
        if not self.scheduled_task_name.startswith("CodexFeishu-Worker-"):
            raise RuntimeConfigurationError("isolated worker task name has an invalid prefix")


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    schema_version: int
    mode: RuntimeMode
    workspace_root: Path
    database_path: Path
    shadow_database_path: Path
    codex_home: Path
    codex_executable: Path
    generated_schema_root: Path
    project_allowlist: tuple[Path, ...]
    thread_allowlist: frozenset[str]
    project_name: str | None = None
    codex_projects: CodexProjectAutomation | None = None
    feishu: FeishuBinding | None = None
    worker_isolation: WorkerIsolation | None = None
    remote: RemoteCapabilities = RemoteCapabilities()
    retention_days: int = 30
    command_ttl_seconds: int = 86_400
    status_path: Path | None = None
    backup_root: Path | None = None

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise RuntimeConfigurationError("unsupported runtime config schema version")
        root = self.workspace_root.resolve()
        object.__setattr__(self, "workspace_root", root)
        project_name = self.project_name or root.name
        if not project_name.strip() or len(project_name) > 60:
            raise RuntimeConfigurationError("project_name must contain 1..60 characters")
        object.__setattr__(self, "project_name", project_name.strip())
        for field_name in (
            "database_path",
            "shadow_database_path",
            "codex_home",
            "codex_executable",
            "generated_schema_root",
        ):
            object.__setattr__(self, field_name, getattr(self, field_name).resolve())
        object.__setattr__(
            self,
            "project_allowlist",
            tuple(path.resolve() for path in self.project_allowlist),
        )
        if self.retention_days < 7:
            raise RuntimeConfigurationError("retention must cover the minimum dedup horizon")
        if self.command_ttl_seconds <= 0:
            raise RuntimeConfigurationError("command TTL must be positive")
        if self.database_path == self.shadow_database_path:
            raise RuntimeConfigurationError("shadow and runtime databases must be distinct")
        for protected in (self.database_path, self.shadow_database_path):
            if not protected.is_relative_to(root):
                raise RuntimeConfigurationError("runtime databases must stay inside workspace_root")
        for field_name in ("status_path", "backup_root"):
            optional = getattr(self, field_name)
            if optional is not None:
                resolved = optional.resolve()
                object.__setattr__(self, field_name, resolved)
                if not resolved.is_relative_to(root):
                    raise RuntimeConfigurationError("status and backup paths must stay inside workspace_root")
        if self.mode is not RuntimeMode.OFFLINE and not self.codex_executable.is_file():
            raise RuntimeConfigurationError("pinned Codex executable does not exist")
        if self.mode is not RuntimeMode.OFFLINE and (
            not self.project_allowlist or not self.thread_allowlist
        ):
            raise RuntimeConfigurationError("active modes require project and thread allowlists")
        if _MODE_LEVEL[self.mode] >= _MODE_LEVEL[RuntimeMode.OUTBOUND]:
            if self.feishu is None:
                raise RuntimeConfigurationError("Feishu binding is required for outbound modes")
            if not self.project_allowlist or not self.thread_allowlist:
                raise RuntimeConfigurationError("outbound modes require project and thread allowlists")
        if self.feishu is not None:
            contract = self.feishu.endpoint_contract.resolve()
            if not contract.is_file():
                raise RuntimeConfigurationError("endpoint contract does not exist")
        if self.codex_projects is not None and self.codex_projects.enabled:
            if self.mode is not RuntimeMode.OUTBOUND:
                raise RuntimeConfigurationError(
                    "Codex project auto-provisioning is restricted to outbound mode"
                )
            if self.feishu is None or self.feishu.conversation_mode is not ConversationMode.TOPIC_GROUP:
                raise RuntimeConfigurationError(
                    "Codex project auto-provisioning requires topic_group mode"
                )
        if self.remote.enabled:
            if self.mode is not RuntimeMode.OUTBOUND:
                raise RuntimeConfigurationError(
                    "independent remote capabilities currently require outbound topic-group mode"
                )
            if self.feishu is None or self.feishu.conversation_mode is not ConversationMode.TOPIC_GROUP:
                raise RuntimeConfigurationError(
                    "remote capabilities require topic_group conversation mode"
                )
            if self.codex_projects is None or not self.codex_projects.enabled:
                raise RuntimeConfigurationError(
                    "remote topic routing requires Codex project authority"
                )
            if (
                self.remote.delivery is RemoteDeliveryMode.APP_SERVER
                and self.worker_isolation is None
            ):
                raise RuntimeConfigurationError(
                    "remote capabilities require a separately-principalled App Server worker"
                )
            if self.worker_isolation is not None:
                launch_file = self.worker_isolation.launch_file.resolve()
                if not launch_file.is_relative_to(root / ".runtime"):
                    raise RuntimeConfigurationError(
                        "isolated worker launch file must stay inside workspace .runtime"
                    )

    def allows(self, capability: RuntimeMode) -> bool:
        return _MODE_LEVEL[self.mode] >= _MODE_LEVEL[capability]


def _required(table: dict[str, Any], name: str) -> Any:
    if name not in table:
        raise RuntimeConfigurationError(f"missing required config field: {name}")
    return table[name]


def load_runtime_config(path: Path) -> RuntimeConfig:
    config_path = path.resolve()
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)
    allowed_top = {
        "schema_version",
        "mode",
        "paths",
        "allowlist",
        "feishu",
        "worker_isolation",
        "retention_days",
        "command_ttl_seconds",
        "project_name",
        "codex_projects",
        "remote",
    }
    unknown = set(raw) - allowed_top
    if unknown:
        raise RuntimeConfigurationError(f"unknown config fields: {sorted(unknown)}")
    paths = _required(raw, "paths")
    allowlist = raw.get("allowlist", {})
    if not isinstance(paths, dict) or not isinstance(allowlist, dict):
        raise RuntimeConfigurationError("paths and allowlist must be tables")
    allowed_paths = {
        "workspace_root",
        "database",
        "shadow_database",
        "codex_home",
        "codex_executable",
        "schema_root",
        "status",
        "backup_root",
    }
    extra_paths = set(paths) - allowed_paths
    if extra_paths:
        raise RuntimeConfigurationError(f"unknown path fields: {sorted(extra_paths)}")
    extra_allowlist = set(allowlist) - {"projects", "threads"}
    if extra_allowlist:
        raise RuntimeConfigurationError(f"unknown allowlist fields: {sorted(extra_allowlist)}")
    feishu_raw = raw.get("feishu")
    feishu = None
    if feishu_raw is not None:
        allowed_feishu = {
            "tenant_key",
            "app_id",
            "owner_open_id",
            "p2p_chat_id",
            "credential_target",
            "endpoint_contract",
            "conversation_mode",
            "topic_chat_id",
        }
        extra = set(feishu_raw) - allowed_feishu
        if extra:
            raise RuntimeConfigurationError(f"unknown Feishu fields: {sorted(extra)}")
        feishu = FeishuBinding(
            tenant_key=str(_required(feishu_raw, "tenant_key")),
            app_id=str(_required(feishu_raw, "app_id")),
            owner_open_id=str(_required(feishu_raw, "owner_open_id")),
            p2p_chat_id=str(_required(feishu_raw, "p2p_chat_id")),
            credential_target=str(_required(feishu_raw, "credential_target")),
            endpoint_contract=(
                config_path.parent / str(_required(feishu_raw, "endpoint_contract"))
            ).resolve(),
            conversation_mode=ConversationMode(str(feishu_raw.get("conversation_mode", "p2p"))),
            topic_chat_id=(
                str(feishu_raw["topic_chat_id"])
                if feishu_raw.get("topic_chat_id") is not None
                else None
            ),
        )
    worker_raw = raw.get("worker_isolation")
    worker = None
    if worker_raw is not None:
        if not isinstance(worker_raw, dict):
            raise RuntimeConfigurationError("worker_isolation must be a table")
        allowed_worker = {
            "worker_sid",
            "scheduled_task_name",
            "launch_file",
            "worker_codex_home",
        }
        extra_worker = set(worker_raw) - allowed_worker
        if extra_worker:
            raise RuntimeConfigurationError(f"unknown worker fields: {sorted(extra_worker)}")
        worker = WorkerIsolation(
            worker_sid=str(_required(worker_raw, "worker_sid")),
            scheduled_task_name=str(_required(worker_raw, "scheduled_task_name")),
            launch_file=(config_path.parent / str(_required(worker_raw, "launch_file"))).resolve(),
            worker_codex_home=(
                config_path.parent / str(_required(worker_raw, "worker_codex_home"))
            ).resolve(),
        )
    codex_projects_raw = raw.get("codex_projects")
    codex_projects = None
    if codex_projects_raw is not None:
        if not isinstance(codex_projects_raw, dict):
            raise RuntimeConfigurationError("codex_projects must be a table")
        extra_projects = set(codex_projects_raw) - {
            "enabled",
            "activity_after_ms",
            "primary_project_id",
        }
        if extra_projects:
            raise RuntimeConfigurationError(
                f"unknown codex_projects fields: {sorted(extra_projects)}"
            )
        codex_projects = CodexProjectAutomation(
            enabled=bool(codex_projects_raw.get("enabled", False)),
            activity_after_ms=int(codex_projects_raw.get("activity_after_ms", 0)),
            primary_project_id=str(codex_projects_raw.get("primary_project_id", "")),
        )
    remote_raw = raw.get("remote")
    remote = RemoteCapabilities()
    if remote_raw is not None:
        if not isinstance(remote_raw, dict):
            raise RuntimeConfigurationError("remote must be a table")
        allowed_remote = {
            "enabled",
            "text",
            "images",
            "files",
            "approvals",
            "controls",
            "auto_approve",
            "delivery",
            "max_image_bytes",
            "max_file_bytes",
        }
        extra_remote = set(remote_raw) - allowed_remote
        if extra_remote:
            raise RuntimeConfigurationError(
                f"unknown remote fields: {sorted(extra_remote)}"
            )
        remote = RemoteCapabilities(
            enabled=bool(remote_raw.get("enabled", False)),
            text=bool(remote_raw.get("text", False)),
            images=bool(remote_raw.get("images", False)),
            files=bool(remote_raw.get("files", False)),
            approvals=bool(remote_raw.get("approvals", False)),
            controls=bool(remote_raw.get("controls", False)),
            auto_approve=bool(remote_raw.get("auto_approve", False)),
            delivery=RemoteDeliveryMode(str(remote_raw.get("delivery", "app_server"))),
            max_image_bytes=int(remote_raw.get("max_image_bytes", 10 * 1024 * 1024)),
            max_file_bytes=int(remote_raw.get("max_file_bytes", 25 * 1024 * 1024)),
        )
    base = config_path.parent
    return RuntimeConfig(
        schema_version=int(_required(raw, "schema_version")),
        mode=RuntimeMode(str(_required(raw, "mode"))),
        workspace_root=(base / str(_required(paths, "workspace_root"))).resolve(),
        database_path=(base / str(_required(paths, "database"))).resolve(),
        shadow_database_path=(base / str(_required(paths, "shadow_database"))).resolve(),
        codex_home=(base / str(_required(paths, "codex_home"))).resolve(),
        codex_executable=(base / str(_required(paths, "codex_executable"))).resolve(),
        generated_schema_root=(base / str(_required(paths, "schema_root"))).resolve(),
        project_allowlist=tuple(
            (base / str(value)).resolve() for value in allowlist.get("projects", [])
        ),
        thread_allowlist=frozenset(str(value) for value in allowlist.get("threads", [])),
        project_name=(str(raw["project_name"]) if raw.get("project_name") is not None else None),
        codex_projects=codex_projects,
        feishu=feishu,
        worker_isolation=worker,
        remote=remote,
        retention_days=int(raw.get("retention_days", 30)),
        command_ttl_seconds=int(raw.get("command_ttl_seconds", 86_400)),
        status_path=(base / str(paths["status"])).resolve() if "status" in paths else None,
        backup_root=(base / str(paths["backup_root"])).resolve()
        if "backup_root" in paths
        else None,
    )
