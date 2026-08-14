"""Feishu transport adapters governed by a frozen tenant contract."""

from .contracts import EndpointContract, TenantContract
from .client import FeishuClient

__all__ = ["EndpointContract", "TenantContract", "FeishuClient"]
