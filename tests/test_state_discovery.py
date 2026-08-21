from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_feishu_bridge.codex import state_discovery
from codex_feishu_bridge.codex.state_discovery import DiscoveryError, discover_rollouts


def _rollout(home: Path, *, thread_id: str, project: Path) -> Path:
    path = home / "sessions" / "2026" / "08" / "14" / f"rollout-{thread_id}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": thread_id, "cwd": str(project), "version": "1"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_discovery_skips_rollout_that_vanishes_after_enumeration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "codex-home"
    project = tmp_path / "project"
    project.mkdir()
    rollout = _rollout(home, thread_id="thread-1", project=project)
    original = state_discovery._session_metadata

    def read_then_remove(path: Path) -> dict[str, object]:
        metadata = original(path)
        if path == rollout:
            path.unlink()
        return metadata

    monkeypatch.setattr(state_discovery, "_session_metadata", read_then_remove)

    assert discover_rollouts(
        home,
        project_allowlist=(project,),
        thread_allowlist=frozenset({"thread-1"}),
    ) == ()


def test_discovery_still_fails_closed_for_invalid_metadata(tmp_path: Path) -> None:
    home = tmp_path / "codex-home"
    project = tmp_path / "project"
    project.mkdir()
    path = home / "sessions" / "bad.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("not-json\n", encoding="utf-8")

    with pytest.raises(DiscoveryError, match="invalid JSONL prefix"):
        discover_rollouts(
            home,
            project_allowlist=(project,),
            thread_allowlist=frozenset({"thread-1"}),
        )


@pytest.mark.parametrize("content", [b"", b'{"type":"session_'])
def test_discovery_skips_rollout_while_first_record_is_incomplete(
    tmp_path: Path, content: bytes
) -> None:
    home = tmp_path / "codex-home"
    project = tmp_path / "project"
    project.mkdir()
    path = home / "sessions" / "new.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(content)

    assert discover_rollouts(
        home,
        project_allowlist=(project,),
        thread_allowlist=frozenset({"thread-1"}),
    ) == ()
