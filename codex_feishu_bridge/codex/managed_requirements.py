"""Strict parsing and enforcement of App Server managed requirements."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from jsonschema import Draft7Validator

from ..security.jcs import canonicalize
from .execution_profile import ApprovalPolicy, ExecutionProfile, SandboxType


class ManagedRequirementsError(RuntimeError):
    pass


_ALLOWED_FIELDS = {
    "allowAppshots",
    "allowManagedHooksOnly",
    "allowRemoteControl",
    "allowedApprovalPolicies",
    "allowedPermissionProfiles",
    "allowedSandboxModes",
    "allowedWebSearchModes",
    "allowedWindowsSandboxImplementations",
    "computerUse",
    "defaultPermissions",
    "enforceResidency",
    "featureRequirements",
    "models",
}


@dataclass(frozen=True, slots=True)
class ManagedRequirements:
    raw: dict[str, Any] | None
    requirements_hash: str

    @classmethod
    def parse(cls, result: dict[str, Any], schema_path: Path) -> "ManagedRequirements":
        with schema_path.resolve().open(encoding="utf-8") as handle:
            validator = Draft7Validator(json.load(handle))
        errors = sorted(validator.iter_errors(result), key=lambda item: list(item.path))
        if errors:
            raise ManagedRequirementsError(errors[0].message)
        requirements = result.get("requirements")
        if requirements is not None:
            if not isinstance(requirements, dict):
                raise ManagedRequirementsError("requirements must be an object or null")
            extra = set(requirements) - _ALLOWED_FIELDS
            if extra:
                raise ManagedRequirementsError(f"unknown managed requirement fields: {sorted(extra)}")
        return cls(requirements, sha256(canonicalize(requirements)).hexdigest())

    def enforce(self, profile: ExecutionProfile, *, remote_dispatch: bool) -> None:
        if self.raw is None:
            return
        if remote_dispatch and self.raw.get("allowRemoteControl") is False:
            raise ManagedRequirementsError("managed policy denies remote control")
        allowed_policies = self.raw.get("allowedApprovalPolicies")
        if isinstance(allowed_policies, list) and profile.approval_policy.value not in allowed_policies:
            raise ManagedRequirementsError("approval policy is not managed-policy allowed")
        allowed_sandboxes = self.raw.get("allowedSandboxModes")
        sandbox_name = {
            SandboxType.READ_ONLY: "read-only",
            SandboxType.WORKSPACE_WRITE: "workspace-write",
            SandboxType.DANGER_FULL_ACCESS: "danger-full-access",
        }[profile.sandbox_type]
        if isinstance(allowed_sandboxes, list) and sandbox_name not in allowed_sandboxes:
            raise ManagedRequirementsError("sandbox mode is not managed-policy allowed")
        permission_profiles = self.raw.get("allowedPermissionProfiles")
        if permission_profiles is not None and not isinstance(permission_profiles, dict):
            raise ManagedRequirementsError("allowedPermissionProfiles has an invalid shape")
