"""Allowlist-only normalization of persisted Codex rollout records."""

from __future__ import annotations

import re
from hashlib import sha256
from typing import Any

from ..image_content import decode_image_data_url
from ..models import EmbeddedImage, EventKind, NormalizedEvent


class RolloutRecordError(ValueError):
    """A complete rollout record is malformed or control-relevant but unknown."""


class UnsupportedRolloutVersion(RolloutRecordError):
    """The persisted rollout version is not in the pinned compatibility set."""


class RolloutNormalizer:
    """Converts explicit user-visible assistant and user messages.

    Unknown record types are ignored. Assistant messages with an unknown phase
    fail closed because treating them as commentary or final would be a content
    classification guess. User attachment wrappers are reduced to the request
    plus path-free attachment labels.
    """

    _USER_REQUEST_MARKER = "\n## My request:\n"
    _ATTACHED_FILE = re.compile(r"^## ([^:\r\n]+): [^\r\n]+$", re.MULTILINE)
    _IMAGE_REFERENCE = re.compile(
        r'<image\s+name=.*?\s+path="[^"]+">\s*', re.IGNORECASE
    )
    _CODEX_DELEGATION = re.compile(
        r"\A<codex_delegation>\s*"
        r"<source_thread_id>[0-9a-fA-F-]{36}</source_thread_id>\s*"
        r"<input>(?P<input>.*)</input>\s*"
        r"</codex_delegation>\Z",
        re.DOTALL,
    )
    _IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})

    def __init__(self, supported_versions: frozenset[str] | None = None) -> None:
        self.supported_versions = supported_versions or frozenset({"1"})

    def normalize(self, record: dict[str, Any]) -> NormalizedEvent | None:
        if not isinstance(record, dict):
            raise RolloutRecordError("rollout record must be an object")
        record_type = record.get("type")
        payload = record.get("payload")
        if record_type == "session_meta":
            self._validate_session_meta(payload)
            return None
        if not isinstance(payload, dict):
            if record_type in {"event_msg", "response_item"}:
                raise RolloutRecordError(f"{record_type} payload must be an object")
            return None
        if record_type == "response_item":
            return self._normalize_response_item(record, payload)
        if record_type == "event_msg":
            return self._normalize_event_message(record, payload)
        return None

    def _validate_session_meta(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise RolloutRecordError("session_meta payload must be an object")
        version = str(payload.get("rollout_version", payload.get("version", "1")))
        if version not in self.supported_versions:
            raise UnsupportedRolloutVersion(f"unsupported rollout version: {version}")

    def _normalize_response_item(
        self, record: dict[str, Any], payload: dict[str, Any]
    ) -> NormalizedEvent | None:
        if payload.get("type") in {"custom_tool_call_output", "function_call_output"}:
            images = self._extract_content_images(payload.get("output"))
            if not images:
                return None
            return self._event(
                record,
                payload,
                EventKind.COMMENTARY,
                "",
                "visible_tool_output",
                images,
            )
        if payload.get("type") != "message":
            return None
        role = payload.get("role")
        if role == "user":
            content = payload.get("content")
            images = self._extract_content_images(content)
            text = self._extract_user_text(content, has_images=bool(images))
            if not text and not images:
                return None
            return self._event(
                record,
                payload,
                EventKind.COMMENTARY,
                text,
                "user_message",
                images,
            )
        if role != "assistant":
            return None
        phase = payload.get("phase")
        kind = self._kind_from_phase(phase)
        content = payload.get("content")
        text = self._extract_content_text(content)
        images = self._extract_content_images(content)
        if not text and not images:
            return None
        return self._event(record, payload, kind, text, "response_item", images)

    def _normalize_event_message(
        self, record: dict[str, Any], payload: dict[str, Any]
    ) -> NormalizedEvent | None:
        if payload.get("type") != "agent_message":
            return None
        # Current Desktop rollouts emit an id-less event_msg immediately before
        # the authoritative response_item. Without a persisted item identity it
        # cannot participate in exactly-once delivery, so it is ignored rather
        # than assigned a synthetic identity.
        if not (payload.get("item_id") or payload.get("id") or record.get("item_id")):
            return None
        phase = payload.get("phase")
        if phase is None:
            return None
        kind = self._kind_from_phase(phase)
        text = payload.get("message")
        if not isinstance(text, str) or not text:
            return None
        return self._event(record, payload, kind, text, "event_msg")

    @staticmethod
    def _kind_from_phase(phase: Any) -> EventKind:
        if phase == "commentary":
            return EventKind.COMMENTARY
        if phase == "final_answer":
            return EventKind.FINAL_ANSWER
        raise RolloutRecordError(f"unknown assistant message phase: {phase!r}")

    @staticmethod
    def _extract_content_text(content: Any) -> str:
        if not isinstance(content, list):
            return ""
        pieces: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") not in {"output_text", "text"}:
                continue
            text = part.get("text")
            if isinstance(text, str):
                pieces.append(text)
        return "".join(pieces)

    @classmethod
    def _extract_user_text(cls, content: Any, *, has_images: bool) -> str:
        if not isinstance(content, list):
            return ""
        pieces = [
            part.get("text")
            for part in content
            if isinstance(part, dict)
            and part.get("type") == "input_text"
            and isinstance(part.get("text"), str)
        ]
        raw = "".join(pieces)
        attached_names: list[str] = []
        if cls._USER_REQUEST_MARKER in raw:
            preamble, raw = raw.rsplit(cls._USER_REQUEST_MARKER, 1)
            attached_names = cls._ATTACHED_FILE.findall(preamble)
        raw = cls._IMAGE_REFERENCE.sub("", raw).strip("\r\n")
        if raw.startswith("<codex_delegation>"):
            delegation = cls._CODEX_DELEGATION.fullmatch(raw)
            if delegation is None:
                # A malformed internal envelope must not leak task identifiers
                # or routing metadata into Feishu.
                return ""
            raw = delegation.group("input").strip("\r\n")
        labels: list[str] = []
        seen: set[str] = set()
        for attached_name in attached_names:
            safe_name = attached_name.replace("\\", "/").rsplit("/", 1)[-1].strip()
            if not safe_name or safe_name in seen:
                continue
            seen.add(safe_name)
            suffix = "." + safe_name.rsplit(".", 1)[-1].casefold() if "." in safe_name else ""
            if has_images and suffix in cls._IMAGE_SUFFIXES:
                continue
            labels.append(f"📎【{safe_name}】")
        if labels:
            label_text = "\n".join(labels)
            raw = f"{raw}\n\n{label_text}" if raw else label_text
        return raw

    @staticmethod
    def _extract_content_images(content: Any) -> tuple[EmbeddedImage, ...]:
        if not isinstance(content, list):
            return ()
        images: list[EmbeddedImage] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") not in {"input_image", "output_image", "image"}:
                continue
            image_url = part.get("image_url")
            if not isinstance(image_url, str):
                raise RolloutRecordError("visible image lacks an image_url")
            if not image_url.startswith("data:"):
                # Remote image fetching is intentionally outside the bridge's
                # authority. Markdown URLs remain available through text flow.
                continue
            try:
                mime_type, suffix, data = decode_image_data_url(image_url)
            except ValueError as exc:
                raise RolloutRecordError(str(exc)) from exc
            digest = sha256(data).hexdigest()
            images.append(EmbeddedImage(mime_type, suffix, data, digest))
        return tuple(images)

    @staticmethod
    def _event(
        record: dict[str, Any],
        payload: dict[str, Any],
        kind: EventKind,
        text: str,
        source_type: str,
        images: tuple[EmbeddedImage, ...] = (),
    ) -> NormalizedEvent:
        thread_value = payload.get("thread_id") or record.get("thread_id")
        explicit_turn = payload.get("turn_id")
        metadata = payload.get("internal_chat_message_metadata_passthrough")
        nested_turn = None
        if metadata is not None:
            if not isinstance(metadata, dict):
                raise RolloutRecordError("internal message metadata must be an object")
            nested_turn = metadata.get("turn_id")
            if nested_turn is not None and (
                not isinstance(nested_turn, str) or not nested_turn
            ):
                raise RolloutRecordError("internal message metadata has an invalid turn id")
        if explicit_turn is not None and (
            not isinstance(explicit_turn, str) or not explicit_turn
        ):
            raise RolloutRecordError("visible assistant event has an invalid turn id")
        if explicit_turn is not None and nested_turn is not None and explicit_turn != nested_turn:
            raise RolloutRecordError("visible assistant event has conflicting turn identities")
        turn_value = explicit_turn or nested_turn or record.get("turn_id")
        if not isinstance(thread_value, str) or not thread_value:
            raise RolloutRecordError("visible assistant event lacks a trusted thread id")
        if not isinstance(turn_value, str) or not turn_value:
            raise RolloutRecordError("visible assistant event lacks a trusted turn id")
        thread_id = thread_value
        turn_id = turn_value
        explicit_item_id = payload.get("item_id") or payload.get("id") or record.get("item_id")
        if explicit_item_id is None:
            raise RolloutRecordError("visible assistant event lacks a trusted item id")
        item_id = str(explicit_item_id)
        if not item_id:
            raise RolloutRecordError("visible assistant event has an empty item id")
        revision = payload.get("revision", record.get("revision", 0))
        if not isinstance(revision, int) or revision < 0:
            raise RolloutRecordError("revision must be a non-negative integer")
        return NormalizedEvent(
            thread_id=thread_id,
            turn_id=turn_id,
            item_id=item_id,
            kind=kind,
            revision=revision,
            text=text,
            source_type=source_type,
            images=images,
        )
