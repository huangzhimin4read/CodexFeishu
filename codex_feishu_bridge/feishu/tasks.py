"""Stable task-anchor lifecycle."""

from __future__ import annotations

import json
import unicodedata
import uuid
from hashlib import sha256
from pathlib import Path

from ..runtime_storage import RuntimeStorage, utc_now
from .formatter import invisible_marker


_TASK_NAMESPACE = uuid.UUID("e9d2e9bb-d020-5e69-99f7-7e47673abddf")


def normalize_task_title(value: str | None, thread_id: str) -> str:
    """Return a compact provider-safe title without trusting display text."""

    fallback = f"任务 {thread_id[:8]}"
    if not isinstance(value, str):
        return fallback
    visible_characters: list[str] = []
    for character in value:
        if character.isspace():
            visible_characters.append(" ")
        elif character != "|" and unicodedata.category(character) not in {"Cc", "Cf", "Cs"}:
            visible_characters.append(character)
    visible = "".join(visible_characters)
    normalized = " ".join(visible.split()).strip()
    if not normalized:
        return fallback
    return normalized[:80].rstrip() or fallback


def normalize_project_name(value: str | None, project_root: Path) -> str:
    fallback = project_root.resolve().name or "项目"
    if not isinstance(value, str):
        return fallback
    visible_characters: list[str] = []
    for character in value:
        if character.isspace():
            visible_characters.append(" ")
        elif character != "|" and unicodedata.category(character) not in {"Cc", "Cf", "Cs"}:
            visible_characters.append(character)
    normalized = " ".join("".join(visible_characters).split()).strip()
    return normalized[:60].rstrip() or fallback


def topic_title(task_title: str, project_name: str) -> str:
    return f"{task_title}|{project_name}"


def _provider_clean_title_hash(visible_title: str) -> str:
    # Versioned so a deployment that previously embedded a marker is forced to
    # update the same provider anchor even when its visible title is unchanged.
    return sha256(b"provider-clean-v1\x00" + visible_title.encode("utf-8")).hexdigest()


