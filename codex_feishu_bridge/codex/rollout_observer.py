"""Incremental complete-record reader for rollout JSONL files."""

from __future__ import annotations

import json
import os
from hashlib import sha256
from pathlib import Path

from ..models import RolloutBatch, SourceCursor
from .normalizer import RolloutNormalizer, RolloutRecordError


class RolloutReadError(RuntimeError):
    """The source cannot be advanced safely."""


class SourceReplacedError(RolloutReadError):
    """A source identity or size changed behind a committed cursor."""


def file_identity_from_stat(stat: os.stat_result) -> str:
    return f"{stat.st_dev}:{stat.st_ino}"


def _last_committed_record_hash(handle: object, committed_offset: int) -> str | None:
    if committed_offset == 0:
        return None
    # Locate the previous complete record without rereading the whole rollout.
    binary = handle
    end = committed_offset
    binary.seek(end - 1)
    if binary.read(1) == b"\n":
        end -= 1
    position = end
    start = 0
    block_size = 8192
    while position > 0:
        block_start = max(0, position - block_size)
        binary.seek(block_start)
        block = binary.read(position - block_start)
        index = block.rfind(b"\n")
        if index >= 0:
            start = block_start + index + 1
            break
        position = block_start
    binary.seek(start)
    body = binary.read(end - start).rstrip(b"\r")
    return sha256(body).hexdigest()


