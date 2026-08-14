"""Fenced Feishu dispatch through the Codex desktop-owned writer."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from ..runtime_storage import RuntimeStorage, utc_now
from ..security.jcs import canonicalize
from .controller import DispatchError, DispatchResult
from .desktop_gateway import CodexDesktopGateway, DesktopGatewayError


@dataclass(frozen=True, slots=True)
class _RecordedUserMessage:
    turn_id: str
    item_id: str


class DesktopCodexDispatcher:
    """Submit to the desktop UI, then prove acceptance from new rollout bytes."""

    def __init__(
        self,
        storage: RuntimeStorage,
        gateway: CodexDesktopGateway,
        *,
        codex_home: Path,
        authorize: Callable[..., tuple[int, int, int]],
        server_epoch: str,
        connection_epoch: str,
        rollout_confirmation_seconds: float = 20.0,
    ) -> None:
        self.storage = storage
        self.gateway = gateway
        self.codex_home = codex_home.resolve()
        self.authorize = authorize
        self.server_epoch = server_epoch
        self.connection_epoch = connection_epoch
        self.rollout_confirmation_seconds = rollout_confirmation_seconds

    def _rollout_snapshots(self, thread_id: str) -> dict[Path, int]:
        root = self.codex_home / "sessions"
        if not root.is_dir():
            return {}
        snapshots: dict[Path, int] = {}
        for path in root.rglob(f"*{thread_id}.jsonl"):
            try:
                snapshots[path.resolve()] = path.stat().st_size
            except OSError:
                continue
        return snapshots

    @staticmethod
    def _message_text(payload: dict[str, Any]) -> str | None:
        if payload.get("type") != "message" or payload.get("role") != "user":
            return None
        content = payload.get("content")
        if not isinstance(content, list):
            return None
        parts = [
            item.get("text")
            for item in content
            if isinstance(item, dict)
            and item.get("type") == "input_text"
            and isinstance(item.get("text"), str)
        ]
        return "".join(parts)

    @staticmethod
    def _matches_submitted_text(
        actual_text: str | None,
        expected_text: str,
        *,
        has_attachments: bool,
    ) -> bool:
        if actual_text is None:
            return False

        def without_one_terminal_newline(value: str) -> str:
            if value.endswith("\r\n"):
                return value[:-2]
            if value.endswith("\n"):
                return value[:-1]
            return value

        if actual_text == expected_text or (
            without_one_terminal_newline(actual_text)
            == without_one_terminal_newline(expected_text)
        ):
            return True
        if not has_attachments:
            return False
        marker = "\n## My request:\n"
        if marker not in actual_text:
            return False
        preamble, request_text = actual_text.rsplit(marker, 1)
        return (
            preamble.lstrip().startswith("# Files mentioned by the user:")
            and without_one_terminal_newline(request_text)
            == without_one_terminal_newline(expected_text)
        )

    def _find_new_user_turn(
        self,
        snapshots: dict[Path, int],
        *,
        thread_id: str,
        expected_text: str,
        has_attachments: bool,
    ) -> _RecordedUserMessage | None:
        current = self._rollout_snapshots(thread_id)
        for path, size in current.items():
            start = snapshots.get(path, 0)
            if size <= start:
                continue
            try:
                with path.open("rb") as handle:
                    handle.seek(start)
                    raw = handle.read()
            except OSError:
                continue
            for line in raw.splitlines():
                try:
                    record = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if record.get("type") != "response_item":
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict) or not self._matches_submitted_text(
                    self._message_text(payload),
                    expected_text,
                    has_attachments=has_attachments,
                ):
                    continue
                metadata = payload.get("internal_chat_message_metadata_passthrough")
                turn_id = metadata.get("turn_id") if isinstance(metadata, dict) else None
                item_id = payload.get("item_id") or payload.get("id")
                if (
                    isinstance(turn_id, str)
                    and turn_id
                    and isinstance(item_id, str)
                    and item_id
                ):
                    return _RecordedUserMessage(turn_id, item_id)
        return None

    def _wait_for_new_user_turn(
        self,
        snapshots: dict[Path, int],
        *,
        thread_id: str,
        expected_text: str,
        has_attachments: bool,
    ) -> _RecordedUserMessage | None:
        deadline = time.monotonic() + self.rollout_confirmation_seconds
        while time.monotonic() < deadline:
            match = self._find_new_user_turn(
                snapshots,
                thread_id=thread_id,
                expected_text=expected_text,
                has_attachments=has_attachments,
            )
            if match is not None:
                return match
            time.sleep(0.1)
        return None

    def dispatch(
        self,
        *,
        ingress_message_id: str,
        thread_id: str,
        text: str,
        required_capability: str,
        attachment_paths: Sequence[Path] = (),
    ) -> DispatchResult:
        binding_epoch, identity_epoch, fencing_token = self.authorize(
            thread_id,
            required_capability=required_capability,
        )
        canonical_attachments = tuple(
            str(Path(path).resolve(strict=True)) for path in attachment_paths
        )
        request = {
            "method": "desktop/submit",
            "params": {
                "threadId": thread_id,
                "text": text,
                "attachments": canonical_attachments,
            },
        }
        request_hash = sha256(canonicalize(request)).hexdigest()
        attempt_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"codex-feishu:{ingress_message_id}"))
        client_message_id = str(
            uuid.uuid5(uuid.NAMESPACE_OID, f"codex-feishu-desktop:{ingress_message_id}")
        )
        now = utc_now()
        with self.storage.immediate() as connection:
            existing = connection.execute(
                "SELECT request_hash,state,turn_id FROM dispatch_records WHERE dispatch_attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise DispatchError("desktop dispatch identity conflicts with another request")
                return DispatchResult(attempt_id, existing["turn_id"], existing["state"])
            connection.execute(
                "INSERT INTO dispatch_attempts(dispatch_attempt_id,state,updated_at) "
                "VALUES(?,'dispatching',?)",
                (attempt_id, now),
            )
            connection.execute(
                "INSERT INTO dispatch_records(dispatch_attempt_id,ingress_message_id,thread_id,"
                "client_user_message_id,profile_hash,binding_epoch,identity_binding_epoch,fencing_token,"
                "server_epoch,connection_epoch,request_hash,state,created_at,updated_at) "
                "VALUES(?,?,?,?,? ,?,?,?,?,?,?,'prepared',?,?)",
                (
                    attempt_id,
                    ingress_message_id,
                    thread_id,
                    client_message_id,
                    "desktop-host-managed",
                    binding_epoch,
                    identity_epoch,
                    fencing_token,
                    self.server_epoch,
                    self.connection_epoch,
                    request_hash,
                    now,
                    now,
                ),
            )

        snapshots = self._rollout_snapshots(thread_id)
        updated = self.storage.connection.execute(
            "UPDATE dispatch_records SET state='bytes_sending',request_id='desktop-ui',updated_at=? "
            "WHERE dispatch_attempt_id=? AND state='prepared' AND fencing_token=?",
            (utc_now(), attempt_id, fencing_token),
        )
        if updated.rowcount != 1:
            raise DispatchError("desktop dispatch lost its service fence")
        try:
            gateway_result = self.gateway.submit(
                thread_id,
                text,
                attachments=tuple(Path(path) for path in canonical_attachments),
            )
        except DesktopGatewayError:
            self.storage.connection.execute(
                "UPDATE dispatch_records SET state='outcome_unknown',updated_at=? "
                "WHERE dispatch_attempt_id=? AND state='bytes_sending'",
                (utc_now(), attempt_id),
            )
            self.storage.connection.execute(
                "UPDATE dispatch_attempts SET state='outcome_unknown',updated_at=? "
                "WHERE dispatch_attempt_id=?",
                (utc_now(), attempt_id),
            )
            return DispatchResult(attempt_id, None, "outcome_unknown")

        recorded_message = self._wait_for_new_user_turn(
            snapshots,
            thread_id=thread_id,
            expected_text=text,
            has_attachments=bool(canonical_attachments),
        )
        if recorded_message is None:
            self.storage.connection.execute(
                "UPDATE dispatch_records SET state='outcome_unknown',updated_at=? "
                "WHERE dispatch_attempt_id=? AND state='bytes_sending'",
                (utc_now(), attempt_id),
            )
            self.storage.connection.execute(
                "UPDATE dispatch_attempts SET state='outcome_unknown',updated_at=? "
                "WHERE dispatch_attempt_id=?",
                (utc_now(), attempt_id),
            )
            return DispatchResult(attempt_id, None, "outcome_unknown")

        turn_id = recorded_message.turn_id
        response = {
            "threadId": thread_id,
            "turnId": turn_id,
            "userItemId": recorded_message.item_id,
            "submitted": gateway_result.submitted,
            "usedForegroundFallback": gateway_result.used_foreground_fallback,
        }
        with self.storage.immediate() as connection:
            accepted = connection.execute(
                "UPDATE dispatch_records SET state='accepted',turn_id=?,user_item_id=?,updated_at=? "
                "WHERE dispatch_attempt_id=? AND state='bytes_sending'",
                (turn_id, recorded_message.item_id, utc_now(), attempt_id),
            )
            if accepted.rowcount != 1:
                raise DispatchError("desktop acceptance lost compare-and-swap")
            connection.execute(
                "UPDATE dispatch_attempts SET state='accepted',response_hash=?,updated_at=? "
                "WHERE dispatch_attempt_id=? AND state='dispatching'",
                (sha256(canonicalize(response)).hexdigest(), utc_now(), attempt_id),
            )
            connection.execute(
                "INSERT INTO executed_command_tombstones(tombstone_key,content_hash,target_thread_id,"
                "dispatch_attempt_id,retain_until) VALUES(?,?,?,?,datetime('now','+365 days'))",
                (ingress_message_id, request_hash, thread_id, attempt_id),
            )
        return DispatchResult(attempt_id, turn_id, "accepted")
