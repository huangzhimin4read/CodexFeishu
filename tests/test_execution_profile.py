from pathlib import Path

import json
import pytest
from jsonschema import Draft7Validator

from codex_feishu_bridge.codex.execution_profile import (
    ExecutionProfile,
    ProfileError,
    SandboxType,
)


ROOT = Path(__file__).parents[1]
TURN_SCHEMA = json.loads(
    (ROOT / "generated/codex/0.145.0/stable/v2/TurnStartParams.json").read_text(
        encoding="utf-8"
    )
)


def _validate(profile: ExecutionProfile) -> dict:
    params = profile.turn_start_params(
        thread_id="thread-1", text="hello", client_user_message_id="attempt-1"
    )
    Draft7Validator(TURN_SCHEMA).validate(params)
    return params


def test_read_only_network_is_stable_and_has_no_roots(tmp_path: Path) -> None:
    params = _validate(
        ExecutionProfile(
            sandbox_type=SandboxType.READ_ONLY,
            cwd=tmp_path,
            network_access=True,
        )
    )
    assert params["sandboxPolicy"] == {"type": "readOnly", "networkAccess": True}


def test_workspace_write_carries_network_and_roots(tmp_path: Path) -> None:
    params = _validate(
        ExecutionProfile(
            sandbox_type=SandboxType.WORKSPACE_WRITE,
            cwd=tmp_path,
            network_access=False,
            writable_roots=(tmp_path,),
        )
    )
    assert params["sandboxPolicy"]["type"] == "workspaceWrite"
    assert params["sandboxPolicy"]["networkAccess"] is False
    assert params["sandboxPolicy"]["writableRoots"] == [str(tmp_path.resolve())]


def test_danger_full_access_has_no_false_boundaries(tmp_path: Path) -> None:
    params = _validate(
        ExecutionProfile(
            sandbox_type=SandboxType.DANGER_FULL_ACCESS,
            cwd=tmp_path,
            network_access=None,
        )
    )
    assert params["sandboxPolicy"] == {"type": "dangerFullAccess"}
    with pytest.raises(ProfileError, match="network setting"):
        ExecutionProfile(
            sandbox_type=SandboxType.DANGER_FULL_ACCESS,
            cwd=tmp_path,
            network_access=False,
        )


def test_roots_are_rejected_for_read_only(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match="does not accept"):
        ExecutionProfile(
            sandbox_type=SandboxType.READ_ONLY,
            cwd=tmp_path,
            writable_roots=(tmp_path,),
        )
