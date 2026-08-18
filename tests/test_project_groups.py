import json
from pathlib import Path

from codex_feishu_bridge.codex.project_catalog import CatalogProject
from codex_feishu_bridge.feishu.client import ProviderOutcome, ProviderResult
from codex_feishu_bridge.feishu.project_groups import ProjectGroupManager
from codex_feishu_bridge.runtime_storage import RuntimeStorage


class ProjectClient:
    def __init__(self, create_outcome: ProviderOutcome = ProviderOutcome.CONFIRMED) -> None:
        self.create_outcome = create_outcome
        self.calls: list[tuple[str, dict]] = []

    def call(self, endpoint: str, **kwargs):
        self.calls.append((endpoint, kwargs))
        if endpoint == "create_chat":
            return ProviderResult(
                self.create_outcome,
                "0" if self.create_outcome is ProviderOutcome.CONFIRMED else "transport_unknown",
                chat_id="chat-new" if self.create_outcome is ProviderOutcome.CONFIRMED else None,
            )
        chat_id = kwargs["path_parameters"]["chat_id"]
        name = "项目A" if chat_id == "chat-new" else "主项目"
        return ProviderResult(
            ProviderOutcome.CONFIRMED,
            "0",
            response={
                "code": 0,
                "data": {
                    "chat_id": chat_id,
                    "name": name,
                    "chat_mode": "group",
                    "chat_type": "private",
                    "group_message_type": "thread",
                },
            },
        )


class PreflightUnavailableClient(ProjectClient):
    def call(self, endpoint: str, **kwargs):
        if endpoint == "preflight_chat":
            self.calls.append((endpoint, kwargs))
            return ProviderResult(ProviderOutcome.RETRYABLE, "temporary_unavailable")
        return super().call(endpoint, **kwargs)


def _project(tmp_path: Path, project_id: str = "project-a", name: str = "项目A"):
    root = tmp_path / project_id
    root.mkdir(exist_ok=True)
    return CatalogProject(project_id, name, (root.resolve(),))


def test_active_project_creates_one_private_thread_message_group(tmp_path: Path) -> None:
    client = ProjectClient()
    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="outbound")
        manager = ProjectGroupManager(
            storage, client, owner_open_id="owner", uuid_window_seconds=36_000
        )
        project = _project(tmp_path)
        result = manager.ensure(project, last_activity_ms=2000)
        assert result.state == "active" and result.chat_id == "chat-new" and result.created
        create = client.calls[0]
        assert create[0] == "create_chat"
        assert create[1]["query"]["user_id_type"] == "open_id"
        assert create[1]["query"]["uuid"]
        assert create[1]["json_body"] == {
            "name": "项目A",
            "description": "Codex 项目通知：项目A",
            "owner_id": "owner",
            "chat_mode": "group",
            "chat_type": "private",
            "group_message_type": "thread",
            "join_message_visibility": "not_anyone",
            "leave_message_visibility": "not_anyone",
        }
        assert manager.ensure(project, last_activity_ms=3000).chat_id == "chat-new"
        assert [item[0] for item in client.calls].count("create_chat") == 1


def test_unknown_create_result_never_blindly_creates_again(tmp_path: Path) -> None:
    client = ProjectClient(ProviderOutcome.UNKNOWN)
    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="outbound")
        manager = ProjectGroupManager(
            storage, client, owner_open_id="owner", uuid_window_seconds=36_000
        )
        project = _project(tmp_path)
        assert manager.ensure(project, last_activity_ms=2000).state == "outcome_unknown"
        assert manager.ensure(project, last_activity_ms=3000).state == "outcome_unknown"
        assert [item[0] for item in client.calls].count("create_chat") == 1


def test_primary_existing_group_is_bound_by_stable_ids_and_shape(tmp_path: Path) -> None:
    client = ProjectClient()
    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="outbound")
        manager = ProjectGroupManager(
            storage, client, owner_open_id="owner", uuid_window_seconds=36_000
        )
        result = manager.register_existing(
            _project(tmp_path, "primary", "主项目"),
            chat_id="chat-primary",
            last_activity_ms=2000,
        )
        assert result.state == "active" and result.chat_id == "chat-primary"


def test_existing_project_rename_and_root_move_update_metadata(tmp_path: Path) -> None:
    client = ProjectClient()
    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="outbound")
        manager = ProjectGroupManager(
            storage, client, owner_open_id="owner", uuid_window_seconds=36_000
        )
        original = _project(tmp_path, "project-a", "项目A")
        assert manager.ensure(original, last_activity_ms=2000).created
        moved_root = tmp_path / "moved"
        moved_root.mkdir()
        renamed = CatalogProject("project-a", "项目甲", (moved_root.resolve(),))
        result = manager.ensure(renamed, last_activity_ms=3000)
        assert result.state == "active" and not result.created
        row = storage.connection.execute(
            "SELECT display_name,root_paths_json,chat_id FROM project_groups WHERE project_id=?",
            ("project-a",),
        ).fetchone()
        assert row["display_name"] == "项目甲"
        assert json.loads(row["root_paths_json"]) == [str(moved_root.resolve())]
        assert row["chat_id"] == "chat-new"
        assert [item[0] for item in client.calls].count("create_chat") == 1


def test_stable_chat_id_remains_active_when_shape_preflight_is_unavailable(
    tmp_path: Path,
) -> None:
    client = PreflightUnavailableClient()
    with RuntimeStorage(tmp_path / "runtime.db") as storage:
        storage.initialize_runtime(sink_mode="outbound")
        manager = ProjectGroupManager(
            storage, client, owner_open_id="owner", uuid_window_seconds=36_000
        )
        registered = manager.register_existing(
            _project(tmp_path, "primary", "已改名项目"),
            chat_id="stable-chat-id",
            last_activity_ms=2000,
        )
        assert registered.state == "active"
        row = storage.connection.execute(
            "SELECT state,last_error FROM project_groups WHERE project_id='primary'"
        ).fetchone()
        assert row["state"] == "active"
        assert row["last_error"] == "group_preflight_warning:temporary_unavailable"

        created = manager.ensure(_project(tmp_path), last_activity_ms=3000)
        assert created.state == "active" and created.chat_id == "chat-new"
