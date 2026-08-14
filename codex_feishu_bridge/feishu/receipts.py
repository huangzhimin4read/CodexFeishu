"""Durable, truthful status receipts for validated Feishu ingress."""

from __future__ import annotations

import json
from hashlib import sha256

from ..runtime_config import FeishuBinding
from ..runtime_storage import RuntimeStorage
from .outbound import stable_uuid


_STATUSES = frozenset({"control", "pending", "unconfirmed", "submitted"})


def queue_ingress_status(
    storage: RuntimeStorage,
    binding: FeishuBinding,
    *,
    message_id: str,
    target_thread_id: str | None,
    status: str,
    text: str,
    priority: int,
) -> int | None:
    """Queue one idempotent provider-visible status for an ingress message.

    ``pending`` means the bridge will retry because dispatch has not started.
    ``unconfirmed`` means the desktop operation may have happened but exact
    Codex acceptance could not be proved. ``submitted`` means the selected
    Codex writer accepted a new user turn. In desktop mode, acceptance requires
    that exact input to appear in newly appended rollout bytes. No state is
    described as "read", because Codex exposes no human-read receipt.
    """

    if status not in _STATUSES:
        raise ValueError("invalid ingress receipt status")
    logical_id = f"{status}-ack:{message_id}"
    existing = storage.connection.execute(
        "SELECT outbox_id FROM provider_outbox WHERE logical_message_id=?",
        (logical_id,),
    ).fetchone()
    if existing is not None:
        return None

    endpoint_name = "send_message"
    target_message_id = None
    reply_in_thread = False
    target_chat_id = binding.target_chat_id
    conversation_mode = binding.conversation_mode.value
    if target_thread_id is not None:
        task = storage.connection.execute(
            "SELECT conversation_mode,chat_id FROM task_bindings "
            "WHERE thread_id=? AND opted_in=1",
            (target_thread_id,),
        ).fetchone()
        if task is not None:
            target_chat_id = str(task["chat_id"])
            conversation_mode = str(task["conversation_mode"])
            if conversation_mode == "topic_group":
                endpoint_name = "reply_message"
                target_message_id = message_id
                reply_in_thread = True

    body = json.dumps({"text": text}, ensure_ascii=False, separators=(",", ":"))
    return storage.enqueue_provider_message(
        logical_message_id=logical_id,
        operation="control",
        endpoint_name=endpoint_name,
        target_message_id=target_message_id,
        reply_in_thread=reply_in_thread,
        stable_uuid=stable_uuid(
            logical_id,
            chat_id=target_chat_id,
            conversation_mode=conversation_mode,
        ),
        marker=f"{status}:" + message_id[:20],
        body_json=body,
        body_hash=sha256(body.encode("utf-8")).hexdigest(),
        priority=priority,
        thread_id=target_thread_id,
        item_id=message_id,
    )
