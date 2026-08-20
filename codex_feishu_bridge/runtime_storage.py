"""Runtime persistence for M1-M6 workflows."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from .storage import BridgeStorage, InvalidTransition


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


RUNTIME_SCHEMA_VERSION = 16


class RuntimeStorage(BridgeStorage):
    def initialize_runtime(self, *, sink_mode: str) -> None:
        if sink_mode not in {"shadow_only", "outbound", "control", "pilot"}:
            raise ValueError("invalid runtime sink mode")
        self.initialize()
        self.execute_schema_migration(
            """
            CREATE TABLE IF NOT EXISTS runtime_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS task_bindings (
                thread_id TEXT PRIMARY KEY,
                project_root TEXT NOT NULL,
                chat_id TEXT,
                anchor_message_id TEXT UNIQUE,
                anchor_state TEXT NOT NULL DEFAULT 'pending',
                anchor_uuid TEXT UNIQUE,
                anchor_marker TEXT UNIQUE,
                current_binding_epoch INTEGER NOT NULL DEFAULT 0,
                identity_binding_epoch INTEGER NOT NULL DEFAULT 0,
                conversation_mode TEXT NOT NULL DEFAULT 'p2p',
                provider_thread_id TEXT,
                task_title TEXT,
                project_name TEXT,
                anchor_title_hash TEXT,
                pending_title_hash TEXT,
                blocked_title_hash TEXT,
                title_sync_error TEXT,
                title_revision INTEGER NOT NULL DEFAULT 0,
                lifecycle_state TEXT NOT NULL DEFAULT 'active' CHECK(lifecycle_state IN ('active','archived')),
                opted_in INTEGER NOT NULL CHECK(opted_in IN (0,1)),
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS project_groups (
                project_id TEXT PRIMARY KEY,
                project_kind TEXT NOT NULL CHECK(project_kind='local'),
                display_name TEXT NOT NULL,
                root_paths_json TEXT NOT NULL,
                chat_id TEXT UNIQUE,
                chat_mode TEXT,
                group_message_type TEXT,
                state TEXT NOT NULL CHECK(state IN (
                    'creating','active','outcome_unknown','failed','disabled'
                )),
                create_uuid TEXT UNIQUE,
                create_attempted_at_ms INTEGER,
                last_activity_ms INTEGER NOT NULL,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS provider_outbox (
                outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
                logical_message_id TEXT UNIQUE NOT NULL,
                thread_id TEXT,
                turn_id TEXT,
                item_id TEXT,
                operation TEXT NOT NULL,
                message_type TEXT NOT NULL DEFAULT 'text' CHECK(message_type IN ('text','image','interactive')),
                endpoint_name TEXT NOT NULL,
                target_message_id TEXT,
                reply_in_thread INTEGER NOT NULL DEFAULT 0 CHECK(reply_in_thread IN (0,1)),
                stable_uuid TEXT NOT NULL,
                marker TEXT NOT NULL,
                body_json TEXT NOT NULL,
                body_hash TEXT NOT NULL,
                priority INTEGER NOT NULL,
                state TEXT NOT NULL CHECK(state IN (
                    'pending','leased','confirmed','retryable','permanent','unknown',
                    'delivery_indeterminate','retention_policy_blocked','final_undelivered'
                )),
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT NOT NULL,
                lease_owner TEXT,
                lease_expires_at TEXT,
                provider_message_id TEXT,
                first_attempt_at TEXT,
                last_error_code TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_provider_outbox_ready
                ON provider_outbox(state,next_attempt_at,priority,outbox_id);
            CREATE TABLE IF NOT EXISTS outbound_images (
                outbox_id INTEGER PRIMARY KEY REFERENCES provider_outbox(outbox_id) ON DELETE CASCADE,
                source_path TEXT NOT NULL,
                file_name TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                content BLOB NOT NULL CHECK(length(content)>0 AND length(content)<=10485760),
                content_hash TEXT NOT NULL,
                image_key TEXT,
                upload_attempt_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outbound_rich_images (
                outbox_id INTEGER NOT NULL REFERENCES provider_outbox(outbox_id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL CHECK(ordinal>0),
                source_path TEXT NOT NULL,
                file_name TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                content BLOB NOT NULL CHECK(length(content)>0 AND length(content)<=10485760),
                content_hash TEXT NOT NULL,
                image_key TEXT,
                upload_attempt_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(outbox_id,ordinal)
            );
            CREATE TABLE IF NOT EXISTS endpoint_contracts (
                contract_hash TEXT PRIMARY KEY,
                app_id TEXT NOT NULL,
                published_version TEXT NOT NULL,
                contract_json TEXT NOT NULL,
                activated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS identity_bindings (
                binding_key TEXT PRIMARY KEY,
                tenant_key TEXT NOT NULL,
                app_id TEXT NOT NULL,
                owner_open_id TEXT NOT NULL,
                p2p_chat_id TEXT NOT NULL,
                active_chat_id TEXT,
                active_chat_type TEXT NOT NULL DEFAULT 'p2p',
                conversation_mode TEXT NOT NULL DEFAULT 'p2p',
                binding_epoch INTEGER NOT NULL,
                contract_hash TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('active','drifted','revoked')),
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ingress_messages (
                tenant_key TEXT NOT NULL,
                app_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                event_id TEXT,
                chat_id TEXT NOT NULL,
                sender_open_id TEXT NOT NULL,
                chat_type TEXT NOT NULL,
                root_id TEXT,
                parent_id TEXT,
                provider_thread_id TEXT,
                message_type TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                raw_hash TEXT NOT NULL,
                received_at TEXT NOT NULL,
                ingest_seq INTEGER,
                routing_state TEXT NOT NULL,
                target_thread_id TEXT,
                binding_epoch INTEGER,
                identity_binding_epoch INTEGER,
                PRIMARY KEY(tenant_key,app_id,message_id)
            );
            CREATE TABLE IF NOT EXISTS ingress_payloads (
                message_id TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ingress_attachments (
                message_id TEXT PRIMARY KEY REFERENCES ingress_payloads(message_id) ON DELETE CASCADE,
                resource_key TEXT NOT NULL,
                resource_type TEXT NOT NULL CHECK(resource_type IN ('image','file')),
                original_file_name TEXT,
                mime_type TEXT,
                content BLOB,
                content_hash TEXT,
                local_path TEXT,
                state TEXT NOT NULL CHECK(state IN (
                    'pending','retryable','ready','materialized','permanent'
                )),
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT NOT NULL,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chat_sequences (
                binding_key TEXT PRIMARY KEY,
                next_ingest_seq INTEGER NOT NULL,
                current_task_id TEXT,
                active_binding_epoch INTEGER NOT NULL,
                pending_task_id TEXT,
                pending_binding_epoch INTEGER,
                selection_state TEXT NOT NULL CHECK(selection_state IN (
                    'active','selection_pending','selection_indeterminate'
                )),
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS message_ancestry (
                message_id TEXT PRIMARY KEY,
                root_id TEXT,
                parent_id TEXT,
                thread_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                provider_thread_id TEXT,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS approval_actions (
                token_hash TEXT PRIMARY KEY,
                approval_id TEXT UNIQUE NOT NULL,
                server_request_id TEXT NOT NULL,
                server_method TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                turn_id TEXT,
                tenant_key TEXT NOT NULL,
                app_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                card_message_id TEXT NOT NULL,
                operator_open_id TEXT NOT NULL,
                binding_epoch INTEGER NOT NULL,
                identity_binding_epoch INTEGER NOT NULL,
                kill_generation INTEGER NOT NULL,
                server_epoch TEXT NOT NULL,
                connection_epoch TEXT NOT NULL,
                session_id TEXT NOT NULL,
                service_fencing_token INTEGER NOT NULL,
                decision_map_json TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                consumed_decision TEXT,
                consumed_at TEXT,
                UNIQUE(server_epoch,connection_epoch,server_request_id)
            );
            CREATE TABLE IF NOT EXISTS service_state (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                instance_id TEXT,
                fencing_token INTEGER NOT NULL DEFAULT 0,
                kill_generation INTEGER NOT NULL DEFAULT 0,
                process_state TEXT NOT NULL DEFAULT 'stopped',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS transient_messages (
                message_id TEXT PRIMARY KEY,
                turn_id TEXT NOT NULL,
                message_type TEXT NOT NULL,
                lifecycle_state TEXT NOT NULL CHECK(lifecycle_state IN (
                    'transient_active','cleanup_queued','withdrawn','archived',
                    'retention_policy_blocked'
                )),
                operation_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS dispatch_records (
                dispatch_attempt_id TEXT PRIMARY KEY,
                ingress_message_id TEXT UNIQUE NOT NULL,
                thread_id TEXT NOT NULL,
                client_user_message_id TEXT UNIQUE NOT NULL,
                profile_hash TEXT NOT NULL,
                binding_epoch INTEGER NOT NULL,
                identity_binding_epoch INTEGER NOT NULL,
                fencing_token INTEGER NOT NULL,
                server_epoch TEXT NOT NULL,
                connection_epoch TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                request_id TEXT,
                submitted_text_hash TEXT,
                has_attachments INTEGER NOT NULL DEFAULT 0 CHECK(has_attachments IN (0,1)),
                turn_id TEXT,
                user_item_id TEXT,
                state TEXT NOT NULL CHECK(state IN (
                    'prepared','bytes_sending','accepted','outcome_unknown','completed','rejected'
                )),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS remote_task_grants (
                thread_id TEXT PRIMARY KEY REFERENCES task_bindings(thread_id) ON DELETE CASCADE,
                project_root TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                task_binding_epoch INTEGER NOT NULL,
                identity_binding_epoch INTEGER NOT NULL,
                service_fencing_token INTEGER NOT NULL DEFAULT 0,
                capabilities_json TEXT NOT NULL,
                capabilities_hash TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('active','revoked','drifted')),
                authorized_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS thread_profiles (
                thread_id TEXT PRIMARY KEY,
                profile_hash TEXT NOT NULL,
                profile_epoch INTEGER NOT NULL,
                displayed_hash TEXT,
                effective_hash TEXT,
                reconciliation_state TEXT NOT NULL CHECK(reconciliation_state IN (
                    'pending','matched','mismatch'
                )),
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS selection_confirmations (
                logical_message_id TEXT PRIMARY KEY,
                target_thread_id TEXT NOT NULL,
                binding_epoch INTEGER NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('pending','confirmed','indeterminate')),
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS path_identities (
                canonical_path TEXT PRIMARY KEY,
                final_path TEXT NOT NULL,
                volume_serial INTEGER NOT NULL,
                file_index TEXT NOT NULL,
                reparse_tag INTEGER NOT NULL,
                security_hash TEXT NOT NULL,
                nearest_existing_parent TEXT NOT NULL,
                captured_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pilot_samples (
                sample_id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at TEXT NOT NULL,
                final_confirmed INTEGER NOT NULL,
                final_undelivered INTEGER NOT NULL,
                delivery_indeterminate INTEGER NOT NULL,
                dispatch_unknown INTEGER NOT NULL,
                approval_unknown INTEGER NOT NULL,
                dead_letters INTEGER NOT NULL,
                latency_ms INTEGER
            );
            INSERT OR IGNORE INTO service_state(singleton,updated_at) VALUES(1,'1970-01-01T00:00:00Z');
            """
        )
        self._migrate_runtime_schema()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT value FROM runtime_metadata WHERE key='sink_mode'"
            ).fetchone()
            if existing is not None and existing[0] != sink_mode:
                raise ValueError("runtime database sink mode is immutable")
            connection.execute(
                "INSERT OR IGNORE INTO runtime_metadata(key,value) VALUES('sink_mode',?)",
                (sink_mode,),
            )
            connection.execute(
                "INSERT OR REPLACE INTO runtime_metadata(key,value) VALUES('runtime_schema_version',?)",
                (str(RUNTIME_SCHEMA_VERSION),),
            )
            connection.execute(
                "INSERT OR IGNORE INTO runtime_metadata(key,value) VALUES('lineage_id',?)",
                (json.dumps(uuid.uuid4().hex),),
            )

    def _migrate_runtime_schema(self) -> None:
        """Add newer routing and task-title fields to an existing database."""

        required = {
            "task_bindings": {
                "conversation_mode": "TEXT NOT NULL DEFAULT 'p2p'",
                "provider_thread_id": "TEXT",
                "task_title": "TEXT",
                "project_name": "TEXT",
                "anchor_title_hash": "TEXT",
                "pending_title_hash": "TEXT",
                "blocked_title_hash": "TEXT",
                "title_sync_error": "TEXT",
                "title_revision": "INTEGER NOT NULL DEFAULT 0",
                "lifecycle_state": "TEXT NOT NULL DEFAULT 'active' CHECK(lifecycle_state IN ('active','archived'))",
            },
            "provider_outbox": {
                "reply_in_thread": "INTEGER NOT NULL DEFAULT 0 CHECK(reply_in_thread IN (0,1))",
                "message_type": "TEXT NOT NULL DEFAULT 'text' CHECK(message_type IN ('text','image','interactive'))",
            },
            "identity_bindings": {
                "active_chat_id": "TEXT",
                "active_chat_type": "TEXT NOT NULL DEFAULT 'p2p'",
                "conversation_mode": "TEXT NOT NULL DEFAULT 'p2p'",
            },
            "message_ancestry": {
                "provider_thread_id": "TEXT",
            },
            "ingress_messages": {
                "provider_thread_id": "TEXT",
                "dispatch_not_before": "TEXT",
                "dispatch_attempt_count": "INTEGER NOT NULL DEFAULT 0",
                "last_dispatch_error": "TEXT",
            },
            "remote_task_grants": {
                "service_fencing_token": "INTEGER NOT NULL DEFAULT 0",
            },
            "dispatch_records": {
                "user_item_id": "TEXT",
                "submitted_text_hash": "TEXT",
                "has_attachments": "INTEGER NOT NULL DEFAULT 0 CHECK(has_attachments IN (0,1))",
            },
            "approval_actions": {
                "server_epoch": "TEXT NOT NULL DEFAULT ''",
                "connection_epoch": "TEXT NOT NULL DEFAULT ''",
                "session_id": "TEXT NOT NULL DEFAULT ''",
                "service_fencing_token": "INTEGER NOT NULL DEFAULT 0",
            },
        }
        statements: list[str] = []
        for table, columns in required.items():
            present = {
                str(row[1])
                for row in self.connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for name, declaration in columns.items():
                if name not in present:
                    statements.append(f"ALTER TABLE {table} ADD COLUMN {name} {declaration};")
        if statements:
            self.execute_schema_migration("\n".join(statements))
        self._migrate_approval_request_identity()
        self.connection.execute(
            "UPDATE identity_bindings SET active_chat_id=p2p_chat_id "
            "WHERE active_chat_id IS NULL OR active_chat_id=''"
        )
        self.connection.execute(
            "UPDATE task_bindings SET blocked_title_hash=pending_title_hash,"
            "title_sync_error=COALESCE((SELECT outbox.last_error_code FROM provider_outbox outbox "
            "WHERE outbox.thread_id=task_bindings.thread_id AND outbox.operation='anchor_title' "
            "AND outbox.item_id=task_bindings.pending_title_hash "
            "AND outbox.state IN ('permanent','delivery_indeterminate') "
            "ORDER BY outbox.outbox_id DESC LIMIT 1),'title_delivery_failed'),"
            "pending_title_hash=NULL WHERE pending_title_hash IS NOT NULL AND EXISTS("
            "SELECT 1 FROM provider_outbox outbox WHERE outbox.thread_id=task_bindings.thread_id "
            "AND outbox.operation='anchor_title' AND outbox.item_id=task_bindings.pending_title_hash "
            "AND outbox.state IN ('permanent','delivery_indeterminate'))"
        )

    def _migrate_approval_request_identity(self) -> None:
        """Scope App Server request ids to one connection generation.

        JSON-RPC ids restart from zero after an App Server reconnect.  Older
        databases made the wire id globally unique, so the first approval
        after a reconnect could crash the Broker.  Rebuild the table once and
        retain every historical action while making the actual identity
        ``(server_epoch, connection_epoch, server_request_id)``.
        """

        definition = self.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='approval_actions'"
        ).fetchone()
        if definition is None or "server_request_id TEXT UNIQUE NOT NULL" not in str(definition[0]):
            return
        self.execute_schema_migration(
            """
            ALTER TABLE approval_actions RENAME TO approval_actions_v10;
            CREATE TABLE approval_actions (
                token_hash TEXT PRIMARY KEY,
                approval_id TEXT UNIQUE NOT NULL,
                server_request_id TEXT NOT NULL,
                server_method TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                turn_id TEXT,
                tenant_key TEXT NOT NULL,
                app_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                card_message_id TEXT NOT NULL,
                operator_open_id TEXT NOT NULL,
                binding_epoch INTEGER NOT NULL,
                identity_binding_epoch INTEGER NOT NULL,
                kill_generation INTEGER NOT NULL,
                server_epoch TEXT NOT NULL,
                connection_epoch TEXT NOT NULL,
                session_id TEXT NOT NULL,
                service_fencing_token INTEGER NOT NULL,
                decision_map_json TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                consumed_decision TEXT,
                consumed_at TEXT,
                UNIQUE(server_epoch,connection_epoch,server_request_id)
            );
            INSERT INTO approval_actions(
                token_hash,approval_id,server_request_id,server_method,thread_id,turn_id,
                tenant_key,app_id,chat_id,card_message_id,operator_open_id,binding_epoch,
                identity_binding_epoch,kill_generation,server_epoch,connection_epoch,
                session_id,service_fencing_token,decision_map_json,expires_at,
                consumed_decision,consumed_at
            )
            SELECT token_hash,approval_id,server_request_id,server_method,thread_id,turn_id,
                tenant_key,app_id,chat_id,card_message_id,operator_open_id,binding_epoch,
                identity_binding_epoch,kill_generation,server_epoch,connection_epoch,
                session_id,service_fencing_token,decision_map_json,expires_at,
                consumed_decision,consumed_at
            FROM approval_actions_v10;
            DROP TABLE approval_actions_v10;
            """
        )

    @property
    def sink_mode(self) -> str:
        row = self.connection.execute(
            "SELECT value FROM runtime_metadata WHERE key='sink_mode'"
        ).fetchone()
        if row is None:
            raise RuntimeError("runtime storage is not initialized")
        return str(row[0])

    def ensure_writable_sink(self) -> None:
        if self.sink_mode == "shadow_only":
            raise PermissionError("shadow_only storage cannot create provider work")

    def upsert_runtime_metadata(self, key: str, value: Any) -> None:
        self.connection.execute(
            "INSERT INTO runtime_metadata(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, separators=(",", ":"))),
        )

    def enqueue_provider_message(
        self,
        *,
        logical_message_id: str,
        operation: str,
        message_type: str = "text",
        endpoint_name: str,
        stable_uuid: str,
        marker: str,
        body_json: str,
        body_hash: str,
        priority: int,
        thread_id: str | None = None,
        turn_id: str | None = None,
        item_id: str | None = None,
        target_message_id: str | None = None,
        reply_in_thread: bool = False,
    ) -> int:
        self.ensure_writable_sink()
        if message_type not in {"text", "image", "interactive"}:
            raise ValueError("invalid provider message type")
        now = utc_now()
        result = self.connection.execute(
            "INSERT INTO provider_outbox("
            "logical_message_id,thread_id,turn_id,item_id,operation,message_type,endpoint_name,target_message_id,"
            "reply_in_thread,stable_uuid,marker,body_json,body_hash,priority,state,next_attempt_at,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                logical_message_id,
                thread_id,
                turn_id,
                item_id,
                operation,
                message_type,
                endpoint_name,
                target_message_id,
                int(reply_in_thread),
                stable_uuid,
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
        return int(result.lastrowid)

    def attach_outbound_image(
        self,
        *,
        outbox_id: int,
        source_path: str,
        file_name: str,
        mime_type: str,
        content: bytes,
        content_hash: str,
    ) -> None:
        if not content or len(content) > 10 * 1024 * 1024:
            raise ValueError("image payload must contain at most 10 MiB")
        row = self.connection.execute(
            "SELECT message_type,state FROM provider_outbox WHERE outbox_id=?", (outbox_id,)
        ).fetchone()
        if row is None or row["message_type"] != "image" or row["state"] != "pending":
            raise InvalidTransition("image payload requires a pending image outbox row")
        self.connection.execute(
            "INSERT INTO outbound_images(outbox_id,source_path,file_name,mime_type,content,"
            "content_hash,updated_at) VALUES(?,?,?,?,?,?,?)",
            (
                outbox_id,
                source_path,
                file_name,
                mime_type,
                content,
                content_hash,
                utc_now(),
            ),
        )

    def image_payload_for_lease(self, outbox_id: int, instance_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT image.* FROM outbound_images image JOIN provider_outbox outbox "
            "ON outbox.outbox_id=image.outbox_id WHERE image.outbox_id=? "
            "AND outbox.state='leased' AND outbox.lease_owner=? AND outbox.message_type='image'",
            (outbox_id, instance_id),
        ).fetchone()
        if row is None:
            raise InvalidTransition("leased image payload is missing")
        if row["image_key"] is None:
            self.connection.execute(
                "UPDATE outbound_images SET upload_attempt_count=upload_attempt_count+1,updated_at=? "
                "WHERE outbox_id=?",
                (utc_now(), outbox_id),
            )
        return row

    def stage_uploaded_image(
        self,
        outbox_id: int,
        instance_id: str,
        image_key: str,
    ) -> None:
        if not image_key:
            raise ValueError("image key must not be empty")
        body_json = json.dumps(
            {"image_key": image_key}, ensure_ascii=False, separators=(",", ":")
        )
        from hashlib import sha256

        with self.transaction() as connection:
            result = connection.execute(
                "UPDATE outbound_images SET image_key=?,updated_at=? WHERE outbox_id=? "
                "AND image_key IS NULL",
                (image_key, utc_now(), outbox_id),
            )
            if result.rowcount != 1:
                raise InvalidTransition("uploaded image key lost compare-and-swap")
            outbox = connection.execute(
                "UPDATE provider_outbox SET body_json=?,body_hash=?,state='retryable',"
                "next_attempt_at=?,lease_owner=NULL,lease_expires_at=NULL,last_error_code=NULL,updated_at=? "
                "WHERE outbox_id=? AND state='leased' AND lease_owner=? AND message_type='image'",
                (
                    body_json,
                    sha256(body_json.encode("utf-8")).hexdigest(),
                    utc_now(),
                    utc_now(),
                    outbox_id,
                    instance_id,
                ),
            )
            if outbox.rowcount != 1:
                raise InvalidTransition("image upload staging lost compare-and-swap")

    def rich_image_payload_for_lease(
        self, outbox_id: int, instance_id: str
    ) -> sqlite3.Row | None:
        """Return the next not-yet-uploaded image for one leased rich post."""

        row = self.connection.execute(
            "SELECT image.* FROM outbound_rich_images image JOIN provider_outbox outbox "
            "ON outbox.outbox_id=image.outbox_id WHERE image.outbox_id=? "
            "AND image.image_key IS NULL AND outbox.state='leased' "
            "AND outbox.lease_owner=? ORDER BY image.ordinal LIMIT 1",
            (outbox_id, instance_id),
        ).fetchone()
        if row is not None:
            self.connection.execute(
                "UPDATE outbound_rich_images SET upload_attempt_count=upload_attempt_count+1,"
                "updated_at=? WHERE outbox_id=? AND ordinal=? AND image_key IS NULL",
                (utc_now(), outbox_id, row["ordinal"]),
            )
        return row

    def stage_uploaded_rich_image(
        self,
        outbox_id: int,
        instance_id: str,
        ordinal: int,
        image_key: str,
    ) -> None:
        """Persist one uploaded key and release the rich post for its next phase."""

        if not image_key:
            raise ValueError("image key must not be empty")
        with self.transaction() as connection:
            updated = connection.execute(
                "UPDATE outbound_rich_images SET image_key=?,updated_at=? "
                "WHERE outbox_id=? AND ordinal=? AND image_key IS NULL",
                (image_key, utc_now(), outbox_id, ordinal),
            )
            if updated.rowcount != 1:
                raise InvalidTransition("rich image key lost compare-and-swap")
            outbox = connection.execute(
                "UPDATE provider_outbox SET state='retryable',next_attempt_at=?,"
                "lease_owner=NULL,lease_expires_at=NULL,last_error_code=NULL,updated_at=? "
                "WHERE outbox_id=? AND state='leased' AND lease_owner=?",
                (utc_now(), utc_now(), outbox_id, instance_id),
            )
            if outbox.rowcount != 1:
                raise InvalidTransition("rich image upload staging lost compare-and-swap")

    def lease_outbox(self, instance_id: str, lease_until: str) -> sqlite3.Row | None:
        self.ensure_writable_sink()
        now = utc_now()
        with self.transaction() as connection:
            # A worker can exit after acquiring a lease but before recording the
            # provider result. Treat an expired lease as an unknown send outcome,
            # never as a fresh retry: reconciliation will first search provider
            # history or reuse the same UUID while its idempotency window is open.
            connection.execute(
                "UPDATE provider_outbox SET state='unknown',lease_owner=NULL,"
                "lease_expires_at=NULL,last_error_code='lease_expired',updated_at=? "
                "WHERE state='leased' AND lease_expires_at IS NOT NULL AND lease_expires_at<=?",
                (now, now),
            )
            row = connection.execute(
                "SELECT * FROM provider_outbox WHERE "
                "state IN ('pending','retryable') AND next_attempt_at<=? "
                "AND NOT EXISTS (SELECT 1 FROM provider_outbox earlier WHERE "
                "earlier.turn_id=provider_outbox.turn_id AND earlier.turn_id IS NOT NULL "
                "AND earlier.outbox_id<provider_outbox.outbox_id AND earlier.state NOT IN "
                "('confirmed','permanent','delivery_indeterminate','retention_policy_blocked','final_undelivered')) "
                "ORDER BY priority DESC,outbox_id LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                return None
            result = connection.execute(
                "UPDATE provider_outbox SET state='leased',lease_owner=?,lease_expires_at=?,"
                "attempt_count=attempt_count+1,first_attempt_at=COALESCE(first_attempt_at,?),updated_at=? "
                "WHERE outbox_id=? AND state IN ('pending','retryable')",
                (instance_id, lease_until, now, now, row["outbox_id"]),
            )
            if result.rowcount != 1:
                raise InvalidTransition("outbox lease lost compare-and-swap")
            return connection.execute(
                "SELECT * FROM provider_outbox WHERE outbox_id=?", (row["outbox_id"],)
            ).fetchone()

    def finish_outbox(
        self,
        outbox_id: int,
        instance_id: str,
        *,
        state: str,
        provider_message_id: str | None = None,
        error_code: str | None = None,
        next_attempt_at: str | None = None,
    ) -> None:
        if state not in {
            "confirmed",
            "retryable",
            "permanent",
            "unknown",
            "delivery_indeterminate",
            "retention_policy_blocked",
            "final_undelivered",
        }:
            raise ValueError("invalid outbox terminal/retry state")
        result = self.connection.execute(
            "UPDATE provider_outbox SET state=?,provider_message_id=COALESCE(?,provider_message_id),"
            "last_error_code=?,next_attempt_at=COALESCE(?,next_attempt_at),lease_owner=NULL,"
            "lease_expires_at=NULL,updated_at=? WHERE outbox_id=? AND state='leased' AND lease_owner=?",
            (
                state,
                provider_message_id,
                error_code,
                next_attempt_at,
                utc_now(),
                outbox_id,
                instance_id,
            ),
        )
        if result.rowcount != 1:
            raise InvalidTransition("outbox completion lost compare-and-swap")

    def confirm_task_title_update(
        self,
        outbox_id: int,
        instance_id: str,
        *,
        thread_id: str,
        title_hash: str,
    ) -> None:
        """Atomically confirm provider delivery and the local title projection."""

        with self.transaction() as connection:
            leased = connection.execute(
                "SELECT marker FROM provider_outbox WHERE outbox_id=? AND state='leased' "
                "AND lease_owner=? AND operation='anchor_title' AND thread_id=? AND item_id=?",
                (outbox_id, instance_id, thread_id, title_hash),
            ).fetchone()
            if leased is None:
                raise InvalidTransition("task title outbox confirmation lost compare-and-swap")
            outbox = connection.execute(
                "UPDATE provider_outbox SET state='confirmed',last_error_code=NULL,lease_owner=NULL,"
                "lease_expires_at=NULL,updated_at=? WHERE outbox_id=? AND state='leased' "
                "AND lease_owner=? AND operation='anchor_title' AND thread_id=? AND item_id=?",
                (utc_now(), outbox_id, instance_id, thread_id, title_hash),
            )
            if outbox.rowcount != 1:
                raise InvalidTransition("task title outbox confirmation lost compare-and-swap")
            binding = connection.execute(
                "UPDATE task_bindings SET anchor_title_hash=?,anchor_marker=?,"
                "pending_title_hash=NULL,blocked_title_hash=NULL,title_sync_error=NULL,updated_at=? "
                "WHERE thread_id=? AND pending_title_hash=?",
                (title_hash, leased["marker"], utc_now(), thread_id, title_hash),
            )
            if binding.rowcount != 1:
                raise InvalidTransition("task title binding confirmation lost compare-and-swap")

    def fail_task_title_update(
        self,
        outbox_id: int,
        instance_id: str,
        *,
        thread_id: str,
        title_hash: str,
        state: str,
        error_code: str,
    ) -> None:
        """Persist a non-retryable or indeterminate title projection outcome."""

        if state not in {"permanent", "delivery_indeterminate"}:
            raise ValueError("invalid task title failure state")
        with self.transaction() as connection:
            outbox = connection.execute(
                "UPDATE provider_outbox SET state=?,last_error_code=?,lease_owner=NULL,"
                "lease_expires_at=NULL,updated_at=? WHERE outbox_id=? AND state='leased' "
                "AND lease_owner=? AND operation='anchor_title' AND thread_id=? AND item_id=?",
                (
                    state,
                    error_code,
                    utc_now(),
                    outbox_id,
                    instance_id,
                    thread_id,
                    title_hash,
                ),
            )
            if outbox.rowcount != 1:
                raise InvalidTransition("task title failure lost outbox compare-and-swap")
            binding = connection.execute(
                "UPDATE task_bindings SET pending_title_hash=NULL,blocked_title_hash=?,"
                "title_sync_error=?,updated_at=? WHERE thread_id=? AND pending_title_hash=?",
                (title_hash, error_code, utc_now(), thread_id, title_hash),
            )
            if binding.rowcount != 1:
                raise InvalidTransition("task title failure lost binding compare-and-swap")

    @contextmanager
    def immediate(self) -> Iterator[sqlite3.Connection]:
        with self.transaction() as connection:
            yield connection
