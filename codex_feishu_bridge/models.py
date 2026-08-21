"""Typed core models shared by the M0 adapters and storage layer."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256


class EventKind(StrEnum):
    COMMENTARY = "commentary"
    FINAL_ANSWER = "final_answer"


class OwnershipState(StrEnum):
    DESKTOP_MIRROR_ONLY = "desktop_mirror_only"
    BRIDGE_IDLE = "bridge_idle"
    BRIDGE_OWNED = "bridge_owned"
    UNKNOWN = "unknown"


class DispatchState(StrEnum):
    RECEIVED = "received"
    CLAIMED = "claimed"
    DISPATCHING = "dispatching"
    ACCEPTED = "accepted"
    OUTCOME_UNKNOWN = "outcome_unknown"
    COMPLETED = "completed"


class TurnState(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    OUTCOME_UNKNOWN = "outcome_unknown"


@dataclass(frozen=True, slots=True)
class EmbeddedImage:
    mime_type: str
    suffix: str
    content: bytes
    content_hash: str


class DeliveryState(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    RETRYABLE = "retryable"
    PERMANENT = "permanent"
    UNKNOWN = "unknown"
    FINAL_UNDELIVERED = "final_undelivered"


class ApprovalState(StrEnum):
    ISSUED = "issued"
    ACTION_COMMITTED = "action_committed"
    RESPONSE_SENDING = "response_sending"
    RESOLVED = "resolved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    OUTCOME_UNKNOWN = "outcome_unknown"


@dataclass(frozen=True, slots=True)
class NormalizedEvent:
    thread_id: str
    turn_id: str
    item_id: str
    kind: EventKind
    revision: int
    text: str
    source_type: str
    images: tuple[EmbeddedImage, ...] = ()
    client_user_message_id: str | None = None

    @property
    def logical_key(self) -> str:
        material = "\x1f".join(
            (
                self.thread_id,
                self.turn_id,
                self.item_id,
                self.kind.value,
                str(self.revision),
            )
        )
        return sha256(material.encode("utf-8")).hexdigest()

    @property
    def content_hash(self) -> str:
        if not self.images:
            return sha256(self.text.encode("utf-8")).hexdigest()
        digest = sha256()
        digest.update(b"codex-visible-content-v2\x00")
        digest.update(self.text.encode("utf-8"))
        for image in self.images:
            digest.update(b"\x00image\x00")
            digest.update(image.mime_type.encode("ascii"))
            digest.update(b"\x00")
            digest.update(image.content_hash.encode("ascii"))
        return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class SourceCursor:
    source_path: str
    file_id: str
    committed_offset: int = 0
    last_record_hash: str | None = None
    schema_version: str = "1"


@dataclass(frozen=True, slots=True)
class RolloutBatch:
    events: tuple[NormalizedEvent, ...]
    cursor: SourceCursor
    ignored_records: int = 0
    active_turn_ids: frozenset[str] = frozenset()


class ObservationSource(StrEnum):
    APP_SERVER = "app_server"
    ROLLOUT = "rollout"
