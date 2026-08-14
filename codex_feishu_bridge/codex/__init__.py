"""Codex protocol, rollout, and controlled-dispatch adapters."""

from .shadow_observer import ShadowObserver

__all__ = ["ShadowObserver"]

from .app_server_client import AppServerProtocol, StdioAppServer
from .compatibility import CompatibilityMatrix
from .execution_profile import ExecutionProfile
from .normalizer import RolloutNormalizer
from .rollout_observer import IncrementalRolloutReader
from .source_policy import may_emit

__all__ = [
    "AppServerProtocol",
    "CompatibilityMatrix",
    "ExecutionProfile",
    "IncrementalRolloutReader",
    "RolloutNormalizer",
    "StdioAppServer",
    "may_emit",
]
