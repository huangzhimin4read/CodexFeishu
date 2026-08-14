from pathlib import Path

import pytest

from codex_feishu_bridge.codex.execution_profile import (
    ApprovalPolicy,
    ExecutionProfile,
    SandboxType,
)
from codex_feishu_bridge.codex.managed_requirements import (
    ManagedRequirements,
    ManagedRequirementsError,
)
from codex_feishu_bridge.commands import ControlCommand
from codex_feishu_bridge.controls import ControlError, ProfileController
from codex_feishu_bridge.runtime_storage import RuntimeStorage


ROOT = Path(__file__).parents[1]
SCHEMA = ROOT / "generated/codex/0.145.0/stable/v2/ConfigRequirementsReadResponse.json"


def test_managed_requirements_reject_denied_remote_and_sandbox(tmp_path: Path) -> None:
    profile = ExecutionProfile(SandboxType.READ_ONLY, tmp_path, network_access=False)
    requirements = ManagedRequirements.parse(
        {
            "requirements": {
                "allowRemoteControl": False,
                "allowedSandboxModes": ["workspace-write"],
                "allowedApprovalPolicies": ["never"],
            }
        },
        SCHEMA,
    )
    with pytest.raises(ManagedRequirementsError, match="remote control"):
        requirements.enforce(profile, remote_dispatch=True)


def test_managed_requirements_unknown_field_fails_closed() -> None:
    with pytest.raises(ManagedRequirementsError, match="unknown"):
        ManagedRequirements.parse({"requirements": {"futurePolicy": True}}, SCHEMA)


def test_profile_controller_persists_full_profile_and_blocks_full_access(tmp_path: Path) -> None:
    with RuntimeStorage(tmp_path / "db.sqlite") as storage:
        storage.initialize_runtime(sink_mode="control")
        default = ExecutionProfile(
            SandboxType.WORKSPACE_WRITE,
            tmp_path,
            network_access=False,
            writable_roots=(tmp_path,),
        )
        controller = ProfileController(storage, (tmp_path,), default)
        profile_hash = controller.apply("thread", ControlCommand("network", "on"))
        assert len(profile_hash) == 64 and controller.load("thread").network_access is True
        with pytest.raises(ControlError, match="distinct-principal"):
            controller.apply("thread", ControlCommand("sandbox", "dangerFullAccess"))
