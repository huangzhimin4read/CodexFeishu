"""Durable, activity-triggered Feishu group provisioning per Codex project."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass

from ..codex.project_catalog import CatalogProject
from ..runtime_storage import RuntimeStorage, utc_now
from .client import FeishuClient, ProviderOutcome
from .provisioning import _is_private_topic_group


_PROJECT_GROUP_NAMESPACE = uuid.UUID("c9e61a63-d453-54a6-b963-52f8d1e7002f")


@dataclass(frozen=True, slots=True)
class ProjectGroupResult:
    project_id: str
    state: str
    chat_id: str | None
    created: bool = False


def _roots_json(project: CatalogProject) -> str:
    return json.dumps(
        [str(path) for path in project.root_paths],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _chat_data(response: dict | None) -> dict:
    if not isinstance(response, dict) or not isinstance(response.get("data"), dict):
        return {}
    data = response["data"]
    return data["chat"] if isinstance(data.get("chat"), dict) else data


class ProjectGroupManager:
    """Provision at most one private thread-message group per active project."""

    def __init__(
        self,
        storage: RuntimeStorage,
        client: FeishuClient,
        *,
        owner_open_id: str,
        uuid_window_seconds: int,
    ) -> None:
        if uuid_window_seconds <= 0:
            raise ValueError("create-chat UUID window must be positive")
        self.storage = storage
        self.client = client
        self.owner_open_id = owner_open_id
        self.uuid_window_ms = uuid_window_seconds * 1000
        self._retry_not_before_ms: dict[str, int] = {}

    def register_existing(
        self,
        project: CatalogProject,
        *,
        chat_id: str,
        last_activity_ms: int,
    ) -> ProjectGroupResult:
        """Bind the already approved primary group after a live shape/name check."""
        verified = self.client.call(
            "preflight_chat",
            path_parameters={"chat_id": chat_id},
            chat_id=chat_id,
        )
        chat = _chat_data(verified.response)
        if (
            verified.outcome is not ProviderOutcome.CONFIRMED
            or not _is_private_topic_group(verified.response)
            or chat.get("name") != project.display_name
        ):
            raise RuntimeError("primary project group does not match current Codex project")
        chat_mode = str(chat["chat_mode"])
        group_message_type = chat.get("group_message_type")
        if group_message_type is not None:
            group_message_type = str(group_message_type)
        now = utc_now()
        with self.storage.immediate() as connection:
            existing = connection.execute(
                "SELECT chat_id FROM project_groups WHERE project_id=?", (project.project_id,)
            ).fetchone()
            if existing is not None and existing["chat_id"] not in {None, chat_id}:
                raise RuntimeError("primary Codex project is already bound to another group")
            connection.execute(
                "INSERT INTO project_groups(project_id,project_kind,display_name,root_paths_json,chat_id,"
                "chat_mode,group_message_type,state,last_activity_ms,created_at,updated_at) "
                "VALUES(?,'local',?,?,?,?,?,'active',?,?,?) "
                "ON CONFLICT(project_id) DO UPDATE SET display_name=excluded.display_name,"
                "root_paths_json=excluded.root_paths_json,chat_id=excluded.chat_id,"
                "chat_mode=excluded.chat_mode,group_message_type=excluded.group_message_type,state='active',"
                "last_activity_ms=MAX(project_groups.last_activity_ms,excluded.last_activity_ms),"
                "last_error=NULL,updated_at=excluded.updated_at",
                (
                    project.project_id,
                    project.display_name,
                    _roots_json(project),
                    chat_id,
                    chat_mode,
                    group_message_type,
                    last_activity_ms,
                    now,
                    now,
                ),
            )
        return ProjectGroupResult(project.project_id, "active", chat_id)

    def ensure(
        self,
        project: CatalogProject,
        *,
        last_activity_ms: int,
    ) -> ProjectGroupResult:
        if last_activity_ms <= 0:
            raise ValueError("project activity time must be positive")
        now_ms = int(time.time() * 1000)
        row = self.storage.connection.execute(
            "SELECT * FROM project_groups WHERE project_id=?", (project.project_id,)
        ).fetchone()
        if row is not None:
            self._assert_identity(row, project)
            self.storage.connection.execute(
                "UPDATE project_groups SET last_activity_ms=MAX(last_activity_ms,?),updated_at=? "
                "WHERE project_id=?",
                (last_activity_ms, utc_now(), project.project_id),
            )
            if row["state"] == "active" and row["chat_id"]:
                return ProjectGroupResult(project.project_id, "active", row["chat_id"])
            if row["state"] == "outcome_unknown":
                if row["chat_id"]:
                    return self._reverify(project, str(row["chat_id"]))
                return ProjectGroupResult(project.project_id, "outcome_unknown", None)
            if row["state"] in {"failed", "disabled"}:
                return ProjectGroupResult(project.project_id, str(row["state"]), row["chat_id"])
            if now_ms < self._retry_not_before_ms.get(project.project_id, 0):
                return ProjectGroupResult(project.project_id, "creating", None)
            attempted = int(row["create_attempted_at_ms"] or 0)
            if attempted <= 0 or now_ms - attempted >= self.uuid_window_ms:
                self._mark_unknown(project.project_id, "create_uuid_window_expired")
                return ProjectGroupResult(project.project_id, "outcome_unknown", None)
            create_uuid = str(row["create_uuid"])
        else:
            create_uuid = str(uuid.uuid5(_PROJECT_GROUP_NAMESPACE, project.project_id))
            now = utc_now()
            with self.storage.immediate() as connection:
                connection.execute(
                    "INSERT INTO project_groups(project_id,project_kind,display_name,root_paths_json,"
                    "state,create_uuid,create_attempted_at_ms,last_activity_ms,created_at,updated_at) "
                    "VALUES(?,'local',?,?,'creating',?,?,?,?,?)",
                    (
                        project.project_id,
                        project.display_name,
                        _roots_json(project),
                        create_uuid,
                        now_ms,
                        last_activity_ms,
                        now,
                        now,
                    ),
                )
        result = self.client.call(
            "create_chat",
            query={
                "user_id_type": "open_id",
                "set_bot_manager": "true",
                "uuid": create_uuid,
            },
            json_body={
                "name": project.display_name,
                "description": f"Codex 项目通知：{project.display_name}",
                "owner_id": self.owner_open_id,
                "chat_mode": "group",
                "chat_type": "private",
                "group_message_type": "thread",
                "join_message_visibility": "not_anyone",
                "leave_message_visibility": "not_anyone",
            },
        )
        if result.outcome is ProviderOutcome.CONFIRMED and result.chat_id:
            return self._reverify(project, result.chat_id, created=True)
        if result.outcome is ProviderOutcome.RETRYABLE:
            self._retry_not_before_ms[project.project_id] = now_ms + 30_000
            self.storage.connection.execute(
                "UPDATE project_groups SET last_error=?,updated_at=? WHERE project_id=? AND state='creating'",
                (result.code, utc_now(), project.project_id),
            )
            return ProjectGroupResult(project.project_id, "creating", None)
        if result.outcome is ProviderOutcome.UNKNOWN:
            self._mark_unknown(project.project_id, result.code)
            return ProjectGroupResult(project.project_id, "outcome_unknown", None)
        self.storage.connection.execute(
            "UPDATE project_groups SET state='failed',last_error=?,updated_at=? WHERE project_id=?",
            (result.code, utc_now(), project.project_id),
        )
        return ProjectGroupResult(project.project_id, "failed", None)

    def _reverify(
        self,
        project: CatalogProject,
        chat_id: str,
        *,
        created: bool = False,
    ) -> ProjectGroupResult:
        verified = self.client.call(
            "preflight_chat",
            path_parameters={"chat_id": chat_id},
            chat_id=chat_id,
        )
        chat = _chat_data(verified.response)
        if (
            verified.outcome is ProviderOutcome.CONFIRMED
            and _is_private_topic_group(verified.response)
            and chat.get("name") == project.display_name
        ):
            self.storage.connection.execute(
                "UPDATE project_groups SET chat_id=?,chat_mode=?,group_message_type=?,"
                "state='active',last_error=NULL,updated_at=? WHERE project_id=?",
                (
                    chat_id,
                    str(chat["chat_mode"]),
                    (
                        str(chat["group_message_type"])
                        if chat.get("group_message_type") is not None
                        else None
                    ),
                    utc_now(),
                    project.project_id,
                ),
            )
            return ProjectGroupResult(project.project_id, "active", chat_id, created)
        if verified.outcome is ProviderOutcome.PERMANENT:
            state = "failed"
        else:
            state = "outcome_unknown"
        self.storage.connection.execute(
            "UPDATE project_groups SET chat_id=?,state=?,last_error=?,updated_at=? WHERE project_id=?",
            (chat_id, state, "group_preflight:" + verified.code, utc_now(), project.project_id),
        )
        return ProjectGroupResult(project.project_id, state, chat_id, created)

    def _assert_identity(self, row: object, project: CatalogProject) -> None:
        if row["project_kind"] != "local":
            raise RuntimeError("project group is not bound to a local Codex project")
        try:
            stored_roots = tuple(json.loads(row["root_paths_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("project group roots are corrupt") from exc
        expected_roots = tuple(str(path) for path in project.root_paths)
        if row["display_name"] != project.display_name or stored_roots != expected_roots:
            raise RuntimeError("Codex project identity changed after group binding")

    def _mark_unknown(self, project_id: str, reason: str) -> None:
        self.storage.connection.execute(
            "UPDATE project_groups SET state='outcome_unknown',last_error=?,updated_at=? "
            "WHERE project_id=?",
            (reason, utc_now(), project_id),
        )
