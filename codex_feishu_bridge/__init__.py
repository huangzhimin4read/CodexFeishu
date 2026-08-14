"""Owner-operated Codex Desktop to Feishu topic-group bridge.

Runtime capability gates keep outbound mirroring, remote input, approvals, and
control commands independently auditable and fail closed.
"""

from .config import BridgeSettings

__all__ = ["BridgeSettings"]
__version__ = "0.1.0"
