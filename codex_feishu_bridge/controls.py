"""Next-turn execution-profile mutations."""

from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

from .codex.execution_profile import ApprovalPolicy, ExecutionProfile, SandboxType
from .commands import ControlCommand
from .runtime_storage import RuntimeStorage, utc_now
from .security.jcs import canonicalize
from .security.windows_paths import capture_path_identity


class ControlError(RuntimeError):
    pass


class ProfileController:
    def __init__(
        self,
        storage: RuntimeStorage,
        allowed_roots: tuple[Path, ...],
        default_profile: ExecutionProfile,
    ) -> None:
        self.storage = storage
        self.allowed_roots = allowed_roots
        self.default_profile = default_profile

    def load(self, thread_id: str) -> ExecutionProfile:
        row = self.storage.connection.execute(
            "SELECT p.canonical_json FROM thread_profiles t JOIN execution_profiles p "
            "ON p.profile_hash=t.profile_hash WHERE t.thread_id=?", (thread_id,)
        ).fetchone()
        if row is None:
            return self.default_profile
        value = json.loads(bytes(row[0]).decode("utf-8"))
        return ExecutionProfile(
            sandbox_type=SandboxType(value["sandbox_type"]),
            cwd=Path(value["cwd"]),
            approval_policy=ApprovalPolicy(value["approval_policy"]),
            approvals_reviewer=value["approvals_reviewer"],
            network_access=value["network_access"],
            writable_roots=tuple(Path(item) for item in value["writable_roots"]),
        )

    def apply(self, thread_id: str, command: ControlCommand) -> str:
        profile = self.load(thread_id)
        if command.name == "sandbox":
            sandbox = SandboxType(command.argument)
            profile = replace(
                profile,
                sandbox_type=sandbox,
                network_access=None if sandbox is SandboxType.DANGER_FULL_ACCESS else False,
                writable_roots=() if sandbox is not SandboxType.WORKSPACE_WRITE else profile.writable_roots,
            )
        elif command.name == "network":
            if profile.sandbox_type is SandboxType.DANGER_FULL_ACCESS:
                raise ControlError("dangerFullAccess has no Codex-enforced network Boolean")
            profile = replace(profile, network_access=command.argument == "on")
        elif command.name == "approval-policy":
            profile = replace(profile, approval_policy=ApprovalPolicy(command.argument))
        elif command.name == "cwd":
            identity = capture_path_identity(command.argument or "", self.allowed_roots)
            profile = replace(profile, cwd=identity.canonical_path)
        elif command.name == "writable":
            if profile.sandbox_type is not SandboxType.WORKSPACE_WRITE:
                raise ControlError("writable roots apply only to workspaceWrite")
            identity = capture_path_identity(command.argument or "", self.allowed_roots)
            profile = replace(profile, writable_roots=(identity.canonical_path,))
        else:
            raise ControlError("command is not an execution-profile mutation")
        return self.persist(thread_id, profile)

    def persist(self, thread_id: str, profile: ExecutionProfile) -> str:
        raw = {
            "sandbox_type": profile.sandbox_type.value,
            "cwd": str(profile.cwd),
            "approval_policy": profile.approval_policy.value,
            "approvals_reviewer": profile.approvals_reviewer,
            "network_access": profile.network_access,
            "writable_roots": [str(path) for path in profile.writable_roots],
        }
        encoded = canonicalize(raw)
        profile_hash = sha256(encoded).hexdigest()
        with self.storage.immediate() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO execution_profiles(profile_id,profile_hash,canonical_json,created_at) "
                "VALUES(?,?,?,?)",
                (profile_hash, profile_hash, encoded, utc_now()),
            )
            current = connection.execute(
                "SELECT profile_epoch FROM thread_profiles WHERE thread_id=?", (thread_id,)
            ).fetchone()
            epoch = int(current[0]) + 1 if current else 1
            connection.execute(
                "INSERT INTO thread_profiles(thread_id,profile_hash,profile_epoch,reconciliation_state,updated_at) "
                "VALUES(?,?,?,'pending',?) ON CONFLICT(thread_id) DO UPDATE SET "
                "profile_hash=excluded.profile_hash,profile_epoch=excluded.profile_epoch,"
                "reconciliation_state='pending',updated_at=excluded.updated_at",
                (thread_id, profile_hash, epoch, utc_now()),
            )
        return profile_hash
