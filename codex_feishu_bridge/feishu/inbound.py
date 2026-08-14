"""Fail-closed P2P or topic-group ingress validation and routing."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from hashlib import sha256
from typing import Any

from ..commands import ControlCommand, parse_command
from ..runtime_config import FeishuBinding, RemoteCapabilities
from ..runtime_storage import RuntimeStorage, utc_now
from ..security.jcs import canonicalize


class IngressRejected(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class IngressDecision:
    duplicate: bool
    routing_state: str
    target_thread_id: str | None
    ingest_seq: int | None
    command: ControlCommand | None


def _require_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise IngressRejected(f"missing {name}")
    return value


class IngressRouter:
    def __init__(
        self,
        storage: RuntimeStorage,
        binding: FeishuBinding,
        capabilities: RemoteCapabilities | None = None,
    ) -> None:
        self.storage = storage
        self.binding = binding
        self.capabilities = capabilities or RemoteCapabilities(
            enabled=True, text=True
        )

    def ingest(self, event: dict[str, Any]) -> IngressDecision:
        header = event.get("header")
        payload = event.get("event")
        if not isinstance(header, dict) or not isinstance(payload, dict):
            raise IngressRejected("invalid event envelope")
        tenant = _require_string(header.get("tenant_key"), "tenant_key")
        app_id = _require_string(header.get("app_id"), "app_id")
        event_id = _require_string(header.get("event_id"), "event_id")
        message = payload.get("message")
        sender = payload.get("sender")
        if not isinstance(message, dict) or not isinstance(sender, dict):
            raise IngressRejected("invalid message event")
        sender_id = sender.get("sender_id")
        if not isinstance(sender_id, dict):
            raise IngressRejected("invalid sender identity")
        open_id = _require_string(sender_id.get("open_id"), "sender.open_id")
        sender_type = _require_string(sender.get("sender_type"), "sender.sender_type")
        sender_tenant = _require_string(sender.get("tenant_key"), "sender.tenant_key")
        message_id = _require_string(message.get("message_id"), "message_id")
        chat_id = _require_string(message.get("chat_id"), "chat_id")
        chat_type = _require_string(message.get("chat_type"), "chat_type")
        message_type = _require_string(message.get("message_type"), "message_type")
        if (
            tenant != self.binding.tenant_key
            or app_id != self.binding.app_id
            or open_id != self.binding.owner_open_id
            or sender_type != "user"
            or sender_tenant != self.binding.tenant_key
            or chat_type != self.binding.target_chat_type
        ):
            raise IngressRejected("event identity/chat binding mismatch")
        content = message.get("content")
        if not isinstance(content, str):
            raise IngressRejected("message content must be a serialized string")
        content_hash = sha256(content.encode("utf-8")).hexdigest()
        text = ""
        resource_key: str | None = None
        resource_type: str | None = None
        original_file_name: str | None = None
        if message_type == "text":
            try:
                decoded_content = json.loads(content)
            except json.JSONDecodeError as exc:
                raise IngressRejected("text content is not valid provider JSON") from exc
            if not isinstance(decoded_content, dict) or not isinstance(decoded_content.get("text"), str):
                raise IngressRejected("text message lacks a text field")
            text = decoded_content["text"]
            command = parse_command(message_type, text)
            if command is not None and not self.capabilities.controls:
                raise IngressRejected("remote control commands are disabled")
            if command is None and not self.capabilities.text:
                raise IngressRejected("remote text input is disabled")
        elif message_type in {"image", "file"}:
            enabled = self.capabilities.images if message_type == "image" else self.capabilities.files
            if not enabled:
                raise IngressRejected(f"remote {message_type} input is disabled")
            try:
                decoded_content = json.loads(content)
            except json.JSONDecodeError as exc:
                raise IngressRejected("attachment content is not valid provider JSON") from exc
            if not isinstance(decoded_content, dict):
                raise IngressRejected("attachment content must be an object")
            key_field = "image_key" if message_type == "image" else "file_key"
            resource_key = decoded_content.get(key_field)
            if (
                not isinstance(resource_key, str)
                or not re.fullmatch(r"[A-Za-z0-9._-]{1,512}", resource_key)
            ):
                raise IngressRejected("attachment resource key is invalid")
            resource_type = message_type
            if message_type == "file":
                candidate_name = decoded_content.get("file_name")
                if not isinstance(candidate_name, str) or not candidate_name.strip():
                    raise IngressRejected("file message lacks a file name")
                if len(candidate_name) > 255 or any(
                    ord(character) < 32 for character in candidate_name
                ):
                    raise IngressRejected("file name is invalid")
                original_file_name = candidate_name
        else:
            raise IngressRejected("unsupported inbound message type")
        raw_hash = sha256(canonicalize(event)).hexdigest()
        root_id = message.get("root_id") or None
        parent_id = message.get("parent_id") or None
        provider_thread_id = message.get("thread_id") or None
        if provider_thread_id is not None and not isinstance(provider_thread_id, str):
            raise IngressRejected("thread_id must be a string when present")
        with self.storage.immediate() as connection:
            if not self._chat_is_authorized(connection, chat_id):
                raise IngressRejected("event chat is not an active Codex project group")
            existing = connection.execute(
                "SELECT chat_id,content_hash FROM ingress_messages WHERE tenant_key=? AND app_id=? AND message_id=?",
                (tenant, app_id, message_id),
            ).fetchone()
            if existing is not None:
                if existing["chat_id"] != chat_id or existing["content_hash"] != content_hash:
                    self._open_conflict(connection, message_id)
                    connection.execute("COMMIT")
                    raise IngressRejected("message identity has conflicting chat/content")
                return IngressDecision(True, "duplicate", None, None, None)
            sequence = connection.execute(
                "SELECT * FROM chat_sequences WHERE binding_key='owner'"
            ).fetchone()
            if sequence is None:
                connection.execute(
                    "INSERT INTO chat_sequences(binding_key,next_ingest_seq,active_binding_epoch,"
                    "selection_state,updated_at) VALUES('owner',1,0,'active',?)",
                    (utc_now(),),
                )
                sequence = connection.execute(
                    "SELECT * FROM chat_sequences WHERE binding_key='owner'"
                ).fetchone()
            ingest_seq = int(sequence["next_ingest_seq"])
            command = parse_command(message_type, text)
            if command is not None:
                target, routed_state = self._route(
                    connection, chat_id, root_id, parent_id, provider_thread_id, sequence
                )
                if self.binding.conversation_mode.value == "p2p" and (
                    sequence["selection_state"] == "active"
                    or command.name in {"hard-stop", "status"}
                ):
                    target, routing_state = sequence["current_task_id"], "control"
                elif target is not None:
                    routing_state = "control"
                else:
                    routing_state = routed_state
            else:
                target, routing_state = self._route(
                    connection, chat_id, root_id, parent_id, provider_thread_id, sequence
                )
            connection.execute(
                "INSERT INTO ingress_messages(tenant_key,app_id,message_id,event_id,chat_id,sender_open_id,"
                "chat_type,root_id,parent_id,provider_thread_id,message_type,content_hash,raw_hash,received_at,ingest_seq,"
                "routing_state,target_thread_id,binding_epoch,identity_binding_epoch) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    tenant,
                    app_id,
                    message_id,
                    event_id,
                    chat_id,
                    open_id,
                    chat_type,
                    root_id,
                    parent_id,
                    provider_thread_id,
                    message_type,
                    content_hash,
                    raw_hash,
                    utc_now(),
                    ingest_seq,
                    routing_state,
                    target,
                    sequence["active_binding_epoch"],
                    self._identity_epoch(connection),
                ),
            )
            connection.execute(
                "INSERT INTO ingress_payloads(message_id,text,created_at,expires_at) "
                "VALUES(?,?,?,datetime('now','+1 day'))",
                (message_id, text, utc_now()),
            )
            if resource_key is not None and resource_type is not None:
                connection.execute(
                    "INSERT INTO ingress_attachments(message_id,resource_key,resource_type,"
                    "original_file_name,state,next_attempt_at,created_at,updated_at) "
                    "VALUES(?,?,?,?,'pending',?,?,?)",
                    (
                        message_id,
                        resource_key,
                        resource_type,
                        original_file_name,
                        utc_now(),
                        utc_now(),
                        utc_now(),
                    ),
                )
            if (
                target is not None
                and root_id is not None
                and parent_id is not None
                and routing_state in {"routed_reply", "control"}
            ):
                connection.execute(
                    "INSERT INTO message_ancestry(message_id,root_id,parent_id,thread_id,chat_id,"
                    "provider_thread_id,source,created_at) VALUES(?,?,?,?,?,?,'inbound',?)",
                    (
                        message_id,
                        root_id,
                        parent_id,
                        target,
                        chat_id,
                        provider_thread_id,
                        utc_now(),
                    ),
                )
            connection.execute(
                "UPDATE chat_sequences SET next_ingest_seq=next_ingest_seq+1,updated_at=? WHERE binding_key='owner'",
                (utc_now(),),
            )
        return IngressDecision(False, routing_state, target, ingest_seq, command)

    def _route(
        self,
        connection: Any,
        chat_id: str,
        root_id: str | None,
        parent_id: str | None,
        provider_thread_id: str | None,
        sequence: Any,
    ) -> tuple[str | None, str]:
        if root_id is None and parent_id is None:
            if self.binding.conversation_mode.value == "topic_group":
                return None, "routing_indeterminate"
            if sequence["selection_state"] != "active" or not sequence["current_task_id"]:
                return None, "routing_indeterminate"
            return str(sequence["current_task_id"]), "routed_current"
        if root_id is None or parent_id is None:
            return None, "routing_indeterminate"
        root = connection.execute(
            "SELECT thread_id,chat_id,provider_thread_id FROM message_ancestry WHERE message_id=?",
            (root_id,),
        ).fetchone()
        parent = connection.execute(
            "SELECT root_id,thread_id,chat_id,provider_thread_id FROM message_ancestry WHERE message_id=?",
            (parent_id,),
        ).fetchone()
        if root is None or parent is None:
            return None, "routing_indeterminate"
        parent_root = parent["root_id"] or parent_id
        if (
            root["chat_id"] != chat_id
            or parent["chat_id"] != chat_id
            or root["thread_id"] != parent["thread_id"]
            or parent_root != root_id
        ):
            return None, "routing_indeterminate"
        if self.binding.conversation_mode.value == "topic_group":
            if not provider_thread_id:
                return None, "routing_indeterminate"
            known_threads = {
                value
                for value in (root["provider_thread_id"], parent["provider_thread_id"])
                if value
            }
            if known_threads and known_threads != {provider_thread_id}:
                return None, "routing_indeterminate"
            task = connection.execute(
                "SELECT provider_thread_id,conversation_mode,chat_id,opted_in FROM task_bindings WHERE thread_id=?",
                (root["thread_id"],),
            ).fetchone()
            if (
                task is None
                or not task["opted_in"]
                or task["conversation_mode"] != "topic_group"
                or task["chat_id"] != chat_id
            ):
                return None, "routing_indeterminate"
            if task["provider_thread_id"] not in {None, "", provider_thread_id}:
                return None, "routing_indeterminate"
            connection.execute(
                "UPDATE task_bindings SET provider_thread_id=COALESCE(provider_thread_id,?),updated_at=? "
                "WHERE thread_id=?",
                (provider_thread_id, utc_now(), root["thread_id"]),
            )
            connection.execute(
                "UPDATE message_ancestry SET provider_thread_id=COALESCE(provider_thread_id,?) "
                "WHERE message_id IN (?,?)",
                (provider_thread_id, root_id, parent_id),
            )
        return str(root["thread_id"]), "routed_reply"

    def _chat_is_authorized(self, connection: Any, chat_id: str) -> bool:
        if self.binding.conversation_mode.value == "p2p":
            return chat_id == self.binding.target_chat_id
        row = connection.execute(
            "SELECT 1 FROM project_groups WHERE chat_id=? AND state='active'",
            (chat_id,),
        ).fetchone()
        return row is not None

    @staticmethod
    def _identity_epoch(connection: Any) -> int:
        row = connection.execute(
            "SELECT binding_epoch,state FROM identity_bindings WHERE binding_key='owner'"
        ).fetchone()
        if row is None or row["state"] != "active":
            raise IngressRejected("identity binding is not active")
        return int(row["binding_epoch"])

    @staticmethod
    def _open_conflict(connection: Any, message_id: str) -> None:
        connection.execute(
            "INSERT INTO circuit_breakers(breaker_name,state,reason,updated_at) VALUES('ingress_conflict','open',?,?) "
            "ON CONFLICT(breaker_name) DO UPDATE SET state='open',reason=excluded.reason,updated_at=excluded.updated_at",
            ("message_id:" + message_id, utc_now()),
        )

    def begin_selection(self, target_thread_id: str) -> int:
        with self.storage.immediate() as connection:
            sequence = connection.execute(
                "SELECT active_binding_epoch,selection_state FROM chat_sequences WHERE binding_key='owner'"
            ).fetchone()
            if sequence is None or sequence["selection_state"] != "active":
                raise IngressRejected("another selection is pending or indeterminate")
            target = connection.execute(
                "SELECT 1 FROM task_bindings WHERE thread_id=? AND opted_in=1 AND anchor_state='confirmed'",
                (target_thread_id,),
            ).fetchone()
            if target is None:
                raise IngressRejected("target task is not selectable")
            next_epoch = int(sequence["active_binding_epoch"]) + 1
            connection.execute(
                "UPDATE chat_sequences SET pending_task_id=?,pending_binding_epoch=?,"
                "selection_state='selection_pending',updated_at=? WHERE binding_key='owner' AND selection_state='active'",
                (target_thread_id, next_epoch, utc_now()),
            )
            return next_epoch

    def queue_selection_confirmation(self, target_thread_id: str, epoch: int) -> str:
        import json

        logical_id = f"selection:{epoch}:{target_thread_id}"
        body = json.dumps(
            {"text": f"已选择任务 {target_thread_id[:8]}，绑定版本 {epoch}"},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self.storage.immediate() as connection:
            connection.execute(
                "INSERT INTO selection_confirmations(logical_message_id,target_thread_id,binding_epoch,state,updated_at) "
                "VALUES(?,?,?,'pending',?)",
                (logical_id, target_thread_id, epoch, utc_now()),
            )
            connection.execute(
                "INSERT INTO provider_outbox(logical_message_id,thread_id,operation,endpoint_name,stable_uuid,"
                "marker,body_json,body_hash,priority,state,next_attempt_at,created_at,updated_at) "
                "VALUES(?,?, 'selection','send_message',?,?,?,?,200,'pending',?,?,?)",
                (
                    logical_id,
                    target_thread_id,
                    stable_uuid(
                        logical_id,
                        chat_id=self.binding.target_chat_id,
                        conversation_mode=self.binding.conversation_mode.value,
                    ),
                    "selection:" + str(epoch),
                    body,
                    sha256(body.encode("utf-8")).hexdigest(),
                    utc_now(),
                    utc_now(),
                    utc_now(),
                ),
            )
        return logical_id

    def confirm_selection(self, target_thread_id: str, epoch: int) -> None:
        updated = self.storage.connection.execute(
            "UPDATE chat_sequences SET current_task_id=?,active_binding_epoch=?,pending_task_id=NULL,"
            "pending_binding_epoch=NULL,selection_state='active',updated_at=? WHERE binding_key='owner' "
            "AND selection_state='selection_pending' AND pending_task_id=? AND pending_binding_epoch=?",
            (target_thread_id, epoch, utc_now(), target_thread_id, epoch),
        )
        if updated.rowcount != 1:
            raise IngressRejected("selection confirmation lost compare-and-swap")

    def mark_selection_indeterminate(self) -> None:
        self.storage.connection.execute(
            "UPDATE chat_sequences SET selection_state='selection_indeterminate',updated_at=? "
            "WHERE binding_key='owner' AND selection_state='selection_pending'",
            (utc_now(),),
        )
