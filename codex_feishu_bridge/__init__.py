"""Single-user Codex Desktop to Feishu/Lark topic-group bridge.

Runtime feature switches keep outbound mirroring, remote input, approvals, and
control commands independently auditable and durable across restarts.
"""

from .config import BridgeSettings

__all__ = ["BridgeSettings"]
__version__ = "0.1.0"
