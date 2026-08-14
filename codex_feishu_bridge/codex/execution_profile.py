"""Exact, non-lossy construction of turn/start execution profiles."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class ProfileError(ValueError):
    """A requested execution profile cannot be represented truthfully."""


class SandboxType(StrEnum):
    READ_ONLY = "readOnly"
    WORKSPACE_WRITE = "workspaceWrite"
    DANGER_FULL_ACCESS = "dangerFullAccess"


class ApprovalPolicy(StrEnum):
    UNTRUSTED = "untrusted"
    ON_REQUEST = "on-request"
    NEVER = "never"


@dataclass(frozen=True, slots=True)
class ExecutionProfile:
    sandbox_type: SandboxType
    cwd: Path
    approval_policy: ApprovalPolicy = ApprovalPolicy.ON_REQUEST
    approvals_reviewer: str = "user"
    network_access: bool | None = False
    writable_roots: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        cwd = self.cwd.resolve()
        roots = tuple(root.resolve() for root in self.writable_roots)
        object.__setattr__(self, "cwd", cwd)
        object.__setattr__(self, "writable_roots", roots)
        if self.approvals_reviewer != "user":
            raise ProfileError("the owner-operated bridge routes approvals to user")
        if self.sandbox_type is SandboxType.DANGER_FULL_ACCESS:
            if self.network_access is not None:
                raise ProfileError(
                    "dangerFullAccess cannot claim a Codex-enforced network setting"
                )
            if roots:
                raise ProfileError(
                    "dangerFullAccess cannot claim Codex-enforced writable roots"
                )
        elif self.network_access is None:
            raise ProfileError("readOnly/workspaceWrite require an explicit Boolean network value")
        if self.sandbox_type is SandboxType.READ_ONLY and roots:
            raise ProfileError("readOnly does not accept writable roots")

    def sandbox_policy(self) -> dict[str, Any]:
        if self.sandbox_type is SandboxType.DANGER_FULL_ACCESS:
            return {"type": self.sandbox_type.value}
        result: dict[str, Any] = {
            "type": self.sandbox_type.value,
            "networkAccess": self.network_access,
        }
        if self.sandbox_type is SandboxType.WORKSPACE_WRITE:
            result["writableRoots"] = [str(path) for path in self.writable_roots]
        return result

    def turn_start_params(
        self,
        *,
        thread_id: str,
        text: str,
        client_user_message_id: str,
        model: str | None = None,
        input_items: tuple[dict[str, Any], ...] | None = None,
    ) -> dict[str, Any]:
        if not thread_id or not client_user_message_id:
            raise ProfileError("thread and client user message ids are required")
        items = input_items or ({"type": "text", "text": text},)
        if not items or not all(isinstance(item, dict) for item in items):
            raise ProfileError("turn input must contain at least one typed item")
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [dict(item) for item in items],
            "clientUserMessageId": client_user_message_id,
            "cwd": str(self.cwd),
            "approvalPolicy": self.approval_policy.value,
            "approvalsReviewer": self.approvals_reviewer,
            "sandboxPolicy": self.sandbox_policy(),
        }
        if model is not None:
            params["model"] = model
        return params
