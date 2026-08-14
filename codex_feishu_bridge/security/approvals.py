"""Single-use, context-bound approval decisions."""

from __future__ import annotations

import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

from ..models import ApprovalState
from ..runtime_storage import RuntimeStorage, utc_now
from .jcs import canonicalize, digest
from .audit import AuditChain


class ApprovalError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ApprovalContext:
    approval_id: str
    server_request_id: str
    server_method: str
    thread_id: str
    turn_id: str | None
    tenant_key: str
    app_id: str
    chat_id: str
    card_message_id: str
    operator_open_id: str
    binding_epoch: int
    identity_binding_epoch: int
    kill_generation: int
    server_epoch: str
    connection_epoch: str
    session_id: str
    service_fencing_token: int


@dataclass(frozen=True, slots=True)
class IssuedApproval:
    opaque_token: str | None
    request_hash: str
    expires_at: str
    created: bool = True


def exact_response_hash(response: dict[str, Any]) -> str:
    return sha256(canonicalize(response)).hexdigest()


class ApprovalBroker:
    def __init__(
        self,
        storage: RuntimeStorage,
        token_key: bytes,
        audit: AuditChain | None = None,
    ) -> None:
        if len(token_key) < 32:
            raise ApprovalError("approval token key must contain at least 256 bits")
        self.storage = storage
        self.token_key = token_key
        self.audit = audit

    def issue(
        self,
        *,
        context: ApprovalContext,
        request: dict[str, Any],
        decision_map: dict[str, dict[str, Any]],
        ttl_seconds: int = 300,
    ) -> IssuedApproval:
        if ttl_seconds <= 0 or not decision_map:
            raise ApprovalError("approval TTL and decision map must be non-empty")
        token = secrets.token_urlsafe(32)
        token_hash = hmac.new(self.token_key, token.encode("ascii"), sha256).hexdigest()
        request_hash = digest(request)
        expires_at = (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat().replace(
            "+00:00", "Z"
        )
        with self.storage.immediate() as connection:
            existing = connection.execute(
                "SELECT a.*,r.request_hash FROM approval_actions a "
                "JOIN approval_requests r ON r.approval_id=a.approval_id "
                "WHERE a.server_epoch=? AND a.connection_epoch=? AND a.server_request_id=?",
                (
                    context.server_epoch,
                    context.connection_epoch,
                    context.server_request_id,
                ),
            ).fetchone()
            if existing is not None:
                exact_identity = (
                    existing["approval_id"] == context.approval_id
                    and existing["request_hash"] == request_hash
                    and existing["server_method"] == context.server_method
                    and existing["thread_id"] == context.thread_id
                    and existing["turn_id"] == context.turn_id
                    and existing["session_id"] == context.session_id
                )
                if not exact_identity:
                    raise ApprovalError(
                        "approval request identity was reused with different content"
                    )
                return IssuedApproval(
                    None,
                    request_hash,
                    str(existing["expires_at"]),
                    created=False,
                )
            connection.execute(
                "INSERT INTO approval_requests(approval_id,state,request_hash,updated_at) VALUES(?,?,?,?)",
                (context.approval_id, ApprovalState.ISSUED.value, request_hash, utc_now()),
            )
            connection.execute(
                "INSERT INTO approval_actions(token_hash,approval_id,server_request_id,server_method,"
                "thread_id,turn_id,tenant_key,app_id,chat_id,"
                "card_message_id,operator_open_id,binding_epoch,identity_binding_epoch,kill_generation,"
                "server_epoch,connection_epoch,session_id,service_fencing_token,decision_map_json,expires_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    token_hash,
                    context.approval_id,
                    context.server_request_id,
                    context.server_method,
                    context.thread_id,
                    context.turn_id,
                    context.tenant_key,
                    context.app_id,
                    context.chat_id,
                    context.card_message_id,
                    context.operator_open_id,
                    context.binding_epoch,
                    context.identity_binding_epoch,
                    context.kill_generation,
                    context.server_epoch,
                    context.connection_epoch,
                    context.session_id,
                    context.service_fencing_token,
                    canonicalize(decision_map).decode("utf-8"),
                    expires_at,
                ),
            )
        if self.audit is not None:
            self.audit.append(
                {
                    "event": "approval_issued",
                    "approval_id": context.approval_id,
                    "request_hash": request_hash,
                    "server_method": context.server_method,
                    "thread_id": context.thread_id,
                }
            )
        return IssuedApproval(token, request_hash, expires_at)

    def commit_card_action(
        self,
        *,
        opaque_token: str,
        decision: str,
        tenant_key: str,
        app_id: str,
        chat_id: str,
        card_message_id: str,
        operator_open_id: str,
        binding_epoch: int,
        identity_binding_epoch: int,
        kill_generation: int,
        server_epoch: str,
        connection_epoch: str,
        session_id: str,
        service_fencing_token: int,
    ) -> tuple[str, dict[str, Any]]:
        token_hash = hmac.new(
            self.token_key, opaque_token.encode("ascii", errors="strict"), sha256
        ).hexdigest()
        now = datetime.now(UTC)
        with self.storage.immediate() as connection:
            row = connection.execute(
                "SELECT * FROM approval_actions WHERE token_hash=?", (token_hash,)
            ).fetchone()
            if row is None or row["consumed_at"] is not None:
                raise ApprovalError("approval token is invalid or already consumed")
            expires = datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00"))
            if expires <= now:
                connection.execute(
                    "UPDATE approval_requests SET state='expired',updated_at=? "
                    "WHERE approval_id=? AND state='issued'",
                    (utc_now(), row["approval_id"]),
                )
                raise ApprovalError("approval token expired")
            expected = (
                tenant_key,
                app_id,
                chat_id,
                card_message_id,
                operator_open_id,
                binding_epoch,
                identity_binding_epoch,
                kill_generation,
                server_epoch,
                connection_epoch,
                session_id,
                service_fencing_token,
            )
            actual = tuple(
                row[name]
                for name in (
                    "tenant_key",
                    "app_id",
                    "chat_id",
                    "card_message_id",
                    "operator_open_id",
                    "binding_epoch",
                    "identity_binding_epoch",
                    "kill_generation",
                    "server_epoch",
                    "connection_epoch",
                    "session_id",
                    "service_fencing_token",
                )
            )
            if actual != expected:
                raise ApprovalError("approval context binding mismatch")
            decision_map = json.loads(row["decision_map_json"])
            response = decision_map.get(decision)
            if not isinstance(response, dict):
                raise ApprovalError("decision is not offered by the pinned approval schema")
            updated = connection.execute(
                "UPDATE approval_actions SET consumed_at=?,consumed_decision=? "
                "WHERE token_hash=? AND consumed_at IS NULL",
                (utc_now(), decision, token_hash),
            )
            if updated.rowcount != 1:
                raise ApprovalError("approval token consumption lost compare-and-swap")
            transitioned = connection.execute(
                "UPDATE approval_requests SET state='action_committed',updated_at=? "
                "WHERE approval_id=? AND state='issued'",
                (utc_now(), row["approval_id"]),
            )
            if transitioned.rowcount != 1:
                raise ApprovalError("approval action state lost compare-and-swap")
            approval_id = str(row["approval_id"])
        if self.audit is not None:
            self.audit.append(
                {
                    "event": "approval_action_committed",
                    "approval_id": approval_id,
                    "decision": decision,
                }
            )
        return approval_id, response

    def commit_exact_response(self, approval_id: str, response: dict[str, Any]) -> str:
        response_hash = exact_response_hash(response)
        updated = self.storage.connection.execute(
            "UPDATE approval_requests SET state='response_sending',response_hash=?,updated_at=? "
            "WHERE approval_id=? AND state='action_committed' AND response_hash IS NULL",
            (response_hash, utc_now(), approval_id),
        )
        if updated.rowcount != 1:
            raise ApprovalError("exact approval response hash lost compare-and-swap")
        if self.audit is not None:
            self.audit.append(
                {
                    "event": "approval_response_prepared",
                    "approval_id": approval_id,
                    "response_hash": response_hash,
                }
            )
        return response_hash

    def record_response_result(self, approval_id: str, *, accepted: bool | None) -> None:
        target = "resolved" if accepted is True else "rejected" if accepted is False else "outcome_unknown"
        updated = self.storage.connection.execute(
            "UPDATE approval_requests SET state=?,updated_at=? "
            "WHERE approval_id=? AND state='response_sending' AND response_hash IS NOT NULL",
            (target, utc_now(), approval_id),
        )
        if updated.rowcount != 1:
            raise ApprovalError("approval response resolution lost compare-and-swap")
