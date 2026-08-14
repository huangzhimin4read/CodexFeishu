"""Single-source policy for normalized Codex events."""

from __future__ import annotations

from ..models import ObservationSource, OwnershipState


class SourcePolicyError(RuntimeError):
    """A source attempted to act outside its ownership boundary."""


def may_emit(source: ObservationSource, ownership: OwnershipState) -> bool:
    """Return whether a normalized event may enter the outbound logical stream.

    Rollout observations for bridge-owned turns are audit-only. App Server
    events are never accepted as the outbound source for Desktop mirror-only
    threads. Unknown ownership emits nothing.
    """

    if ownership is OwnershipState.UNKNOWN:
        return False
    if ownership is OwnershipState.DESKTOP_MIRROR_ONLY:
        return source is ObservationSource.ROLLOUT
    if ownership in {OwnershipState.BRIDGE_IDLE, OwnershipState.BRIDGE_OWNED}:
        return source is ObservationSource.APP_SERVER
    raise SourcePolicyError(f"unhandled ownership state: {ownership}")
