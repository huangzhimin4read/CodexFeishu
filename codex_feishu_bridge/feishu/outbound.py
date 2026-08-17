"""Durable source-to-provider pipeline and delivery worker."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

from ..codex.desktop_dispatch import matches_desktop_submission
from ..codex.desktop_relay_dispatch import matches_desktop_relay_submission
from ..models import EventKind, NormalizedEvent, RolloutBatch
from ..runtime_storage import RuntimeStorage, utc_now
from .client import FeishuClient, ProviderOutcome, ProviderResult
from .formatter import format_text_chunks, invisible_marker
from .images import LocalImage, extract_local_images
from .user_cli import LarkCliUserSender


_NAMESPACE = uuid.UUID("72f6d750-c92e-54e8-ae48-88cff2d6a9af")
_WAITING_FOR_REPLY_TEXT = "🔔【等待你的回应】"
_SUBAGENT_NOTIFICATION = re.compile(
    r"\A<subagent_notification>.*</subagent_notification>\Z",
    re.DOTALL,
)


def suppress_queued_internal_user_notifications(storage: RuntimeStorage) -> int:
    """Retire queued orchestration envelopes without deleting audit evidence."""

    rows = storage.connection.execute(
        "SELECT outbox_id,body_json FROM provider_outbox WHERE operation='user_message' "
        "AND state IN ('pending','retryable')"
    ).fetchall()
    suppressed: list[int] = []
    for row in rows:
        try:
            body = json.loads(row["body_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        text = body.get("text") if isinstance(body, dict) else None
        if isinstance(text, str) and _SUBAGENT_NOTIFICATION.fullmatch(text.strip("\r\n")):
            suppressed.append(int(row["outbox_id"]))
    if not suppressed:
        return 0
    placeholders = ",".join("?" for _ in suppressed)
    with storage.immediate() as connection:
        result = connection.execute(
            "UPDATE provider_outbox SET state='permanent',"
            "last_error_code='internal_user_notification_suppressed',"
            "lease_owner=NULL,lease_expires_at=NULL,updated_at=? "
            f"WHERE outbox_id IN ({placeholders}) AND state IN ('pending','retryable')",
            (utc_now(), *suppressed),
        )
    return int(result.rowcount)


def stable_uuid(material: str, *, chat_id: str, conversation_mode: str) -> str:
    """Return a provider idempotency key scoped to one delivery surface.

    Feishu de-duplicates UUIDs across the application, not merely within a
    chat.  Parallel P2P and topic-group instances must therefore never reuse
    the same UUID for the same Codex logical item.
    """
    return str(uuid.uuid5(_NAMESPACE, f"{conversation_mode}\x1f{chat_id}\x1f{material}"))


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    inserted_items: int
    queued_messages: int


class OutboundPipeline:
    def __init__(
        self,
        storage: RuntimeStorage,
        *,
        owner_display_name: str = "用户",
        user_messages_as_user: bool = False,
    ) -> None:
        if storage.sink_mode not in {"outbound", "control", "pilot"}:
            raise PermissionError("provider pipeline requires an outbound-capable database")
        self.storage = storage
        self.owner_display_name = owner_display_name.strip() or "用户"
        self.user_messages_as_user = user_messages_as_user

    def ingest_rollout_batch(self, batch: RolloutBatch) -> EnqueueResult:
        """Atomically store source items, outbox rows, and source cursor."""
        queued = 0
        with self.storage.immediate() as connection:
            current = connection.execute(
                "SELECT file_id,committed_offset,last_record_hash,schema_version FROM source_cursors WHERE source_path=?",
                (batch.cursor.source_path,),
            ).fetchone()
            if current is not None:
                if current["file_id"] != batch.cursor.file_id:
                    raise RuntimeError("source identity changed")
                if batch.cursor.schema_version != current["schema_version"]:
                    raise RuntimeError("source schema version changed")
                if batch.cursor.committed_offset < current["committed_offset"]:
                    raise RuntimeError("source cursor moved backwards")
                if batch.cursor.committed_offset == current["committed_offset"] and (
                    batch.cursor.last_record_hash != current["last_record_hash"]
                ):
                    raise RuntimeError("same source offset has conflicting record hash")
            inserted = 0
            for event in batch.events:
                result = connection.execute(
                    "INSERT OR IGNORE INTO items(logical_key,thread_id,turn_id,item_id,event_kind,revision,"
                    "content_hash,text,source_type,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        event.logical_key,
                        event.thread_id,
                        event.turn_id,
                        event.item_id,
                        event.kind.value,
                        event.revision,
                        event.content_hash,
                        event.text,
                        event.source_type,
                        utc_now(),
                    ),
                )
                if result.rowcount != 1:
                    continue
                inserted += 1
                queued += self._enqueue_event(connection, event)
            connection.execute(
                "INSERT INTO source_cursors(source_path,file_id,committed_offset,last_record_hash,schema_version,updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(source_path) DO UPDATE SET "
                "file_id=excluded.file_id,committed_offset=excluded.committed_offset,"
                "last_record_hash=excluded.last_record_hash,schema_version=excluded.schema_version,updated_at=excluded.updated_at",
                (
                    batch.cursor.source_path,
                    batch.cursor.file_id,
                    batch.cursor.committed_offset,
                    batch.cursor.last_record_hash,
                    batch.cursor.schema_version,
                    utc_now(),
                ),
            )
        return EnqueueResult(inserted, queued)

    def _enqueue_event(self, connection: object, event: NormalizedEvent) -> int:
        binding = connection.execute(
            "SELECT anchor_message_id,anchor_state,chat_id,conversation_mode,project_root "
            "FROM task_bindings WHERE thread_id=? AND opted_in=1",
            (event.thread_id,),
        ).fetchone()
        if binding is None:
            raise RuntimeError("event thread is not opted in")
        if binding["anchor_state"] != "confirmed" or not binding["anchor_message_id"]:
            raise RuntimeError("task anchor is not confirmed")

        # A Feishu message submitted through a Codex writer is persisted as a
        # normal Codex user item. Suppress only that exact item when it comes
        # back through rollout observation. Other user messages in the same
        # turn (for example a Codex-originated steer) must still be mirrored.
        if event.source_type == "user_message":
            returning = connection.execute(
                "SELECT 1 FROM dispatch_records WHERE thread_id=? AND turn_id=? "
                "AND user_item_id=? AND state IN ('accepted','completed') LIMIT 1",
                (event.thread_id, event.turn_id, event.item_id),
            ).fetchone()
            if returning is not None:
                return 0
            if self._reconcile_delayed_desktop_return(connection, event):
                return 0

        display_text = event.text
        if event.source_type == "user_message" and not self.user_messages_as_user:
            display_text = (
                f"👤 {self.owner_display_name}：{display_text}"
                if display_text
                else f"👤 {self.owner_display_name}：图片"
            )
        extracted = extract_local_images(
            display_text, project_root=Path(str(binding["project_root"]))
        )
        for index, reason in enumerate(extracted.failures, start=1):
            connection.execute(
                "INSERT INTO dead_letters(category,record_hash,reason,created_at) VALUES(?,?,?,?)",
                (
                    "local_image",
                    sha256(f"{event.logical_key}:{index}:{reason}".encode()).hexdigest(),
                    reason,
                    utc_now(),
                ),
            )
        chunks = (
            format_text_chunks(extracted.text, marker_seed=event.logical_key)
            if extracted.text.strip()
            else ()
        )
        candidate_images = list(extracted.images)
        for index, image in enumerate(event.images, start=1):
            candidate_images.append(
                LocalImage(
                    label=f"Codex 图像 {index}",
                    source_path=(
                        f"rollout://{event.thread_id}/{event.turn_id}/{event.item_id}/{index}"
                    ),
                    file_name=f"codex-image-{image.content_hash[:16]}{image.suffix}",
                    mime_type=image.mime_type,
                    content=image.content,
                    content_hash=image.content_hash,
                )
            )
        existing_hashes = {
            str(row[0])
            for row in connection.execute(
                "SELECT image.content_hash FROM outbound_images image "
                "JOIN provider_outbox outbox ON outbox.outbox_id=image.outbox_id "
                "WHERE outbox.thread_id=? AND outbox.turn_id=?",
                (event.thread_id, event.turn_id),
            ).fetchall()
        }
        images: list[LocalImage] = []
        event_hashes: set[str] = set()
        for image in candidate_images:
            if image.content_hash in event_hashes:
                continue
            # A final answer is a self-contained handoff. If it explicitly
            # references an image, send that image with the final even when an
            # earlier commentary/tool item already displayed identical bytes.
            # Duplicates inside this event are still removed by event_hashes.
            if image.content_hash in existing_hashes and event.kind is not EventKind.FINAL_ANSWER:
                continue
            event_hashes.add(image.content_hash)
            images.append(image)
        now = utc_now()
        priority = 100 if event.kind is EventKind.FINAL_ANSWER else 10
        endpoint = "reply_message"
        units: list[tuple[str, str, str, str, LocalImage | None]] = []
        for chunk in chunks:
            logical_id = f"{event.logical_key}:{chunk.index}"
            units.append((logical_id, "text", chunk.marker, chunk.body_json, None))
        for index, image in enumerate(images, start=1):
            logical_id = f"{event.logical_key}:image:{index}"
            marker = "img:" + sha256(logical_id.encode()).hexdigest()[:24]
            units.append((logical_id, "image", marker, '{"image_key":null}', image))
        if event.kind is EventKind.FINAL_ANSWER:
            # Keep the attention cue as the final provider message so Feishu's
            # topic preview and the bottom of the conversation both make the
            # handoff state obvious. Final-answer content and images preceding
            # this cue are durable final parts, not transient commentary.
            logical_id = f"{event.logical_key}:waiting-for-reply"
            marker = invisible_marker(
                "cfb:" + sha256(logical_id.encode()).hexdigest()[:24]
            )
            body_json = json.dumps(
                {"text": _WAITING_FOR_REPLY_TEXT},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            units.append((logical_id, "text", marker, body_json, None))
        queued_units = 0
        for index, (logical_id, message_type, marker, body_json, image) in enumerate(
            units, start=1
        ):
            if event.source_type == "user_message":
                operation = "user_message"
            elif event.kind is EventKind.FINAL_ANSWER and index == len(units):
                operation = "final"
            else:
                operation = "commentary"
            body_hash = (
                image.content_hash
                if image is not None
                else sha256(body_json.encode("utf-8")).hexdigest()
            )
            inserted = connection.execute(
                "INSERT INTO provider_outbox(logical_message_id,thread_id,turn_id,item_id,operation,"
                "message_type,endpoint_name,target_message_id,reply_in_thread,stable_uuid,marker,body_json,body_hash,"
                "priority,state,next_attempt_at,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(logical_message_id) DO NOTHING",
                (
                    logical_id,
                    event.thread_id,
                    event.turn_id,
                    event.item_id,
                    operation,
                    message_type,
                    endpoint,
                    binding["anchor_message_id"],
                    int(binding["conversation_mode"] == "topic_group"),
                    stable_uuid(
                        logical_id,
                        chat_id=binding["chat_id"],
                        conversation_mode=binding["conversation_mode"],
                    ),
                    marker,
                    body_json,
                    body_hash,
                    priority,
                    "pending",
                    now,
                    now,
                    now,
                ),
            )
            if inserted.rowcount != 1:
                continue
            queued_units += 1
            if image is not None:
                outbox_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
                connection.execute(
                    "INSERT INTO outbound_images(outbox_id,source_path,file_name,mime_type,content,"
                    "content_hash,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        outbox_id,
                        image.source_path,
                        image.file_name,
                        image.mime_type,
                        image.content,
                        image.content_hash,
                        now,
                    ),
                )
        return queued_units

    @staticmethod
    def _reconcile_delayed_desktop_return(
        connection: object,
        event: NormalizedEvent,
    ) -> bool:
        """Claim a late rollout item for one confirmed desktop UI submission."""

        candidates = connection.execute(
            "SELECT dispatch_attempt_id,ingress_message_id,request_hash,request_id,profile_hash,"
            "submitted_text_hash,has_attachments FROM dispatch_records "
            "WHERE thread_id=? AND state='outcome_unknown' "
            "AND request_id IN ('desktop-ui-submitted','desktop-relay-submitted') "
            "AND submitted_text_hash IS NOT NULL "
            "AND julianday(created_at)>=julianday('now','-2 hours') "
            "ORDER BY created_at,dispatch_attempt_id LIMIT 20",
            (event.thread_id,),
        ).fetchall()
        for candidate in candidates:
            if candidate["request_id"] == "desktop-relay-submitted":
                profile_hash = str(candidate["profile_hash"] or "")
                prefix = "desktop-relay:"
                if not profile_hash.startswith(prefix) or not matches_desktop_relay_submission(
                    event.text,
                    str(candidate["submitted_text_hash"]),
                    relay_thread_id=profile_hash.removeprefix(prefix),
                ):
                    continue
            elif not matches_desktop_submission(
                event.text,
                str(candidate["submitted_text_hash"]),
                has_attachments=bool(candidate["has_attachments"]),
            ):
                continue
            updated = connection.execute(
                "UPDATE dispatch_records SET state='accepted',turn_id=?,user_item_id=?,updated_at=? "
                "WHERE dispatch_attempt_id=? AND state='outcome_unknown' "
                "AND request_id=?",
                (
                    event.turn_id,
                    event.item_id,
                    utc_now(),
                    candidate["dispatch_attempt_id"],
                    candidate["request_id"],
                ),
            )
            if updated.rowcount != 1:
                continue
            response_hash = sha256(
                f"{event.thread_id}\x1f{event.turn_id}\x1f{event.item_id}".encode("utf-8")
            ).hexdigest()
            connection.execute(
                "UPDATE dispatch_attempts SET state='accepted',response_hash=?,updated_at=? "
                "WHERE dispatch_attempt_id=? AND state IN ('dispatching','outcome_unknown')",
                (response_hash, utc_now(), candidate["dispatch_attempt_id"]),
            )
            connection.execute(
                "INSERT OR IGNORE INTO executed_command_tombstones("
                "tombstone_key,content_hash,target_thread_id,dispatch_attempt_id,retain_until) "
                "VALUES(?,?,?,?,datetime('now','+365 days'))",
                (
                    candidate["ingress_message_id"],
                    candidate["request_hash"],
                    event.thread_id,
                    candidate["dispatch_attempt_id"],
                ),
            )
            return True
        return False


class OutboxWorker:
    def __init__(
        self,
        storage: RuntimeStorage,
        client: FeishuClient,
        instance_id: str,
        *,
        user_message_sender: LarkCliUserSender | None = None,
        owner_display_name: str = "用户",
    ) -> None:
        self.storage = storage
        self.client = client
        self.instance_id = instance_id
        self.user_message_sender = user_message_sender
        self.owner_display_name = owner_display_name.strip() or "用户"

    def run_once(self) -> bool:
        lease_until = (datetime.now(UTC) + timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
        row = self.storage.lease_outbox(self.instance_id, lease_until)
        if row is None:
            return False
        body = json.loads(row["body_json"])
        if not isinstance(body, dict):
            self.storage.finish_outbox(
                row["outbox_id"], self.instance_id, state="permanent", error_code="invalid_body"
            )
            return True
        binding = self.storage.connection.execute(
            "SELECT chat_id,conversation_mode,provider_thread_id,opted_in FROM task_bindings "
            "WHERE thread_id=?",
            (row["thread_id"],),
        ).fetchone()
        if binding is not None and not binding["opted_in"] and row["operation"] != "anchor_title":
            binding = None
        if binding is None and row["operation"] in {"control", "selection"}:
            binding = self.storage.connection.execute(
                "SELECT COALESCE(active_chat_id,p2p_chat_id) AS chat_id,conversation_mode,NULL AS provider_thread_id "
                "FROM identity_bindings "
                "WHERE binding_key='owner' AND state='active'"
            ).fetchone()
        if binding is None:
            self.storage.finish_outbox(
                row["outbox_id"], self.instance_id, state="permanent", error_code="binding_missing"
            )
            return True
        reply_parent: dict[str, object] | None = None
        if row["endpoint_name"] == "reply_message":
            if row["target_message_id"]:
                parent = self.storage.connection.execute(
                    "SELECT root_id,thread_id,chat_id,provider_thread_id FROM message_ancestry "
                    "WHERE message_id=?",
                    (row["target_message_id"],),
                ).fetchone()
                if parent is None:
                    anchor = self.storage.connection.execute(
                        "SELECT thread_id,chat_id,provider_thread_id FROM task_bindings "
                        "WHERE thread_id=? AND anchor_state='confirmed' AND anchor_message_id=?",
                        (row["thread_id"], row["target_message_id"]),
                    ).fetchone()
                    if anchor is not None:
                        reply_parent = {
                            "root_id": None,
                            "thread_id": anchor["thread_id"],
                            "chat_id": anchor["chat_id"],
                            "provider_thread_id": anchor["provider_thread_id"],
                        }
                else:
                    reply_parent = dict(parent)
            if (
                reply_parent is None
                or reply_parent["thread_id"] != row["thread_id"]
                or reply_parent["chat_id"] != binding["chat_id"]
            ):
                terminal = "final_undelivered" if row["operation"] == "final" else "permanent"
                self.storage.finish_outbox(
                    row["outbox_id"],
                    self.instance_id,
                    state=terminal,
                    error_code="reply_ancestry_conflict",
                )
                self.storage.connection.execute(
                    "INSERT INTO dead_letters(category,record_hash,reason,created_at) VALUES(?,?,?,?)",
                    (
                        "reply_ancestry",
                        sha256(
                            f"{row['outbox_id']}:{row['thread_id']}:{row['target_message_id']}".encode()
                        ).hexdigest(),
                        "reply target ancestry is missing or conflicts",
                        utc_now(),
                    ),
                )
                if terminal == "final_undelivered":
                    self.storage.connection.execute(
                        "INSERT INTO circuit_breakers(breaker_name,state,reason,updated_at) "
                        "VALUES('final_delivery','open',?,?) ON CONFLICT(breaker_name) DO UPDATE SET "
                        "state='open',reason=excluded.reason,updated_at=excluded.updated_at",
                        ("turn:" + str(row["turn_id"]), utc_now()),
                    )
                return True
        msg_type = "interactive" if row["operation"] == "approval" else row["message_type"]
        if msg_type == "image":
            image = self.storage.image_payload_for_lease(row["outbox_id"], self.instance_id)
            if not image["image_key"]:
                result = self.client.upload_image(
                    file_name=image["file_name"],
                    mime_type=image["mime_type"],
                    content=bytes(image["content"]),
                )
                if result.outcome is ProviderOutcome.CONFIRMED and result.image_key:
                    self.storage.stage_uploaded_image(
                        row["outbox_id"], self.instance_id, result.image_key
                    )
                elif result.outcome in {ProviderOutcome.RETRYABLE, ProviderOutcome.UNKNOWN}:
                    delay = result.retry_after_seconds or min(
                        300.0, 2 ** min(int(image["upload_attempt_count"]) + 1, 8)
                    )
                    retry_at = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat().replace(
                        "+00:00", "Z"
                    )
                    self.storage.finish_outbox(
                        row["outbox_id"],
                        self.instance_id,
                        state="retryable",
                        error_code="image_upload:" + result.code,
                        next_attempt_at=retry_at,
                    )
                else:
                    terminal = (
                        "final_undelivered" if row["operation"] == "final" else "permanent"
                    )
                    self.storage.finish_outbox(
                        row["outbox_id"],
                        self.instance_id,
                        state=terminal,
                        error_code="image_upload:" + result.code,
                    )
                    if terminal == "final_undelivered":
                        self.storage.connection.execute(
                            "INSERT INTO circuit_breakers(breaker_name,state,reason,updated_at) "
                            "VALUES('final_delivery','open',?,?) ON CONFLICT(breaker_name) DO UPDATE SET "
                            "state='open',reason=excluded.reason,updated_at=excluded.updated_at",
                            ("turn:" + str(row["turn_id"]), utc_now()),
                        )
                return True
            body = {"image_key": str(image["image_key"])}
        result: ProviderResult | None = None
        outbound_body_json = row["body_json"]
        if (
            row["operation"] in {"user_message", "subscription"}
            and msg_type == "text"
            and self.user_message_sender is not None
            and row["endpoint_name"] == "reply_message"
            and row["target_message_id"]
            and isinstance(body.get("text"), str)
        ):
            result = self.user_message_sender.reply_text(
                message_id=str(row["target_message_id"]),
                text=str(body["text"]),
                reply_in_thread=bool(row["reply_in_thread"]),
                idempotency_key=str(row["stable_uuid"]),
            )
            if result.outcome is ProviderOutcome.PERMANENT and row["operation"] == "user_message":
                fallback_body = {
                    "text": f"👤 {self.owner_display_name}：{body['text']}"
                }
                outbound_body_json = json.dumps(
                    fallback_body, ensure_ascii=False, separators=(",", ":")
                )
                self.storage.upsert_runtime_metadata(
                    "user_cli_last_fallback",
                    {"at": utc_now(), "code": result.code},
                )
                result = None
        if result is not None:
            pass
        elif row["endpoint_name"] == "send_message":
            result = self.client.call(
                "send_message",
                query={"receive_id_type": "chat_id"},
                json_body={
                    "receive_id": binding["chat_id"],
                    "content": outbound_body_json,
                    "msg_type": msg_type,
                    "uuid": row["stable_uuid"],
                },
                chat_id=binding["chat_id"],
            )
        elif row["endpoint_name"] == "update_message":
            result = self.client.call(
                "update_message",
                path_parameters={"message_id": row["target_message_id"]},
                json_body={
                    "content": outbound_body_json,
                    "msg_type": msg_type,
                },
                chat_id=binding["chat_id"],
            )
        else:
            result = self.client.call(
                row["endpoint_name"],
                path_parameters={"message_id": row["target_message_id"]},
                json_body={
                    "content": outbound_body_json,
                    "msg_type": msg_type,
                    "reply_in_thread": bool(row["reply_in_thread"]),
                    "uuid": row["stable_uuid"],
                },
                chat_id=binding["chat_id"],
            )
        if result.outcome is ProviderOutcome.CONFIRMED:
            if row["operation"] == "anchor_title":
                if not row["item_id"]:
                    raise RuntimeError("task title update lacks its title hash")
                self.storage.confirm_task_title_update(
                    row["outbox_id"],
                    self.instance_id,
                    thread_id=row["thread_id"],
                    title_hash=row["item_id"],
                )
            else:
                self.storage.finish_outbox(
                    row["outbox_id"],
                    self.instance_id,
                    state="confirmed",
                    provider_message_id=result.message_id,
                )
            if (
                result.message_id
                and row["endpoint_name"] == "reply_message"
                and row["target_message_id"]
            ):
                assert reply_parent is not None
                root_id = reply_parent["root_id"] or row["target_message_id"]
                self.storage.connection.execute(
                    "INSERT OR REPLACE INTO message_ancestry(message_id,root_id,parent_id,thread_id,chat_id,"
                    "provider_thread_id,source,created_at) VALUES(?,?,?,?,?,?,'outbound',?)",
                    (
                        result.message_id,
                        root_id,
                        row["target_message_id"],
                        row["thread_id"],
                        binding["chat_id"],
                        result.thread_id
                        or reply_parent["provider_thread_id"]
                        or binding["provider_thread_id"],
                        utc_now(),
                    ),
                )
            if row["operation"] == "final":
                self.storage.connection.execute(
                    "UPDATE transient_messages SET lifecycle_state='cleanup_queued',updated_at=? "
                    "WHERE turn_id=? AND lifecycle_state='transient_active'",
                    (utc_now(), row["turn_id"]),
                )
            elif row["operation"] == "commentary" and result.message_id:
                final_part = self.storage.connection.execute(
                    "SELECT 1 FROM provider_outbox WHERE thread_id=? AND turn_id=? AND item_id=? "
                    "AND operation='final' LIMIT 1",
                    (row["thread_id"], row["turn_id"], row["item_id"]),
                ).fetchone()
                if final_part is None:
                    self.storage.connection.execute(
                        "INSERT OR IGNORE INTO transient_messages(message_id,turn_id,message_type,"
                        "lifecycle_state,created_at,updated_at) "
                        "VALUES(?,?,?,'transient_active',?,?)",
                        (result.message_id, row["turn_id"], msg_type, utc_now(), utc_now()),
                    )
            elif row["operation"] == "anchor":
                self.storage.connection.execute(
                    "UPDATE task_bindings SET anchor_message_id=?,provider_thread_id=?,"
                    "anchor_state='confirmed',anchor_title_hash=pending_title_hash,"
                    "pending_title_hash=NULL,updated_at=? "
                    "WHERE thread_id=? AND anchor_state='pending'",
                    (result.message_id, result.thread_id, utc_now(), row["thread_id"]),
                )
                binding_row = self.storage.connection.execute(
                    "SELECT chat_id,provider_thread_id FROM task_bindings WHERE thread_id=?",
                    (row["thread_id"],),
                ).fetchone()
                self.storage.connection.execute(
                    "INSERT OR REPLACE INTO message_ancestry(message_id,root_id,parent_id,thread_id,chat_id,"
                    "provider_thread_id,source,created_at) VALUES(?,NULL,NULL,?,?,?,'anchor',?)",
                    (
                        result.message_id,
                        row["thread_id"],
                        binding_row["chat_id"],
                        binding_row["provider_thread_id"],
                        utc_now(),
                    ),
                )
                if (
                    self.user_message_sender is not None
                    and binding["conversation_mode"] == "topic_group"
                ):
                    subscription_text = "🔔 已订阅任务更新"
                    subscription_body = json.dumps(
                        {"text": subscription_text},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    logical_id = "subscription:" + str(row["thread_id"])
                    now = utc_now()
                    self.storage.connection.execute(
                        "INSERT INTO provider_outbox(logical_message_id,thread_id,operation,message_type,"
                        "endpoint_name,target_message_id,reply_in_thread,stable_uuid,marker,body_json,"
                        "body_hash,priority,state,next_attempt_at,created_at,updated_at) "
                        "VALUES(?,?,'subscription','text','reply_message',?,1,?,?,?,?,90,'pending',?,?,?) "
                        "ON CONFLICT(logical_message_id) DO NOTHING",
                        (
                            logical_id,
                            row["thread_id"],
                            result.message_id,
                            stable_uuid(
                                logical_id,
                                chat_id=str(binding["chat_id"]),
                                conversation_mode=str(binding["conversation_mode"]),
                            ),
                            invisible_marker(
                                "cfb:" + sha256(logical_id.encode()).hexdigest()[:24]
                            ),
                            subscription_body,
                            sha256(subscription_body.encode("utf-8")).hexdigest(),
                            now,
                            now,
                            now,
                        ),
                    )
            elif row["operation"] == "approval":
                approval_id = str(row["logical_message_id"]).removeprefix("approval-card:")
                self.storage.connection.execute(
                    "UPDATE approval_actions SET card_message_id=? WHERE approval_id=? "
                    "AND card_message_id=?",
                    (result.message_id, approval_id, row["logical_message_id"]),
                )
            elif row["operation"] == "selection":
                selection = self.storage.connection.execute(
                    "SELECT target_thread_id,binding_epoch FROM selection_confirmations "
                    "WHERE logical_message_id=? AND state='pending'",
                    (row["logical_message_id"],),
                ).fetchone()
                if selection is not None:
                    with self.storage.immediate() as connection:
                        promoted = connection.execute(
                            "UPDATE chat_sequences SET current_task_id=?,active_binding_epoch=?,"
                            "pending_task_id=NULL,pending_binding_epoch=NULL,selection_state='active',updated_at=? "
                            "WHERE binding_key='owner' AND selection_state='selection_pending' "
                            "AND pending_task_id=? AND pending_binding_epoch=?",
                            (
                                selection["target_thread_id"],
                                selection["binding_epoch"],
                                utc_now(),
                                selection["target_thread_id"],
                                selection["binding_epoch"],
                            ),
                        )
                        if promoted.rowcount != 1:
                            raise RuntimeError("selection confirmation lost compare-and-swap")
                        connection.execute(
                            "UPDATE task_bindings SET current_binding_epoch=?,updated_at=? WHERE thread_id=?",
                            (selection["binding_epoch"], utc_now(), selection["target_thread_id"]),
                        )
                        connection.execute(
                            "UPDATE selection_confirmations SET state='confirmed',updated_at=? "
                            "WHERE logical_message_id=? AND state='pending'",
                            (utc_now(), row["logical_message_id"]),
                        )
        elif result.outcome is ProviderOutcome.RETRYABLE:
            delay = result.retry_after_seconds or min(300.0, 2 ** min(row["attempt_count"], 8))
            retry_at = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat().replace("+00:00", "Z")
            self.storage.finish_outbox(
                row["outbox_id"],
                self.instance_id,
                state="retryable",
                error_code=result.code,
                next_attempt_at=retry_at,
            )
        elif result.outcome is ProviderOutcome.UNKNOWN:
            if row["operation"] == "anchor_title":
                self.storage.fail_task_title_update(
                    row["outbox_id"],
                    self.instance_id,
                    thread_id=row["thread_id"],
                    title_hash=row["item_id"],
                    state="delivery_indeterminate",
                    error_code=result.code,
                )
            else:
                self.storage.finish_outbox(
                    row["outbox_id"],
                    self.instance_id,
                    state="unknown",
                    error_code=result.code,
                )
        else:
            terminal = "final_undelivered" if row["operation"] == "final" else "permanent"
            if row["operation"] == "anchor_title":
                self.storage.fail_task_title_update(
                    row["outbox_id"],
                    self.instance_id,
                    thread_id=row["thread_id"],
                    title_hash=row["item_id"],
                    state="permanent",
                    error_code=result.code,
                )
            else:
                self.storage.finish_outbox(
                    row["outbox_id"], self.instance_id, state=terminal, error_code=result.code
                )
            if terminal == "final_undelivered":
                self.storage.connection.execute(
                    "INSERT INTO circuit_breakers(breaker_name,state,reason,updated_at) "
                    "VALUES('final_delivery','open',?,?) ON CONFLICT(breaker_name) DO UPDATE SET "
                    "state='open',reason=excluded.reason,updated_at=excluded.updated_at",
                    ("turn:" + str(row["turn_id"]), utc_now()),
                )
            if result.code in {"http_401", "http_403"}:
                self.storage.connection.execute(
                    "INSERT INTO circuit_breakers(breaker_name,state,reason,updated_at) VALUES('provider_auth','open',?,?) "
                    "ON CONFLICT(breaker_name) DO UPDATE SET state='open',reason=excluded.reason,updated_at=excluded.updated_at",
                    (result.code, utc_now()),
                )
        return True
