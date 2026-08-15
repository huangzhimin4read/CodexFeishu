"""Fail-closed view of the projects and active tasks shown by Codex Desktop."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .state_discovery import DiscoveryError, RolloutSource, _session_metadata


@dataclass(frozen=True, slots=True)
class CatalogProject:
    project_id: str
    display_name: str
    root_paths: tuple[Path, ...]


def _normalized_path(value: str | Path) -> Path:
    text = os.fspath(value)
    if text.startswith("\\\\?\\"):
        text = text[4:]
    return Path(text).resolve()


def _inside(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


class CodexProjectCatalog:
    """Read the same persisted project authority used by the desktop app.

    The global-state file determines which local projects exist and which
    project owns each task.  The SQLite index contributes activity timestamps
    and rollout paths.  Neither workspace-directory enumeration nor title
    matching is used.
    """

    def __init__(self, codex_home: Path) -> None:
        self.codex_home = codex_home.resolve()
        self.global_state_path = self.codex_home / ".codex-global-state.json"
        self.state_database_path = self.codex_home / "state_5.sqlite"
        self.sessions_root = (self.codex_home / "sessions").resolve()

    def _state(self) -> dict[str, Any]:
        try:
            with self.global_state_path.open(encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DiscoveryError("Codex project state is unreadable") from exc
        if not isinstance(state, dict):
            raise DiscoveryError("Codex project state must be an object")
        return state

    def projects(self) -> tuple[CatalogProject, ...]:
        state = self._state()
        order = state.get("project-order")
        local = state.get("local-projects")
        if not isinstance(order, list) or not isinstance(local, dict):
            raise DiscoveryError("Codex project authority is incomplete")
        if len(order) != len(set(str(value) for value in order)):
            raise DiscoveryError("Codex project order contains duplicates")
        projects: list[CatalogProject] = []
        for raw_id in order:
            project_id = str(raw_id)
            value = local.get(project_id)
            if not isinstance(value, dict) or str(value.get("id", "")) != project_id:
                raise DiscoveryError("Codex project order references an unknown local project")
            name = value.get("name")
            roots = value.get("rootPaths")
            if (
                not isinstance(name, str)
                or not name.strip()
                or len(name.strip()) > 60
                or not isinstance(roots, list)
                or not roots
                or not all(isinstance(item, str) and item.strip() for item in roots)
            ):
                raise DiscoveryError(f"Codex project metadata is invalid: {project_id}")
            resolved_roots = tuple(_normalized_path(item) for item in roots)
            projects.append(CatalogProject(project_id, name.strip(), resolved_roots))
        if set(local) != {project.project_id for project in projects}:
            raise DiscoveryError("Codex project maps and order disagree")
        return tuple(projects)

    def active_rollouts(self, *, activity_after_ms: int) -> tuple[RolloutSource, ...]:
        if activity_after_ms <= 0:
            raise ValueError("activity cutoff must be positive")
        state = self._state()
        assignments = state.get("thread-project-assignments")
        if not isinstance(assignments, dict):
            raise DiscoveryError("Codex thread-project assignments are missing")
        projects = {project.project_id: project for project in self.projects()}
        if not self.state_database_path.is_file():
            raise DiscoveryError("Codex task index is missing")
        uri = self.state_database_path.as_uri() + "?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=2.0)
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT id,rollout_path,cwd,title,updated_at_ms FROM threads "
                "WHERE archived=0 AND thread_source='user' "
                "AND updated_at_ms>=? ORDER BY updated_at_ms,id",
                (activity_after_ms,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise DiscoveryError("Codex task index cannot be queried") from exc
        finally:
            if "connection" in locals():
                connection.close()

        found: list[RolloutSource] = []
        for row in rows:
            thread_id = str(row["id"])
            assignment = assignments.get(thread_id)
            if not isinstance(assignment, dict):
                # Projectless tasks are intentionally outside this feature.
                continue
            if assignment.get("projectKind") != "local":
                continue
            project_id = str(assignment.get("projectId", ""))
            project = projects.get(project_id)
            if project is None:
                raise DiscoveryError("active task references a non-current Codex project")
            cwd = _normalized_path(str(row["cwd"]))
            # Current Codex Desktop persists only projectKind/projectId for
            # local task assignments.  Older builds also duplicated the task
            # path as cwd/path.  The SQLite task index and rollout session
            # metadata remain independent path authorities below; validate a
            # legacy duplicate when it is present, but do not require it.
            assigned_cwd = assignment.get("cwd")
            assigned_path = assignment.get("path")
            if assigned_cwd is not None and not isinstance(assigned_cwd, str):
                raise DiscoveryError("active task assignment cwd is invalid")
            if isinstance(assigned_cwd, str) and cwd != _normalized_path(assigned_cwd):
                raise DiscoveryError("active task paths disagree")
            if assigned_path is not None and not isinstance(assigned_path, str):
                raise DiscoveryError("active task assignment path is invalid")
            if isinstance(assigned_path, str) and cwd != _normalized_path(assigned_path):
                raise DiscoveryError("active task path and cwd disagree")
            if not _inside(cwd, project.root_paths):
                raise DiscoveryError("active task is outside its Codex project roots")
            rollout_path = _normalized_path(str(row["rollout_path"]))
            if not rollout_path.is_file() or not rollout_path.is_relative_to(self.sessions_root):
                raise DiscoveryError("active task rollout path is outside Codex sessions")
            metadata = _session_metadata(rollout_path)
            metadata_id = str(metadata.get("id") or metadata.get("thread_id") or "")
            metadata_cwd = metadata.get("cwd") or metadata.get("project_root")
            if metadata_id != thread_id or not isinstance(metadata_cwd, str):
                raise DiscoveryError("active task rollout identity does not match the index")
            if _normalized_path(metadata_cwd) != cwd:
                raise DiscoveryError("active task rollout cwd does not match the index")
            version = str(metadata.get("rollout_version", metadata.get("version", "1")))
            raw_title = row["title"]
            task_title = raw_title.strip() if isinstance(raw_title, str) and raw_title.strip() else None
            stat = rollout_path.stat()
            found.append(
                RolloutSource(
                    path=rollout_path,
                    thread_id=thread_id,
                    project_root=cwd,
                    rollout_version=version,
                    modified_ns=stat.st_mtime_ns,
                    project_id=project_id,
                    project_name=project.display_name,
                    activity_ms=int(row["updated_at_ms"]),
                    task_title=task_title,
                )
            )
        return tuple(found)

    def archived_thread_ids(self, thread_ids: set[str] | frozenset[str]) -> frozenset[str]:
        """Return the requested user tasks that Codex currently marks archived.

        The desktop SQLite index is the archive-state authority.  Restricting
        the result to already discovered or bound task ids prevents unrelated
        historical rows from changing bridge state.
        """

        requested = {str(thread_id) for thread_id in thread_ids if str(thread_id)}
        if not requested:
            return frozenset()
        if not self.state_database_path.is_file():
            raise DiscoveryError("Codex task index is missing")
        uri = self.state_database_path.as_uri() + "?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=2.0)
            rows = connection.execute(
                "SELECT id FROM threads WHERE archived=1 AND thread_source='user'"
            ).fetchall()
        except sqlite3.Error as exc:
            raise DiscoveryError("Codex task archive state cannot be queried") from exc
        finally:
            if "connection" in locals():
                connection.close()
        return frozenset(str(row[0]) for row in rows if str(row[0]) in requested)
