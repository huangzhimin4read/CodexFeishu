"""Three-second Feishu long-connection handler adapters."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Callable

from .inbound import IngressRejected, IngressRouter


class EventHandlers:
    def __init__(
        self,
        ingress: IngressRouter | None,
        card_callback: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> None:
        self.ingress = ingress
        self.card_callback = card_callback

    def message_receive(self, event: Any) -> None:
        if self.ingress is None:
            return None
        envelope = _to_dict(event)
        try:
            self.ingress.ingest(envelope)
        except IngressRejected:
            # Provider handlers must return promptly. Body-free diagnostics are
            # persisted by the ingress circuit breaker where applicable.
            return None
        return None

    def card_action(self, event: Any) -> dict[str, Any]:
        try:
            return self.card_callback(_to_dict(event))
        except Exception:
            # Invalid, expired, duplicate, or drifted actions fail closed while
            # still returning within the provider callback deadline.
            return {"toast": {"type": "warning", "content": "该操作已失效或未通过校验"}}


def _to_dict(value: Any) -> dict[str, Any]:
    result = _plain(value)
    if isinstance(result, dict):
        return result
    raise TypeError("unsupported SDK event object")


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _plain(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(child) for child in value]
    if hasattr(value, "to_dict"):
        return _plain(value.to_dict())
    if hasattr(value, "__dict__"):
        return {
            key: _plain(child)
            for key, child in vars(value).items()
            if not key.startswith("_")
        }
    return str(value)
