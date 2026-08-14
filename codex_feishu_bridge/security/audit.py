"""Body-free tamper-evident audit chain."""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from ..runtime_storage import RuntimeStorage, utc_now
from .jcs import canonicalize, digest


_FORBIDDEN_KEYS = {
    "body",
    "content",
    "message",
    "payload",
    "prompt",
    "secret",
    "text",
    "token",
}


class AuditError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AuditRecord:
    sequence: int
    record_hash: str
    record_hmac: str


def _assert_body_free(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in _FORBIDDEN_KEYS:
                raise AuditError(f"forbidden audit field: {path}.{key}")
            _assert_body_free(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_body_free(child, f"{path}[{index}]")


class AuditChain:
    def __init__(self, storage: RuntimeStorage, key: bytes) -> None:
        if len(key) < 32:
            raise AuditError("audit key must contain at least 256 bits")
        self.storage = storage
        self.key = key

    def append(self, event: dict[str, Any]) -> AuditRecord:
        _assert_body_free(event)
        with self.storage.immediate() as connection:
            previous = connection.execute(
                "SELECT sequence,record_hmac FROM audit_chain ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            sequence = int(previous["sequence"]) + 1 if previous else 1
            previous_hmac = str(previous["record_hmac"]) if previous else None
            envelope = {
                "sequence": sequence,
                "created_at": utc_now(),
                "previous_hmac": previous_hmac,
                "event": event,
            }
            record_hash = digest(envelope)
            record_hmac = hmac.new(
                self.key,
                canonicalize({"previous_hmac": previous_hmac, "record_hash": record_hash}),
                sha256,
            ).hexdigest()
            connection.execute(
                "INSERT INTO audit_chain(sequence,previous_hmac,record_hash,record_hmac,created_at) "
                "VALUES(?,?,?,?,?)",
                (sequence, previous_hmac, record_hash, record_hmac, envelope["created_at"]),
            )
        return AuditRecord(sequence, record_hash, record_hmac)

    def verify(self) -> int:
        previous_hmac: str | None = None
        count = 0
        for row in self.storage.connection.execute(
            "SELECT sequence,previous_hmac,record_hash,record_hmac FROM audit_chain ORDER BY sequence"
        ):
            if row["sequence"] != count + 1 or row["previous_hmac"] != previous_hmac:
                raise AuditError("audit chain sequence/link mismatch")
            expected = hmac.new(
                self.key,
                canonicalize({"previous_hmac": previous_hmac, "record_hash": row["record_hash"]}),
                sha256,
            ).hexdigest()
            if not hmac.compare_digest(expected, row["record_hmac"]):
                raise AuditError("audit HMAC mismatch")
            previous_hmac = row["record_hmac"]
            count += 1
        return count
