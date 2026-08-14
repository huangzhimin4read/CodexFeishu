import json
import sqlite3
from pathlib import Path

import pytest

from codex_feishu_bridge.codex.project_catalog import CodexProjectCatalog
from codex_feishu_bridge.codex.state_discovery import DiscoveryError


def _catalog_fixture(
    tmp_path: Path,
    *,
    assigned_cwd: Path | None = None,
    omit_assignment_cwd: bool = False,
) -> CodexProjectCatalog:
    home = tmp_path / "codex-home"
    sessions = home / "sessions" / "2026" / "08" / "12"
    sessions.mkdir(parents=True)
    project = tmp_path / "workspace"
    project.mkdir()
    rollout = sessions / "rollout.jsonl"
    rollout.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": "thread-active", "cwd": str(project), "rollout_version": "1"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    active_assignment = {
        "projectKind": "local",
        "projectId": "project-current",
    }
    if not omit_assignment_cwd:
        active_assignment["cwd"] = str(assigned_cwd or project)
    state = {
        "project-order": ["project-current"],
        "local-projects": {
            "project-current": {
                "id": "project-current",
                "name": "当前项目",
                "rootPaths": [str(project)],
            }
        },
        "thread-project-assignments": {
            "thread-active": active_assignment,
            "thread-dormant": {
                "projectKind": "local",
                "projectId": "project-current",
                "cwd": str(project),
            },
        },
    }
    (home / ".codex-global-state.json").write_text(
        json.dumps(state, ensure_ascii=False), encoding="utf-8"
    )
    connection = sqlite3.connect(home / "state_5.sqlite")
    connection.execute(
        "CREATE TABLE threads(id TEXT PRIMARY KEY,rollout_path TEXT,cwd TEXT,title TEXT,updated_at_ms INTEGER,"
        "archived INTEGER,thread_source TEXT)"
    )
    connection.execute(
        "INSERT INTO threads VALUES(?,?,?,?,?,0,'user')",
        ("thread-active", str(rollout), str(project), "优化嘉立创全站访问能力", 2000),
    )
    connection.execute(
        "INSERT INTO threads VALUES(?,?,?,?,?,0,'user')",
        ("thread-dormant", str(rollout), str(project), "休眠任务", 999),
    )
    connection.commit()
    connection.close()
    return CodexProjectCatalog(home)


def test_catalog_uses_current_codex_project_assignments_and_activity(tmp_path: Path) -> None:
    catalog = _catalog_fixture(tmp_path)
    projects = catalog.projects()
    assert [(item.project_id, item.display_name) for item in projects] == [
        ("project-current", "当前项目")
    ]
    active = catalog.active_rollouts(activity_after_ms=1000)
    assert len(active) == 1
    assert active[0].thread_id == "thread-active"
    assert active[0].project_id == "project-current"
    assert active[0].project_name == "当前项目"
    assert active[0].task_title == "优化嘉立创全站访问能力"


def test_catalog_accepts_current_assignment_without_duplicate_cwd(tmp_path: Path) -> None:
    catalog = _catalog_fixture(tmp_path, omit_assignment_cwd=True)
    active = catalog.active_rollouts(activity_after_ms=1000)
    assert len(active) == 1
    assert active[0].thread_id == "thread-active"
    assert active[0].project_id == "project-current"


def test_catalog_fails_closed_when_assignment_cwd_disagrees(tmp_path: Path) -> None:
    catalog = _catalog_fixture(tmp_path, assigned_cwd=tmp_path / "other")
    with pytest.raises(DiscoveryError, match="paths disagree"):
        catalog.active_rollouts(activity_after_ms=1000)