class IncrementalRolloutReader:
    def __init__(self, normalizer: RolloutNormalizer | None = None) -> None:
        self.normalizer = normalizer or RolloutNormalizer()
        self._source_states: dict[
            str, tuple[str, int, str | None, frozenset[str]]
        ] = {}

    @staticmethod
    def _state_before_offset(
        handle: object, committed_offset: int
    ) -> tuple[str | None, set[str]]:
        """Recover explicit turn context and activity after a process restart."""
        if committed_offset <= 0:
            return None, set()
        handle.seek(0)
        consumed = 0
        current_turn: str | None = None
        active_turns: set[str] = set()
        while consumed < committed_offset:
            raw_line = handle.readline()
            if not raw_line:
                break
            consumed += len(raw_line)
            if consumed > committed_offset:
                raise SourceReplacedError("committed offset splits a rollout record")
            try:
                record = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RolloutReadError("invalid JSONL before committed cursor") from exc
            if not isinstance(record, dict):
                continue
            record_type = record.get("type")
            payload = record.get("payload")
            if record_type == "turn_context":
                turn_id = payload.get("turn_id") if isinstance(payload, dict) else None
                if not isinstance(turn_id, str) or not turn_id:
                    raise RolloutReadError("turn_context lacks a trusted turn id")
                current_turn = turn_id
            elif record_type == "event_msg" and isinstance(payload, dict):
                event_type = payload.get("type")
                if event_type in {"task_started", "task_complete"}:
                    turn_id = payload.get("turn_id")
                    if not isinstance(turn_id, str) or not turn_id:
                        raise RolloutReadError(f"{event_type} lacks a trusted turn id")
                    if event_type == "task_started":
                        active_turns.add(turn_id)
                    else:
                        active_turns.discard(turn_id)
        if consumed != committed_offset:
            raise SourceReplacedError("committed offset is not a rollout record boundary")
        return current_turn, active_turns

    def active_turn_ids(self, path: Path) -> frozenset[str]:
        state = self._source_states.get(str(path.resolve()))
        return state[3] if state is not None else frozenset()

    def read(
        self,
        path: Path,
        cursor: SourceCursor | None = None,
        *,
        expected_thread_id: str | None = None,
    ) -> RolloutBatch:
        source = path.resolve()
        source_path = str(source)
        events = []
        ignored = 0
        consumed = 0
        initial_read = cursor is None
        with source.open("rb") as handle:
            stat = os.fstat(handle.fileno())
            identity = file_identity_from_stat(stat)
            if cursor is None:
                cursor = SourceCursor(
                    source_path=source_path, file_id=identity, schema_version="unverified"
                )
            elif cursor.source_path != source_path:
                raise SourceReplacedError("cursor belongs to another source path")
            elif cursor.file_id != identity:
                raise SourceReplacedError("rollout file identity changed")
            if stat.st_size < cursor.committed_offset:
                raise SourceReplacedError("rollout file was truncated before committed offset")
            if cursor.last_record_hash is not None:
                actual = _last_committed_record_hash(handle, cursor.committed_offset)
                if actual != cursor.last_record_hash:
                    raise SourceReplacedError("last committed rollout record changed")
            handle.seek(cursor.committed_offset)
            base_offset = cursor.committed_offset
            last_hash = cursor.last_record_hash
            verified_version = cursor.schema_version
            session_verified = (
                not initial_read
                and verified_version in self.normalizer.supported_versions
            )
            session_thread_id = expected_thread_id if session_verified else None
            cached = self._source_states.get(source_path)
            if cached is not None and cached[:2] == (identity, cursor.committed_offset):
                current_turn_id = cached[2]
                active_turn_ids = set(cached[3])
            elif initial_read:
                current_turn_id = None
                active_turn_ids = set()
            else:
                current_turn_id, active_turn_ids = self._state_before_offset(
                    handle, cursor.committed_offset
                )
                handle.seek(cursor.committed_offset)
            while True:
                raw_line = handle.readline()
                if not raw_line or not raw_line.endswith(b"\n"):
                    break
                consumed += len(raw_line)
                body = raw_line.rstrip(b"\r\n")
                if not body:
                    ignored += 1
                    last_hash = sha256(body).hexdigest()
                    continue
                try:
                    record = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise RolloutReadError(
                        "invalid complete JSONL record at byte "
                        f"{base_offset + consumed - len(raw_line)}"
                    ) from exc
                if not isinstance(record, dict):
                    raise RolloutReadError("rollout record must be an object")
                if record.get("type") == "session_meta":
                    payload = record.get("payload")
                    if not isinstance(payload, dict):
                        raise RolloutReadError("session_meta payload must be an object")
                    version = str(
                        payload.get("rollout_version", payload.get("version", "1"))
                    )
                    thread_id = payload.get("id") or payload.get("thread_id")
                    if version not in self.normalizer.supported_versions:
                        raise RolloutReadError(f"unsupported rollout version: {version}")
                    if not isinstance(thread_id, str) or not thread_id:
                        raise RolloutReadError("session_meta lacks a thread identity")
                    if expected_thread_id is not None and thread_id != expected_thread_id:
                        raise RolloutReadError(
                            "session thread identity differs from discovery binding"
                        )
                    if session_verified and verified_version != version:
                        raise RolloutReadError("rollout schema changed within one source")
                    session_verified = True
                    verified_version = version
                    session_thread_id = thread_id
                elif record.get("type") == "turn_context":
                    payload = record.get("payload")
                    turn_id = payload.get("turn_id") if isinstance(payload, dict) else None
                    if not isinstance(turn_id, str) or not turn_id:
                        raise RolloutReadError("turn_context lacks a trusted turn id")
                    current_turn_id = turn_id
                elif not session_verified:
                    raise RolloutReadError(
                        "rollout source was not preceded by verified session metadata"
                    )
                payload = record.get("payload")
                if record.get("type") == "event_msg" and isinstance(payload, dict):
                    event_type = payload.get("type")
                    if event_type in {"task_started", "task_complete"}:
                        activity_turn = payload.get("turn_id")
                        if not isinstance(activity_turn, str) or not activity_turn:
                            raise RolloutReadError(
                                f"{event_type} lacks a trusted turn id"
                            )
                        if event_type == "task_started":
                            active_turn_ids.add(activity_turn)
                        else:
                            active_turn_ids.discard(activity_turn)
                if isinstance(payload, dict) and record.get("type") in {
                    "response_item",
                    "event_msg",
                }:
                    explicit_turn = payload.get("turn_id")
                    metadata = payload.get("internal_chat_message_metadata_passthrough")
                    nested_turn = None
                    if metadata is not None:
                        if not isinstance(metadata, dict):
                            raise RolloutReadError(
                                "internal message metadata must be an object"
                            )
                        nested_turn = metadata.get("turn_id")
                        if nested_turn is not None and (
                            not isinstance(nested_turn, str) or not nested_turn
                        ):
                            raise RolloutReadError(
                                "internal message metadata has an invalid turn id"
                            )
                    if explicit_turn is not None and nested_turn is not None and explicit_turn != nested_turn:
                        raise RolloutReadError(
                            "rollout record has conflicting turn identities"
                        )
                    if payload.get("thread_id") is None and session_thread_id is not None:
                        record = dict(record)
                        payload = dict(payload)
                        payload["thread_id"] = session_thread_id
                        if payload.get("turn_id") is None:
                            if nested_turn is not None:
                                payload["turn_id"] = nested_turn
                            elif current_turn_id is not None:
                                payload["turn_id"] = current_turn_id
                        record["payload"] = payload
                    elif payload.get("turn_id") is None and (
                        nested_turn is not None or current_turn_id is not None
                    ):
                        record = dict(record)
                        payload = dict(payload)
                        payload["turn_id"] = nested_turn or current_turn_id
                        record["payload"] = payload
                try:
                    event = self.normalizer.normalize(record)
                except RolloutRecordError as exc:
                    raise RolloutReadError(str(exc)) from exc
                last_hash = sha256(body).hexdigest()
                if event is None:
                    ignored += 1
                else:
                    events.append(event)

        new_cursor = SourceCursor(
            source_path=source_path,
            file_id=identity,
            committed_offset=base_offset + consumed,
            last_record_hash=last_hash,
            schema_version=verified_version,
        )
        frozen_active_turns = frozenset(active_turn_ids)
        self._source_states[source_path] = (
            identity,
            new_cursor.committed_offset,
            current_turn_id,
            frozen_active_turns,
        )
        return RolloutBatch(
            events=tuple(events),
            cursor=new_cursor,
            ignored_records=ignored,
            active_turn_ids=frozen_active_turns,
        )
