"""Fenced Feishu dispatch through the persisted Codex CLI writer."""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from ..runtime_storage import RuntimeStorage, utc_now
from ..security.jcs import canonicalize
from .cli_gateway import CodexCliActiveWriter, CodexCliGateway, CodexCliGatewayError
from .controller import DispatchBusy, DispatchError, DispatchResult
from .desktop_dispatch import desktop_submission_text_hash


@dataclass(frozen=True, slots=True)
class _RecordedCliUserMessage:
    turn_id: str


_LEADING_IMAGE = re.compile(
    r'\A(?:\s*<image\b[^>]*\bpath="[^"]+"[^>]*>\s*</image>\s*)+',
    re.IGNORECASE,
)


def matches_cli_submission(
    actual_text: str | None,
    expected_hash: str,
    *,
    has_images: bool,
) -> bool:
    if actual_text is None:
        return False
    if desktop_submission_text_hash(actual_text) == expected_hash:
        return True
    if not has_images:
        return False
    without_images = _LEADING_IMAGE.sub("", actual_text)
    return without_images != actual_text and (
        desktop_submission_text_hash(without_images) == expected_hash
    )


class CodexCliDispatcher:
    """Submit with the CLI, then prove the exact new turn from rollout bytes."""

    def __init__(
        self,
        storage: RuntimeStorage,
        gateway: CodexCliGateway,
        *,
        codex_home: Path,
        authorize: Callable[..., tuple[int, int, int]],
        server_epoch: str,
        connection_epoch: str,
        rollout_confirmation_seconds: float = 30.0,
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
    def _turn_id(payload: dict[str, Any]) -> str | None:
        direct = payload.get("turn_id")
        if isinstance(direct, str) and direct:
            return direct
        metadata = payload.get("internal_chat_message_metadata_passthrough")
        nested = metadata.get("turn_id") if isinstance(metadata, dict) else None
        return nested if isinstance(nested, str) and nested else None

    @staticmethod
    def _message_text(payload: dict[str, Any]) -> str | None:
        if payload.get("type") != "message" or payload.get("role") != "user":
            return None
        content = payload.get("content")
        if not isinstance(content, list):
            return None
        return "".join(
            item.get("text")
            for item in content
            if isinstance(item, dict)
            and item.get("type") == "input_text"
            and isinstance(item.get("text"), str)
        )

    def _find_new_user_turn(
        self,
        snapshots: dict[Path, int],
        *,
        thread_id: str,
        expected_hash: str,
        has_images: bool,
    ) -> _RecordedCliUserMessage | None:
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
            started_turns: set[str] = set()
            contextualized_turns: set[str] = set()
            for line in raw.splitlines():
                try:
                    record = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                if record.get("type") == "event_msg" and payload.get("type") == "task_started":
                    turn_id = self._turn_id(payload)
                    if turn_id is not None:
                        started_turns.add(turn_id)
                    continue
                if record.get("type") == "turn_context":
                    turn_id = self._turn_id(payload)
                    if turn_id in started_turns:
                        contextualized_turns.add(turn_id)
                    continue
                if record.get("type") != "response_item":
                    continue
                turn_id = self._turn_id(payload)
                if turn_id not in contextualized_turns:
                    continue
                if matches_cli_submission(
                    self._message_text(payload),
                    expected_hash,
                    has_images=has_images,
                ):
                    return _RecordedCliUserMessage(turn_id)
        return None

    def _wait_for_new_user_turn(
        self,
        snapshots: dict[Path, int],
        *,
        thread_id: str,
        expected_hash: str,
        has_images: bool,
    ) -> _RecordedCliUserMessage | None:
        deadline = time.monotonic() + self.rollout_confirmation_seconds
        while time.monotonic() < deadline:
            match = self._find_new_user_turn(
                snapshots,
                thread_id=thread_id,
                expected_hash=expected_hash,
                has_images=has_images,
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
        image_paths: Sequence[Path] = (),
    ) -> DispatchResult:
        binding_epoch, identity_epoch, fencing_token = self.authorize(
            thread_id,
            required_capability=required_capability,
        )
        task = self.storage.connection.execute(
            "SELECT project_root FROM task_bindings WHERE thread_id=? AND opted_in=1",
            (thread_id,),
        ).fetchone()
        if task is None:
            raise DispatchError("CLI dispatch target task disappeared")
        project_root = Path(str(task["project_root"])).resolve(strict=True)
        canonical_images = tuple(
            str(Path(path).resolve(strict=True)) for path in image_paths
        )
        request = {
            "method": "cli/exec-resume",
            "params": {
                "threadId": thread_id,
                "text": text,
                "images": canonical_images,
                "cwd": str(project_root),
            },
        }
        request_hash = sha256(canonicalize(request)).hexdigest()
        text_hash = desktop_submission_text_hash(text)
        attempt_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"codex-feishu:{ingress_message_id}"))
        client_message_id = str(
            uuid.uuid5(uuid.NAMESPACE_OID, f"codex-feishu-cli:{ingress_message_id}")
        )
        now = utc_now()
        with self.storage.immediate() as connection:
            existing = connection.execute(
                "SELECT request_hash,state,turn_id,request_id FROM dispatch_records "
                "WHERE dispatch_attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise DispatchError("CLI dispatch identity conflicts with another request")
                state = str(existing["state"])
                if state == "outcome_unknown" and existing["request_id"] == "codex-cli-started":
                    state = "submitted_unconfirmed"
                return DispatchResult(attempt_id, existing["turn_id"], state)
            connection.execute(
                "INSERT INTO dispatch_attempts(dispatch_attempt_id,state,updated_at) "
                "VALUES(?,'dispatching',?)",
                (attempt_id, now),
            )
            connection.execute(
                "INSERT INTO dispatch_records(dispatch_attempt_id,ingress_message_id,thread_id,"
                "client_user_message_id,profile_hash,binding_epoch,identity_binding_epoch,fencing_token,"
                "server_epoch,connection_epoch,request_hash,submitted_text_hash,has_attachments,"
                "state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'prepared',?,?)",
                (
                    attempt_id,
                    ingress_message_id,
                    thread_id,
                    client_message_id,
                    "cli-host-managed",
                    binding_epoch,
                    identity_epoch,
                    fencing_token,
                    self.server_epoch,
                    self.connection_epoch,
                    request_hash,
                    text_hash,
                    int(bool(canonical_images)),
                    now,
                    now,
                ),
            )

        snapshots = self._rollout_snapshots(thread_id)
        updated = self.storage.connection.execute(
            "UPDATE dispatch_records SET state='bytes_sending',request_id='codex-cli',updated_at=? "
            "WHERE dispatch_attempt_id=? AND state='prepared' AND fencing_token=?",
            (utc_now(), attempt_id, fencing_token),
        )
        if updated.rowcount != 1:
            raise DispatchError("CLI dispatch lost its service fence")
        try:
            self.gateway.submit(
                thread_id,
                text,
                image_paths=tuple(Path(path) for path in canonical_images),
                cwd=project_root,
            )
        except CodexCliActiveWriter as exc:
            # The CLI rejected the second writer before emitting thread.started,
            # so no Codex turn exists. Remove only this prepared attempt and let
            # the service use the desktop-owned writer with the same ingress id.
            with self.storage.immediate() as connection:
                removed = connection.execute(
                    "DELETE FROM dispatch_records WHERE dispatch_attempt_id=? "
                    "AND state='bytes_sending' AND request_id='codex-cli'",
                    (attempt_id,),
                )
                if removed.rowcount != 1:
                    raise DispatchError(
                        "CLI active-writer rollback lost compare-and-swap"
                    ) from exc
                connection.execute(
                    "DELETE FROM dispatch_attempts WHERE dispatch_attempt_id=? "
                    "AND state='dispatching'",
                    (attempt_id,),
                )
            raise DispatchBusy("Codex task already has an active writer") from exc
        except CodexCliGatewayError:
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

        submitted = self.storage.connection.execute(
            "UPDATE dispatch_records SET request_id='codex-cli-started',updated_at=? "
            "WHERE dispatch_attempt_id=? AND state='bytes_sending' AND request_id='codex-cli'",
            (utc_now(), attempt_id),
        )
        if submitted.rowcount != 1:
            raise DispatchError("CLI start acknowledgement lost compare-and-swap")
        recorded = self._wait_for_new_user_turn(
            snapshots,
            thread_id=thread_id,
            expected_hash=text_hash,
            has_images=bool(canonical_images),
        )
        if recorded is None:
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
            return DispatchResult(attempt_id, None, "submitted_unconfirmed")

        response = {"threadId": thread_id, "turnId": recorded.turn_id, "writer": "codex-cli"}
        with self.storage.immediate() as connection:
            accepted = connection.execute(
                "UPDATE dispatch_records SET state='accepted',turn_id=?,updated_at=? "
                "WHERE dispatch_attempt_id=? AND state='bytes_sending'",
                (recorded.turn_id, utc_now(), attempt_id),
            )
            if accepted.rowcount != 1:
                raise DispatchError("CLI acceptance lost compare-and-swap")
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
        return DispatchResult(attempt_id, recorded.turn_id, "accepted")
