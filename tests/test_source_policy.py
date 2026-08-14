from codex_feishu_bridge.codex.source_policy import may_emit
from codex_feishu_bridge.models import ObservationSource, OwnershipState


def test_desktop_threads_emit_from_rollout_only() -> None:
    assert may_emit(ObservationSource.ROLLOUT, OwnershipState.DESKTOP_MIRROR_ONLY)
    assert not may_emit(ObservationSource.APP_SERVER, OwnershipState.DESKTOP_MIRROR_ONLY)


def test_bridge_threads_emit_from_app_server_only() -> None:
    assert may_emit(ObservationSource.APP_SERVER, OwnershipState.BRIDGE_OWNED)
    assert not may_emit(ObservationSource.ROLLOUT, OwnershipState.BRIDGE_OWNED)
    assert not may_emit(ObservationSource.ROLLOUT, OwnershipState.UNKNOWN)