class TaskAnchorManager:
    def __init__(self, storage: RuntimeStorage) -> None:
        self.storage = storage

    def opt_in(
        self,
        *,
        thread_id: str,
        project_root: Path,
        chat_id: str,
        conversation_mode: str = "p2p",
        task_title: str | None = None,
        project_name: str | None = None,
    ) -> str:
        if conversation_mode not in {"p2p", "topic_group"}:
            raise ValueError("unsupported task conversation mode")
        task_uuid = str(
            uuid.uuid5(
                _TASK_NAMESPACE,
                f"{project_root.resolve()}\x1f{thread_id}\x1f{conversation_mode}\x1f{chat_id}",
            )
        )
        marker = invisible_marker("task:" + sha256(task_uuid.encode()).hexdigest()[:24])
        display_title = normalize_task_title(task_title, thread_id)
        display_project = normalize_project_name(project_name, project_root)
        visible_title = topic_title(display_title, display_project)
        title_hash = _provider_clean_title_hash(visible_title)
        now = utc_now()
        with self.storage.immediate() as connection:
            identity = connection.execute(
                "SELECT binding_epoch,state FROM identity_bindings WHERE binding_key='owner'"
            ).fetchone()
            identity_epoch = int(identity["binding_epoch"]) if identity and identity["state"] == "active" else 0
            existing = connection.execute(
                "SELECT chat_id,conversation_mode FROM task_bindings WHERE thread_id=?",
                (thread_id,),
            ).fetchone()
            if existing is not None and (
                existing["chat_id"] != chat_id
                or existing["conversation_mode"] != conversation_mode
            ):
                raise RuntimeError("task is already bound to another provider surface")
            connection.execute(
                "INSERT INTO task_bindings(thread_id,project_root,chat_id,anchor_state,anchor_uuid,"
                "anchor_marker,identity_binding_epoch,conversation_mode,task_title,project_name,"
                "pending_title_hash,"
                "title_revision,opted_in,updated_at) "
                "VALUES(?,?,?,'pending',?,?,?,?,?,?,?,1,1,?) "
                "ON CONFLICT(thread_id) DO UPDATE SET opted_in=1,lifecycle_state='active',"
                "updated_at=excluded.updated_at",
                (
                    thread_id,
                    str(project_root.resolve()),
                    chat_id,
                    task_uuid,
                    marker,
                    identity_epoch,
                    conversation_mode,
                    display_title,
                    display_project,
                    title_hash,
                    now,
                ),
            )
            body = json.dumps(
                {"text": visible_title},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            connection.execute(
                "INSERT OR IGNORE INTO provider_outbox(logical_message_id,thread_id,operation,endpoint_name,"
                "stable_uuid,marker,body_json,body_hash,priority,state,next_attempt_at,created_at,updated_at) "
                "VALUES(?,?, 'anchor','send_message',?,?,?,?,100,'pending',?,?,?)",
                (
                    "anchor:" + task_uuid,
                    thread_id,
                    task_uuid,
                    marker,
                    body,
                    sha256(body.encode("utf-8")).hexdigest(),
                    now,
                    now,
                    now,
                ),
            )
        return task_uuid

    def sync_title(self, thread_id: str, task_title: str, project_name: str | None = None) -> bool:
        """Queue one serialized update when an existing topic title changed."""

        display_title = normalize_task_title(task_title, thread_id)
        now = utc_now()
        with self.storage.immediate() as connection:
            binding = connection.execute(
                "SELECT project_root,anchor_message_id,anchor_state,anchor_uuid,anchor_title_hash,"
                "pending_title_hash,blocked_title_hash,title_revision FROM task_bindings "
                "WHERE thread_id=? AND opted_in=1",
                (thread_id,),
            ).fetchone()
            if binding is None or binding["anchor_state"] != "confirmed" or not binding["anchor_message_id"]:
                return False
            display_project = normalize_project_name(project_name, Path(binding["project_root"]))
            visible_title = topic_title(display_title, display_project)
            title_hash = _provider_clean_title_hash(visible_title)
            if (
                binding["anchor_title_hash"] == title_hash
                or binding["pending_title_hash"] is not None
                or binding["blocked_title_hash"] == title_hash
            ):
                return False
            revision = int(binding["title_revision"]) + 1
            marker = invisible_marker(
                "task:" + sha256(str(binding["anchor_uuid"]).encode()).hexdigest()[:24]
            )
            body = json.dumps(
                {"text": visible_title},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            update_uuid = str(
                uuid.uuid5(_TASK_NAMESPACE, f"title\x1f{thread_id}\x1f{revision}\x1f{title_hash}")
            )
            connection.execute(
                "INSERT INTO provider_outbox(logical_message_id,thread_id,item_id,operation,endpoint_name,"
                "target_message_id,stable_uuid,marker,body_json,body_hash,priority,state,next_attempt_at,"
                "created_at,updated_at) VALUES(?,?,?,'anchor_title','update_message',?,?,?,?,?,95,'pending',?,?,?)",
                (
                    f"anchor-title:{thread_id}:{revision}:{title_hash[:16]}",
                    thread_id,
                    title_hash,
                    binding["anchor_message_id"],
                    update_uuid,
                    marker,
                    body,
                    sha256(body.encode("utf-8")).hexdigest(),
                    now,
                    now,
                    now,
                ),
            )
            updated = connection.execute(
                "UPDATE task_bindings SET task_title=?,project_name=?,pending_title_hash=?,"
                "blocked_title_hash=NULL,title_sync_error=NULL,title_revision=?,updated_at=? "
                "WHERE thread_id=? AND pending_title_hash IS NULL AND title_revision=?",
                (
                    display_title,
                    display_project,
                    title_hash,
                    revision,
                    now,
                    thread_id,
                    revision - 1,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError("task title update lost compare-and-swap")
        return True

    def archive(self, thread_id: str) -> bool:
        """Deactivate one task and mark its existing topic as archived.

        Feishu has no supported per-topic unsubscribe API.  This lifecycle is
        therefore deliberately local and enforceable: remote grants are
        revoked immediately and no new task traffic is accepted or mirrored.
        The root message is updated in place when possible, without creating a
        second topic.
        """

        now = utc_now()
        changed = False
        with self.storage.immediate() as connection:
            binding = connection.execute(
                "SELECT project_root,anchor_message_id,anchor_state,anchor_uuid,anchor_title_hash,"
                "pending_title_hash,blocked_title_hash,title_revision,task_title,project_name,"
                "opted_in,lifecycle_state "
                "FROM task_bindings WHERE thread_id=?",
                (thread_id,),
            ).fetchone()
            if binding is None:
                return False
            if binding["opted_in"] or binding["lifecycle_state"] != "archived":
                connection.execute(
                    "UPDATE task_bindings SET opted_in=0,lifecycle_state='archived',updated_at=? "
                    "WHERE thread_id=?",
                    (now, thread_id),
                )
                changed = True
            connection.execute(
                "UPDATE remote_task_grants SET state='revoked',updated_at=? "
                "WHERE thread_id=? AND state!='revoked'",
                (now, thread_id),
            )
            if (
                binding["anchor_state"] != "confirmed"
                or not binding["anchor_message_id"]
                or binding["pending_title_hash"] is not None
            ):
                return changed
            display_title = normalize_task_title(binding["task_title"], thread_id)
            archived_title = normalize_task_title("【已归档】" + display_title, thread_id)
            display_project = normalize_project_name(
                binding["project_name"], Path(binding["project_root"])
            )
            visible_title = topic_title(archived_title, display_project)
            title_hash = _provider_clean_title_hash(visible_title)
            if (
                binding["anchor_title_hash"] == title_hash
                or binding["blocked_title_hash"] == title_hash
            ):
                return changed
            revision = int(binding["title_revision"]) + 1
            marker = invisible_marker(
                "task:" + sha256(str(binding["anchor_uuid"]).encode()).hexdigest()[:24]
            )
            body = json.dumps(
                {"text": visible_title},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            update_uuid = str(
                uuid.uuid5(_TASK_NAMESPACE, f"title\x1f{thread_id}\x1f{revision}\x1f{title_hash}")
            )
            connection.execute(
                "INSERT INTO provider_outbox(logical_message_id,thread_id,item_id,operation,endpoint_name,"
                "target_message_id,stable_uuid,marker,body_json,body_hash,priority,state,next_attempt_at,"
                "created_at,updated_at) VALUES(?,?,?,'anchor_title','update_message',?,?,?,?,?,95,'pending',?,?,?)",
                (
                    f"anchor-title:{thread_id}:{revision}:{title_hash[:16]}",
                    thread_id,
                    title_hash,
                    binding["anchor_message_id"],
                    update_uuid,
                    marker,
                    body,
                    sha256(body.encode("utf-8")).hexdigest(),
                    now,
                    now,
                    now,
                ),
            )
            updated = connection.execute(
                "UPDATE task_bindings SET pending_title_hash=?,blocked_title_hash=NULL,"
                "title_sync_error=NULL,title_revision=?,updated_at=? "
                "WHERE thread_id=? AND pending_title_hash IS NULL AND title_revision=?",
                (title_hash, revision, now, thread_id, revision - 1),
            )
            if updated.rowcount != 1:
                raise RuntimeError("archived task title update lost compare-and-swap")
            changed = True
        return changed

    def reactivate(self, thread_id: str) -> bool:
        """Restore a previously archived binding when Codex activates it again."""

        updated = self.storage.connection.execute(
            "UPDATE task_bindings SET opted_in=1,lifecycle_state='active',updated_at=? "
            "WHERE thread_id=? AND (opted_in=0 OR lifecycle_state!='active')",
            (utc_now(), thread_id),
        )
        return updated.rowcount == 1

    def confirm(self, thread_id: str, message_id: str) -> None:
        updated = self.storage.connection.execute(
            "UPDATE task_bindings SET anchor_message_id=?,anchor_state='confirmed',"
            "anchor_title_hash=pending_title_hash,pending_title_hash=NULL,updated_at=? "
            "WHERE thread_id=? AND anchor_state='pending'",
            (message_id, utc_now(), thread_id),
        )
        if updated.rowcount != 1:
            raise RuntimeError("task anchor confirmation lost compare-and-swap")
        binding = self.storage.connection.execute(
            "SELECT chat_id FROM task_bindings WHERE thread_id=?", (thread_id,)
        ).fetchone()
        self.storage.connection.execute(
            "INSERT OR REPLACE INTO message_ancestry(message_id,root_id,parent_id,thread_id,chat_id,source,created_at) "
            "VALUES(?,NULL,NULL,?,?,?,?)",
            (message_id, thread_id, binding["chat_id"], "anchor", utc_now()),
        )
