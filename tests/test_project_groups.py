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


def test_primary_existing_group_is_bound_only_after_name_and_shape_check(tmp_path: Path) -> None:
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
