"""Fenced Codex turn dispatch, steer, interrupt, and reconciliation."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from ..models import DispatchState, OwnershipState
from ..runtime_storage import RuntimeStorage, utc_now
from ..security.jcs import canonicalize
from .app_server_client import ProtocolError
from .connection import AppServerConnection
from .execution_profile import ExecutionProfile
from .managed_requirements import ManagedRequirements


class DispatchError(RuntimeError):
    pass


class DispatchBusy(DispatchError):
    pass


@dataclass(frozen=True, slots=True)
class DispatchResult:
    dispatch_attempt_id: str
    turn_id: str | None
    state: str


class CodexController:
    def __init__(
        self,
        storage: RuntimeStorage,
        connection: AppServerConnection,
        *,
        schema_root: Path,
        server_epoch: str,
        connection_epoch: str,
    ) -> None:
        self.storage = storage
        self.connection = connection
        self.schema_root = schema_root.resolve()
        self.server_epoch = server_epoch
        self.connection_epoch = connection_epoch

    def read_thread(self, thread_id: str, *, include_turns: bool = False) -> dict[str, Any]:
        return self.connection.request(
            "thread/read", {"threadId": thread_id, "includeTurns": include_turns}
        )

    def _require_dispatchable(
        self, thread_id: str, *, required_capability: str
    ) -> tuple[int, int, int]:
        task = self.storage.connection.execute(
            "SELECT current_binding_epoch,identity_binding_epoch,opted_in,anchor_state,project_root,chat_id FROM task_bindings "
            "WHERE thread_id=?",
            (thread_id,),
        ).fetchone()
        if task is None or not task["opted_in"] or task["anchor_state"] != "confirmed":
            raise DispatchError("task is not opted in with a confirmed anchor")
        identity = self.storage.connection.execute(
            "SELECT binding_epoch,state FROM identity_bindings WHERE binding_key='owner'"
        ).fetchone()
        if identity is None or identity["state"] != "active":
            raise DispatchError("identity binding is inactive")
        ownership = self.storage.connection.execute(
            "SELECT ownership_state FROM thread_bindings WHERE thread_id=?", (thread_id,)
        ).fetchone()
        bridge_owned = (
            ownership is not None
            and ownership["ownership_state"] == OwnershipState.BRIDGE_OWNED.value
        )
        grant = self.storage.connection.execute(
            "SELECT * FROM remote_task_grants WHERE thread_id=? AND state='active'",
            (thread_id,),
        ).fetchone()
        service = self.storage.connection.execute(
            "SELECT fencing_token,process_state FROM service_state WHERE singleton=1"
        ).fetchone()
        if service is None or service["process_state"] != "running":
            raise DispatchError("service is not in running state")
        if not bridge_owned:
            if grant is None:
                raise DispatchError("thread has no active remote-input grant")
            try:
                capabilities = json.loads(grant["capabilities_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise DispatchError("remote-input grant is corrupt") from exc
            encoded_capabilities = canonicalize(capabilities)
            if (
                capabilities.get(required_capability) is not True
                or sha256(encoded_capabilities).hexdigest() != grant["capabilities_hash"]
                or grant["project_root"] != task["project_root"]
                or grant["chat_id"] != task["chat_id"]
                or int(grant["task_binding_epoch"]) != int(task["current_binding_epoch"])
                or int(grant["identity_binding_epoch"]) != int(identity["binding_epoch"])
                or int(grant["service_fencing_token"]) != int(service["fencing_token"])
            ):
                raise DispatchError("remote-input grant no longer matches the task binding")
        return int(task["current_binding_epoch"]), int(identity["binding_epoch"]), int(service["fencing_token"])

    def _managed_requirements(self, profile: ExecutionProfile) -> ManagedRequirements:
        result = self.connection.request("configRequirements/read", None)
        requirements = ManagedRequirements.parse(
            result,
            self.schema_root / "v2" / "ConfigRequirementsReadResponse.json",
        )
        requirements.enforce(profile, remote_dispatch=True)
        return requirements

    @staticmethod
    def _authoritative_active_turn(thread: dict[str, Any]) -> str:
        """Return the single App Server turn that is actually in progress.

        Rollout files are an output stream, not the authority for current
        runtime state.  A crash or interrupted writer can leave an unmatched
        ``task_started`` record behind.  ``thread/resume`` is schema-validated
        and includes turn statuses, so use it to fence ``turn/steer``.
        """

        turns = thread.get("turns")
        if not isinstance(turns, list):
            raise DispatchError("thread/resume turns are malformed")
        active: list[str] = []
        for turn in turns:
            if not isinstance(turn, dict) or turn.get("status") != "inProgress":
                continue
            turn_id = turn.get("id")
            if not isinstance(turn_id, str) or not turn_id:
                raise DispatchError("in-progress turn lacks an id")
            active.append(turn_id)
        if len(active) != 1:
            raise DispatchBusy("App Server did not report exactly one active turn")
        return active[0]

    def dispatch(
        self,
        *,
        ingress_message_id: str,
        thread_id: str,
        text: str,
        profile: ExecutionProfile,
        profile_hash: str,
        input_items: tuple[dict[str, Any], ...] | None = None,
        required_capability: str = "text",
        active_turn_id: str | None = None,
    ) -> DispatchResult:
        binding_epoch, identity_epoch, fencing_token = self._require_dispatchable(
            thread_id, required_capability=required_capability
        )
        thread_result = self.connection.request("thread/resume", {"threadId": thread_id})
        thread = thread_result.get("thread")
        if not isinstance(thread, dict):
            raise DispatchError("thread/read result is malformed")
        status = thread.get("status")
        if not isinstance(status, dict):
            raise DispatchError("thread/read status is malformed")
        requirements = self._managed_requirements(profile)
        attempt_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"codex-feishu:{ingress_message_id}"))
        client_message_id = str(uuid.uuid5(uuid.NAMESPACE_OID, f"codex-feishu:{ingress_message_id}"))
        if status.get("type") == "idle":
            if thread.get("canAcceptDirectInput") is not True:
                raise DispatchError("pinned experimental thread schema does not permit direct input")
            method = "turn/start"
            params = profile.turn_start_params(
                thread_id=thread_id,
                text=text,
                client_user_message_id=client_message_id,
                input_items=input_items,
            )
        else:
            # The rollout-derived value is only a hint.  App Server owns the
            # live turn state and can safely supersede stale unmatched rollout
            # task_started records.
            active_turn_id = self._authoritative_active_turn(thread)
            method = "turn/steer"
            params = {
                "threadId": thread_id,
                "expectedTurnId": active_turn_id,
                "input": list(input_items or ({"type": "text", "text": text},)),
                "clientUserMessageId": client_message_id,
            }
        request_hash = sha256(canonicalize({"method": method, "params": params})).hexdigest()
        now = utc_now()
        with self.storage.immediate() as connection:
            existing = connection.execute(
                "SELECT request_hash,state,turn_id FROM dispatch_records WHERE dispatch_attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise DispatchError("dispatch identity conflicts with a different request")
                return DispatchResult(attempt_id, existing["turn_id"], existing["state"])
            connection.execute(
                "INSERT INTO dispatch_attempts(dispatch_attempt_id,state,updated_at) VALUES(?,'dispatching',?)",
                (attempt_id, now),
            )
            connection.execute(
                "INSERT INTO dispatch_records(dispatch_attempt_id,ingress_message_id,thread_id,"
                "client_user_message_id,profile_hash,binding_epoch,identity_binding_epoch,fencing_token,"
                "server_epoch,connection_epoch,request_hash,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,'prepared',?,?)",
                (
                    attempt_id,
                    ingress_message_id,
                    thread_id,
                    client_message_id,
                    profile_hash,
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

        def before_send(message: dict[str, Any]) -> None:
            updated = self.storage.connection.execute(
                "UPDATE dispatch_records SET state='bytes_sending',request_id=?,updated_at=? "
                "WHERE dispatch_attempt_id=? AND state='prepared' AND fencing_token=?",
                (str(message["id"]), utc_now(), attempt_id, fencing_token),
            )
            if updated.rowcount != 1:
                raise DispatchError("dispatch send fence lost compare-and-swap")

        try:
            result = self.connection.request(
                method, params, timeout=30, before_send=before_send
            )
        except Exception:
            self.storage.connection.execute(
                "UPDATE dispatch_records SET state='outcome_unknown',updated_at=? "
                "WHERE dispatch_attempt_id=? AND state='bytes_sending'",
                (utc_now(), attempt_id),
            )
            self.storage.connection.execute(
                "UPDATE dispatch_attempts SET state='outcome_unknown',updated_at=? WHERE dispatch_attempt_id=?",
                (utc_now(), attempt_id),
            )
            return DispatchResult(attempt_id, None, "outcome_unknown")
        if method == "turn/start":
            turn = result.get("turn") if isinstance(result, dict) else None
            turn_id = turn.get("id") if isinstance(turn, dict) else None
        else:
            turn_id = result.get("turnId") if isinstance(result, dict) else None
        if not isinstance(turn_id, str) or not turn_id:
            raise DispatchError(f"{method} response lacks a turn id")
        if active_turn_id is not None and method == "turn/steer" and turn_id != active_turn_id:
            raise DispatchError("turn/steer response does not match the expected turn")
        with self.storage.immediate() as connection:
            updated = connection.execute(
                "UPDATE dispatch_records SET state='accepted',turn_id=?,updated_at=? "
                "WHERE dispatch_attempt_id=? AND state='bytes_sending'",
                (turn_id, utc_now(), attempt_id),
            )
            if updated.rowcount != 1:
                raise DispatchError("dispatch acceptance lost compare-and-swap")
            connection.execute(
                "UPDATE dispatch_attempts SET state='accepted',response_hash=?,updated_at=? "
                "WHERE dispatch_attempt_id=? AND state='dispatching'",
                (sha256(canonicalize(result)).hexdigest(), utc_now(), attempt_id),
            )
            connection.execute(
                "INSERT INTO executed_command_tombstones(tombstone_key,content_hash,target_thread_id,"
                "dispatch_attempt_id,retain_until) VALUES(?,?,?,?,datetime('now','+365 days'))",
                (ingress_message_id, request_hash, thread_id, attempt_id),
            )
        return DispatchResult(attempt_id, turn_id, "accepted")

    def reconcile_unknown(self, dispatch_attempt_id: str) -> DispatchResult:
        record = self.storage.connection.execute(
            "SELECT * FROM dispatch_records WHERE dispatch_attempt_id=? AND state='outcome_unknown'",
            (dispatch_attempt_id,),
        ).fetchone()
        if record is None:
            raise DispatchError("dispatch is not outcome_unknown")
        history = self.read_thread(record["thread_id"], include_turns=True)
        matches = _find_turns_with_client_id(history, record["client_user_message_id"])
        if len(matches) != 1:
            return DispatchResult(dispatch_attempt_id, None, "outcome_unknown")
        updated = self.storage.connection.execute(
            "UPDATE dispatch_records SET state='accepted',turn_id=?,updated_at=? "
            "WHERE dispatch_attempt_id=? AND state='outcome_unknown'",
            (matches[0], utc_now(), dispatch_attempt_id),
        )
        if updated.rowcount != 1:
            raise DispatchError("unknown dispatch reconciliation lost compare-and-swap")
        return DispatchResult(dispatch_attempt_id, matches[0], "accepted")

    def append(self, thread_id: str, expected_turn_id: str, text: str, client_id: str) -> str:
        self._require_dispatchable(thread_id, required_capability="controls")
        result = self.connection.request(
            "turn/steer",
            {
                "threadId": thread_id,
                "expectedTurnId": expected_turn_id,
                "input": [{"type": "text", "text": text}],
                "clientUserMessageId": client_id,
            },
        )
        if result.get("turnId") != expected_turn_id:
            raise DispatchError("turn/steer response does not match the expected turn")
        return expected_turn_id

    def stop(self, thread_id: str, expected_turn_id: str) -> None:
        self._require_dispatchable(thread_id, required_capability="controls")
        self.connection.request(
            "turn/interrupt", {"threadId": thread_id, "turnId": expected_turn_id}
        )

    def require_control_authorized(self, thread_id: str) -> None:
        self._require_dispatchable(thread_id, required_capability="controls")

    def reconcile_profile(self, thread_id: str, profile: ExecutionProfile) -> bool:
        """Reconcile only sticky fields represented by ``thread/resume``.

        ``sandbox`` in ThreadResumeResponse is the thread's default sandbox,
        not the exact per-turn ``sandboxPolicy`` supplied to ``turn/start``.
        Comparing it with the dispatch profile creates a false mismatch after
        an otherwise schema-valid turn.  Per-turn sandbox identity remains
        bound by dispatch_records.request_hash/profile_hash and the pinned
        turn/start schema; this readback checks only the fields that resume can
        truthfully apply and return.
        """
        result = self.connection.request(
            "thread/resume",
            {
                "threadId": thread_id,
                "cwd": str(profile.cwd),
                "approvalPolicy": profile.approval_policy.value,
                "approvalsReviewer": profile.approvals_reviewer,
            },
        )
        expected = {
            "cwd": str(profile.cwd),
            "approvalPolicy": profile.approval_policy.value,
            "approvalsReviewer": profile.approvals_reviewer,
        }
        effective = {
            "cwd": result.get("cwd"),
            "approvalPolicy": result.get("approvalPolicy"),
            "approvalsReviewer": result.get("approvalsReviewer"),
        }
        expected_hash = sha256(canonicalize(expected)).hexdigest()
        effective_hash = sha256(canonicalize(effective)).hexdigest()
        matched = expected_hash == effective_hash
        self.storage.connection.execute(
            "UPDATE thread_profiles SET displayed_hash=?,effective_hash=?,reconciliation_state=?,updated_at=? "
            "WHERE thread_id=?",
            (
                expected_hash,
                effective_hash,
                "matched" if matched else "mismatch",
                utc_now(),
                thread_id,
            ),
        )
        if not matched:
            self.storage.connection.execute(
                "INSERT INTO circuit_breakers(breaker_name,state,reason,updated_at) "
                "VALUES('profile_reconciliation','open',?,?) ON CONFLICT(breaker_name) DO UPDATE SET "
                "state='open',reason=excluded.reason,updated_at=excluded.updated_at",
                ("thread:" + thread_id, utc_now()),
            )
        else:
            self.storage.connection.execute(
                "UPDATE circuit_breakers SET state='closed',reason=NULL,updated_at=? "
                "WHERE breaker_name='profile_reconciliation' AND reason=?",
                (utc_now(), "thread:" + thread_id),
            )
        return matched


def _find_turns_with_client_id(value: Any, client_id: str) -> list[str]:
    matches: set[str] = set()

    def walk(node: Any, current_turn: str | None = None) -> None:
        if isinstance(node, dict):
            turn = current_turn
            if isinstance(node.get("id"), str) and "items" in node and "status" in node:
                turn = node["id"]
            if node.get("clientUserMessageId") == client_id and turn:
                matches.add(turn)
            for child in node.values():
                walk(child, turn)
        elif isinstance(node, list):
            for child in node:
                walk(child, current_turn)

    walk(value)
    return sorted(matches)
