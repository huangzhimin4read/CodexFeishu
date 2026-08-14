"""Independent global, endpoint, and user-chat token buckets."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from .contracts import EndpointContract


@dataclass(slots=True)
class TokenBucket:
    capacity: float
    refill_per_second: float
    tokens: float
    updated_at: float

    @classmethod
    def create(cls, capacity: float, refill_per_second: float) -> "TokenBucket":
        return cls(capacity, refill_per_second, capacity, time.monotonic())

    def delay(self, amount: float = 1.0) -> float:
        now = time.monotonic()
        self.tokens = min(
            self.capacity,
            self.tokens + (now - self.updated_at) * self.refill_per_second,
        )
        self.updated_at = now
        if self.tokens >= amount:
            return 0.0
        return (amount - self.tokens) / self.refill_per_second

    def consume(self, amount: float = 1.0) -> None:
        if self.tokens < amount:
            raise RuntimeError("token bucket consumed without a reservation")
        self.tokens -= amount


class RateLimiter:
    def __init__(self, *, global_capacity: int = 20, global_refill: float = 20.0) -> None:
        self._global = TokenBucket.create(global_capacity, global_refill)
        self._endpoint: dict[str, TokenBucket] = {}
        self._chat: dict[tuple[str, str], TokenBucket] = {}
        self._lock = threading.Lock()

    def wait(self, endpoint: EndpointContract, chat_id: str | None = None) -> None:
        while True:
            with self._lock:
                endpoint_bucket = self._endpoint.setdefault(
                    endpoint.name,
                    TokenBucket.create(
                        endpoint.rate_limit.capacity,
                        endpoint.rate_limit.refill_per_second,
                    ),
                )
                buckets = [self._global, endpoint_bucket]
                if chat_id and endpoint.rate_limit.user_chat_capacity is not None:
                    chat_bucket = self._chat.setdefault(
                        (endpoint.name, chat_id),
                        TokenBucket.create(
                            endpoint.rate_limit.user_chat_capacity,
                            endpoint.rate_limit.user_chat_refill_per_second or 1.0,
                        ),
                    )
                    buckets.append(chat_bucket)
                waits = [bucket.delay() for bucket in buckets]
                delay = max(waits)
                if delay <= 0:
                    for bucket in buckets:
                        bucket.consume()
                    return
            time.sleep(min(delay, 1.0))
