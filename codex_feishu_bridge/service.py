"""Foreground service orchestration across the authorized runtime modes."""

from __future__ import annotations

import json
import logging
import signal
import threading
import time
import uuid
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from typing import Any

from .codex.app_server_client import AppServerProtocol, ProtocolError, StdioAppServer
from .codex.approval_gateway import ApprovalGateway
from .codex.compatibility import CompatibilityMatrix
from .codex.connection import AppServerConnection
from .codex.controller import CodexController, DispatchBusy, DispatchError
from .codex.cli_dispatch import CodexCliDispatcher
from .codex.cli_gateway import CodexCliGateway
from .codex.desktop_dispatch import DesktopCodexDispatcher
from .codex.desktop_relay_dispatch import DesktopRelayCodexDispatcher
from .codex.desktop_gateway import CodexDesktopGateway, DesktopGatewayError
from .codex.execution_profile import ApprovalPolicy, ExecutionProfile, SandboxType
from .codex.rollout_observer import IncrementalRolloutReader
from .codex.shadow_observer import ShadowObserver
from .codex.project_catalog import CodexProjectCatalog
from .codex.state_discovery import discover_rollouts
from .codex.thread_titles import CodexThreadTitleReader
from .commands import parse_command
from .controls import ControlError, ProfileController
from .diagnostics.health import capture_health, write_status_atomic
from .diagnostics.logging import configure_logging
from .feishu.cleanup import CleanupWorker, schedule_legacy_marker_cleanup
from .feishu.client import FeishuClient
from .feishu.contracts import load_tenant_contract
from .feishu.events import EventHandlers
from .feishu.inbound import IngressRouter
from .feishu.inbound_attachments import materialize_attachment
from .feishu.long_connection import FeishuLongConnection
from .feishu.outbound import (
    OutboundPipeline,
    OutboxWorker,
    stable_uuid,
    suppress_queued_internal_user_notifications,
)
from .feishu.provisioning import ProvisioningPreflight
from .feishu.project_groups import ProjectGroupManager
from .feishu.receipts import queue_ingress_status
from .feishu.reconciliation import SendReconciler
from .feishu.tasks import TaskAnchorManager
from .feishu.user_cli import LarkCliUnavailable, LarkCliUserSender
from .models import OwnershipState, RolloutBatch
from .runtime_config import RuntimeConfig, RuntimeMode
from .runtime_storage import RuntimeStorage, utc_now
from .security.approvals import ApprovalBroker
from .security.audit import AuditChain
from .security.emergency import EmergencyController
from .security.single_instance import PrivateMutex
from .security.dpapi import protect_current_user, unprotect_current_user
from .security.jcs import canonicalize
from .operations.update_gate import UpdateGate
from .operations.pilot import PilotTracker
from hashlib import sha256


class ServiceError(RuntimeError):
    pass


_INGRESS_DELIVERY_TIMEOUT_SECONDS = 60


class _BorrowedTitleTransport:
    """Expose the shared App Server connection to the title reader.

    ``close`` is intentionally a no-op: the service owns the underlying
    connection and shuts it down after quiescing all bridge activity.
    """

    def __init__(self, connection: AppServerConnection) -> None:
        self.connection = connection

    def handshake(self, timeout: float = 10.0) -> object:
        return None

    def request(
        self,
        method: str,
        params: dict[str, object] | None = None,
        *,
        timeout: float = 30.0,
        on_server_request: object | None = None,
    ) -> dict[str, object]:
        return self.connection.request(method, params, timeout=timeout)

    def close(self) -> None:
        return None


def checkpoint_unseen_source(
    storage: RuntimeStorage,
    reader: IncrementalRolloutReader,
    source: Any,
) -> bool:
    """Start a newly opted-in source at its current complete-record boundary.

    Opt-in is prospective: visible records written before activation must not be
    replayed into the provider. A partial trailing record is deliberately not
    checkpointed and will be handled after it becomes complete.
    """
    if storage.cursor_for(str(source.path)) is not None:
        return False
    initial = reader.read(
        source.path,
        expected_thread_id=source.thread_id,
    )
    storage.store_rollout_batch(
        RolloutBatch(
            events=(),
            cursor=initial.cursor,
            ignored_records=initial.ignored_records + len(initial.events),
        )
    )
    return True


class BridgeService:
    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        self.stop_event = threading.Event()
        self.instance_id = str(uuid.uuid4())
        self.server_epoch = str(uuid.uuid4())
        self.connection_epoch = str(uuid.uuid4())
        self.logger = configure_logging(
            config.workspace_root / ".runtime" / "logs" / "bridge.jsonl"
        )
        database = (
            config.shadow_database_path
            if config.mode is RuntimeMode.SHADOW
            else config.database_path
        )
        self.storage = RuntimeStorage(database)
        sink_mode = {
            RuntimeMode.OFFLINE: "shadow_only",
            RuntimeMode.SHADOW: "shadow_only",
            RuntimeMode.OUTBOUND: "outbound",
            RuntimeMode.APPROVALS: "control",
            RuntimeMode.INBOUND: "control",
            RuntimeMode.CONTROLS: "control",
            RuntimeMode.PILOT: "pilot",
        }[config.mode]
        self.storage.initialize_runtime(sink_mode=sink_mode)
        self.client: FeishuClient | None = None
        self.codex_connection: AppServerConnection | None = None
        self.controller: CodexController | None = None
        self.cli_gateway: CodexCliGateway | None = None
        self.cli_dispatcher: CodexCliDispatcher | None = None
        self.desktop_gateway: CodexDesktopGateway | None = None
        self.desktop_dispatcher: DesktopCodexDispatcher | None = None
        self.desktop_relay_dispatcher: DesktopRelayCodexDispatcher | None = None
        self.gateway: ApprovalGateway | None = None
        self.ingress: IngressRouter | None = None
        self.outbox_worker: OutboxWorker | None = None
        self.cleanup_worker: CleanupWorker | None = None
        self.long_connection: FeishuLongConnection | None = None
        self.long_connection_thread: threading.Thread | None = None
        self.long_connection_disconnected_since: float | None = None
        self.audit: AuditChain | None = None
        self.pilot: PilotTracker | None = None
        self.provisioning: ProvisioningPreflight | None = None
        self.project_catalog: CodexProjectCatalog | None = None
        self.project_groups: ProjectGroupManager | None = None
        self.title_reader: CodexThreadTitleReader | None = None
        self._active_rollout_turns: dict[str, frozenset[str]] = {}

    def _acquire_service_fence(self) -> int:
        with self.storage.immediate() as connection:
            restore = connection.execute(
                "SELECT value FROM runtime_metadata WHERE key='restore_mode'"
            ).fetchone()
            if restore is not None and "reconciliation_only" in str(restore[0]):
                raise ServiceError("restored database is reconciliation-only and has not been promoted")
            row = connection.execute(
                "SELECT process_state,fencing_token,instance_id FROM service_state WHERE singleton=1"
            ).fetchone()
            stale_state = row["process_state"] not in {"stopped", "reconciliation_only"}
            fencing = int(row["fencing_token"]) + 1
            connection.execute(
                "UPDATE service_state SET instance_id=?,fencing_token=?,"
                "kill_generation=kill_generation+?,process_state='starting',updated_at=? "
                "WHERE singleton=1 AND fencing_token=?",
                (
                    self.instance_id,
                    fencing,
                    int(stale_state),
                    utc_now(),
                    row["fencing_token"],
                ),
            )
            if stale_state:
                connection.execute(
                    "INSERT INTO runtime_metadata(key,value) VALUES('last_stale_fence_recovery',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (
                        json.dumps(
                            {
                                "previous_instance_id": row["instance_id"],
                                "previous_state": row["process_state"],
                                "recovered_at": utc_now(),
                            },
                            separators=(",", ":"),
                        ),
                    ),
                )
            connection.execute(
                "UPDATE remote_task_grants SET state='revoked',updated_at=? WHERE state='active'",
                (utc_now(),),
            )
            return fencing

    def _configure_provider(self) -> None:
        binding = self.config.feishu
        if binding is None:
            raise ServiceError("provider mode requires a binding")
        contract = load_tenant_contract(binding.endpoint_contract)
        if contract.tenant_key != binding.tenant_key or contract.app_id != binding.app_id:
            raise ServiceError("runtime binding differs from the frozen tenant contract")
        self.storage.connection.execute(
            "INSERT OR IGNORE INTO endpoint_contracts(contract_hash,app_id,published_version,contract_json,activated_at) "
            "VALUES(?,?,?,?,?)",
            (
                contract.contract_hash,
                contract.app_id,
                contract.published_version,
                contract.canonical_json.decode("utf-8"),
                utc_now(),
            ),
        )
        self.client = FeishuClient(
            contract=contract,
            app_id=binding.app_id,
            credential_target=binding.credential_target,
        )
        self.provisioning = ProvisioningPreflight(
            self.storage, self.client, binding, contract
        )
        preflight = self.provisioning.run(live=True, remote=self.config.remote)
        if not preflight.passed:
            hard_failures = {
                failure
                for failure in preflight.failures
                if failure in {"tenant_key_mismatch", "app_id_mismatch"}
            }
            if hard_failures:
                raise ServiceError(
                    "tenant/app identity mismatch: " + ",".join(sorted(hard_failures))
                )
            self.logger.warning(
                "tenant_provisioning_preflight_warning",
                extra={"fields": {"failures": list(preflight.failures)}},
            )
        gate = UpdateGate.load(self.config.generated_schema_root / "baseline.json")
        gate.require_executable(self.config.codex_executable)
        gate.require_schemas(self.config.generated_schema_root)
        if not self.config.remote.enabled:
            self.title_reader = CodexThreadTitleReader(
                executable=self.config.codex_executable,
                codex_home=self.config.codex_home,
                stable_schema_root=self.config.generated_schema_root / "stable",
            )
            self.title_reader.start()
        automation = self.config.codex_projects
        if automation is not None and automation.enabled:
            create_contract = contract.endpoint("create_chat")
            if create_contract.uuid_window_seconds is None:
                raise ServiceError("create_chat endpoint lacks an idempotency window")
            self.project_catalog = CodexProjectCatalog(self.config.codex_home)
            projects = {project.project_id: project for project in self.project_catalog.projects()}
            primary = projects.get(automation.primary_project_id)
            if primary is None:
                raise ServiceError("primary project is absent from current Codex projects")
            self.project_groups = ProjectGroupManager(
                self.storage,
                self.client,
                owner_open_id=binding.owner_open_id,
                uuid_window_seconds=create_contract.uuid_window_seconds,
            )
            self.project_groups.register_existing(
                primary,
                chat_id=binding.target_chat_id,
                last_activity_ms=automation.activity_after_ms,
            )
        user_message_sender = None
        if binding.user_message_identity == "lark_cli_user":
            assert binding.lark_cli_profile is not None
            try:
                user_message_sender = LarkCliUserSender.discover(
                    profile=binding.lark_cli_profile
                )
                user_message_sender.verify_ready()
            except LarkCliUnavailable as exc:
                # User-identity mirroring is optional. A stale or temporarily
                # unavailable CLI authorization must not stop bot delivery.
                user_message_sender = None
                self.logger.warning(
                    "lark_cli_user_identity_unavailable",
                    extra={"fields": {"error": type(exc).__name__}},
                )
                self.storage.upsert_runtime_metadata(
                    "user_cli_startup_fallback",
                    {"at": utc_now(), "code": type(exc).__name__},
                )
        suppressed_notifications = suppress_queued_internal_user_notifications(self.storage)
        if suppressed_notifications:
            self.logger.info(
                "internal_user_notifications_suppressed",
                extra={"fields": {"count": suppressed_notifications}},
            )
        self.outbox_worker = OutboxWorker(
            self.storage,
            self.client,
            self.instance_id,
            user_message_sender=user_message_sender,
            owner_display_name=binding.owner_display_name,
        )
        self.cleanup_worker = CleanupWorker(self.storage, self.client)
        cleanup = schedule_legacy_marker_cleanup(self.storage)
        if cleanup.queued:
            self.logger.info(
                "legacy_ui_marker_cleanup_queued",
                extra={"fields": {"scanned": cleanup.scanned, "queued": cleanup.queued}},
            )

    def _configure_codex(self) -> None:
        schema_root = self.config.generated_schema_root
        approval_key = _load_or_create_local_key(
            self.config.workspace_root / ".runtime" / "approval.key"
        )
        gate = UpdateGate.load(schema_root / "baseline.json")
        gate.require_executable(self.config.codex_executable)
        gate.require_schemas(schema_root)
        matrix = CompatibilityMatrix.load(schema_root / "compatibility-matrix.json")
        protocol = AppServerProtocol(
            schema_root / "stable",
            experimental_schema_root=schema_root / "experimental",
            experimental_api=True,
            compatibility_matrix=matrix,
            approved_experimental_client_methods=frozenset(),
            approved_experimental_server_methods=frozenset(),
            approved_experimental_server_fields=frozenset(
                {
                    ("item/commandExecution/requestApproval", "availableDecisions"),
                    ("item/commandExecution/requestApproval", "additionalPermissions"),
                }
            ),
        )
        transport = StdioAppServer(
            self.config.codex_executable,
            self.config.codex_home,
            protocol,
        )
        connection = AppServerConnection(transport)
        connection.start()
        self.codex_connection = connection
        if self.config.remote.enabled:
            # Title reads share the already-open App Server connection.
            self.title_reader = CodexThreadTitleReader(
                executable=self.config.codex_executable,
                codex_home=self.config.codex_home,
                stable_schema_root=schema_root / "stable",
                transport_factory=lambda: _BorrowedTitleTransport(connection),
            )
            self.title_reader.start()
        self.controller = CodexController(
            self.storage,
            connection,
            schema_root=schema_root / "experimental",
            server_epoch=self.server_epoch,
            connection_epoch=self.connection_epoch,
        )
        binding = self.config.feishu
        assert binding is not None
        self.audit = AuditChain(
            self.storage, sha256(b"audit-chain\x00" + approval_key).digest()
        )
        self.gateway = ApprovalGateway(
            self.storage,
            connection,
            ApprovalBroker(self.storage, approval_key, self.audit),
            binding,
            server_epoch=self.server_epoch,
            connection_epoch=self.connection_epoch,
            session_id=self.instance_id,
            auto_approve=self.config.remote.auto_approve,
        )
        if getattr(self.config.remote, "uses_cli", False):
            self.cli_gateway = CodexCliGateway(
                self.config.codex_executable,
                self.config.codex_home,
            )
            self.cli_dispatcher = CodexCliDispatcher(
                self.storage,
                self.cli_gateway,
                codex_home=self.config.codex_home,
                authorize=self.controller.require_dispatchable,
                server_epoch=self.server_epoch,
                connection_epoch=self.connection_epoch,
            )
        elif self.config.remote.uses_desktop or getattr(
            self.config.remote, "uses_desktop_relay", False
        ):
            if getattr(self.config.remote, "uses_desktop_relay", False):
                relay_thread_id = self.config.remote.desktop_relay_thread_id
                assert relay_thread_id is not None
                self.desktop_gateway = CodexDesktopGateway(
                    background_only=True,
                )
                self.desktop_relay_dispatcher = DesktopRelayCodexDispatcher(
                    self.storage,
                    self.desktop_gateway,
                    codex_home=self.config.codex_home,
                    authorize=self.controller.require_dispatchable,
                    server_epoch=self.server_epoch,
                    connection_epoch=self.connection_epoch,
                    relay_thread_id=relay_thread_id,
                )
            else:
                self.desktop_gateway = CodexDesktopGateway()
                self.desktop_dispatcher = DesktopCodexDispatcher(
                    self.storage,
                    self.desktop_gateway,
                    codex_home=self.config.codex_home,
                    authorize=self.controller.require_dispatchable,
                    server_epoch=self.server_epoch,
                    connection_epoch=self.connection_epoch,
                )

    def _configure_inbound(self) -> None:
        binding = self.config.feishu
        assert binding is not None and self.gateway is not None
        if self.config.remote.receives_messages or self.config.allows(RuntimeMode.INBOUND):
            self.ingress = IngressRouter(self.storage, binding, self.config.remote)
        handlers = EventHandlers(self.ingress, self.gateway.handle_card_action)
        self.long_connection = FeishuLongConnection(
            app_id=binding.app_id,
            credential_target=binding.credential_target,
            handlers=handlers,
        )
        self.long_connection_thread = threading.Thread(
            target=self.long_connection.start,
            name="feishu-long-connection",
            daemon=True,
        )
        self.long_connection_thread.start()
        deadline = time.monotonic() + 30.0
        while (
            self.long_connection_thread.is_alive()
            and self.long_connection.connection_state() != "connected"
            and time.monotonic() < deadline
        ):
            time.sleep(0.1)
        if not self.long_connection_thread.is_alive():
            raise ServiceError("Feishu long-connection thread exited during startup")
        if self.long_connection.connection_state() != "connected":
            raise ServiceError("Feishu long connection was not established before startup timeout")

    def run(self) -> None:
        database_identity = sha256(
            str(self.storage.path).casefold().encode("utf-8")
        ).hexdigest()[:16]
        with ExitStack() as mutexes:
            mutexes.enter_context(PrivateMutex("owner-bridge-" + database_identity))
            if self.config.remote.needs_control_plane or self.config.allows(RuntimeMode.APPROVALS):
                mutexes.enter_context(PrivateMutex("owner-control"))
            self._install_signal_handlers()
            self._acquire_service_fence()
            try:
                if self.config.mode in {RuntimeMode.OFFLINE, RuntimeMode.SHADOW}:
                    self.storage.connection.execute(
                        "UPDATE service_state SET process_state='running',updated_at=? WHERE singleton=1",
                        (utc_now(),),
                    )
                    self._run_shadow()
                    return
                self._configure_provider()
                if self.config.remote.needs_control_plane or self.config.allows(RuntimeMode.APPROVALS):
                    self._configure_codex()
                    self._configure_inbound()
                self.storage.connection.execute(
                    "UPDATE service_state SET process_state='running',updated_at=? WHERE singleton=1",
                    (utc_now(),),
                )
                if self.audit is not None:
                    self.audit.append(
                        {"event": "service_running", "instance_id": self.instance_id}
                    )
                if self.config.mode is RuntimeMode.PILOT:
                    self.pilot = PilotTracker(self.storage)
                    self.pilot.start()
                self._run_main_loop()
            finally:
                self._shutdown()

    def _run_shadow(self) -> None:
        observer = ShadowObserver(self.storage)
        while not self.stop_event.is_set():
            self._monitor_long_connection()
            result = observer.observe_once(
                codex_home=self.config.codex_home,
                project_allowlist=self.config.project_allowlist,
                thread_allowlist=self.config.thread_allowlist,
            )
            self.logger.info(
                "shadow_observation",
                extra={"fields": {"sources": result.sources_seen, "inserted": result.records_inserted}},
            )
            self._write_status()
            self.stop_event.wait(1.0)

    def _run_main_loop(self) -> None:
        reader = IncrementalRolloutReader()
        pipeline = OutboundPipeline(
            self.storage,
            owner_display_name=(
                self.config.feishu.owner_display_name
                if self.config.feishu is not None
                else "用户"
            ),
            user_messages_as_user=(
                self.config.feishu is not None
                and self.config.feishu.user_message_identity == "lark_cli_user"
            ),
        )
        last_preflight = time.monotonic()
        last_pilot_sample = 0.0
        last_project_refresh = 0.0
        automatic_sources: tuple[Any, ...] = ()
        archived_thread_ids: frozenset[str] = frozenset()
        self._suppress_internal_task_bindings()
        while not self.stop_event.is_set():
            self._monitor_long_connection()
            static_sources = discover_rollouts(
                self.config.codex_home,
                project_allowlist=self.config.project_allowlist,
                thread_allowlist=self.config.thread_allowlist,
            )
            static_sources = tuple(
                source
                for source in static_sources
                if source.thread_id not in self.config.internal_thread_ids
            )
            now = time.monotonic()
            if (
                self.project_catalog is not None
                and self.project_groups is not None
                and now - last_project_refresh >= 2.0
            ):
                last_project_refresh = now
                try:
                    bound_ids = {
                        str(row[0])
                        for row in self.storage.connection.execute(
                            "SELECT thread_id FROM task_bindings"
                        ).fetchall()
                    }
                    watched_ids = (
                        bound_ids | {source.thread_id for source in static_sources}
                    ) - self.config.internal_thread_ids
                    archived_thread_ids = self.project_catalog.archived_thread_ids(watched_ids)
                    self._reconcile_archived_task_bindings(archived_thread_ids)
                    candidates = self.project_catalog.active_rollouts(
                        activity_after_ms=self.config.codex_projects.activity_after_ms
                    )
                    candidates = tuple(
                        source
                        for source in candidates
                        if source.thread_id not in self.config.internal_thread_ids
                    )
                    projects = {
                        project.project_id: project for project in self.project_catalog.projects()
                    }
                    activity: dict[str, int] = {}
                    for source in candidates:
                        assert source.project_id is not None
                        activity[source.project_id] = max(
                            activity.get(source.project_id, 0), int(source.activity_ms or 0)
                        )
                    active_chats: dict[str, str] = {}
                    for project_id, activity_ms in activity.items():
                        result = self.project_groups.ensure(
                            projects[project_id], last_activity_ms=activity_ms
                        )
                        if result.state == "active" and result.chat_id:
                            active_chats[project_id] = result.chat_id
                        elif result.state in {"failed", "outcome_unknown"}:
                            self.logger.error(
                                "project_group_not_active",
                                extra={"fields": {"project_id": project_id, "state": result.state}},
                            )
                    automatic_sources = tuple(
                        source
                        for source in candidates
                        if source.project_id in active_chats
                    )
                except Exception as exc:
                    # The static primary allowlist keeps working; automatic
                    # enrollment never guesses through partial desktop state.
                    automatic_sources = ()
                    self.logger.error(
                        "codex_project_discovery_failed",
                        extra={"fields": {"error": type(exc).__name__}},
                    )
            merged = {(source.thread_id, str(source.path)): source for source in static_sources}
            merged.update(
                {(source.thread_id, str(source.path)): source for source in automatic_sources}
            )
            discovered_sources = tuple(
                sorted(
                    (source for source in merged.values() if source.thread_id not in archived_thread_ids),
                    key=lambda item: (item.modified_ns, str(item.path)),
                )
            )
            if self.title_reader is not None:
                for source in discovered_sources:
                    self.title_reader.request_title(source.thread_id, source.project_root)
                sources = tuple(
                    replace(source, task_title=title)
                    if (title := self.title_reader.title_for(source.thread_id, source.project_root))
                    else source
                    for source in discovered_sources
                )
            else:
                sources = discovered_sources
            self._ensure_task_bindings(sources)
            active_rollout_turns: dict[str, set[str]] = {}
            for source in sources:
                task = self.storage.connection.execute(
                    "SELECT anchor_state FROM task_bindings WHERE thread_id=?",
                    (source.thread_id,),
                ).fetchone()
                if task is None or task["anchor_state"] != "confirmed":
                    continue
                cursor = self.storage.cursor_for(str(source.path))
                if cursor is None:
                    checkpoint_unseen_source(self.storage, reader, source)
                    active_rollout_turns.setdefault(source.thread_id, set()).update(
                        reader.active_turn_ids(source.path)
                    )
                    self.logger.info(
                        "source_activation_checkpoint",
                        extra={"fields": {"thread_id": source.thread_id}},
                    )
                    continue
                batch = reader.read(
                    source.path, cursor, expected_thread_id=source.thread_id
                )
                active_rollout_turns.setdefault(source.thread_id, set()).update(
                    batch.active_turn_ids
                )
                if batch.cursor != cursor:
                    pipeline.ingest_rollout_batch(batch)
            self._active_rollout_turns = {
                thread_id: frozenset(turn_ids)
                for thread_id, turn_ids in active_rollout_turns.items()
            }
            # Publish liveness before provider delivery. A cold start can
            # discover a large durable backlog, and twenty rate-limited sends
            # may take longer than the supervisor's stale-health threshold.
            self._write_status()
            self._drain_outbox()
            if self.client is not None:
                reconciler = SendReconciler(self.storage, self.client)
                unknown = self.storage.connection.execute(
                    "SELECT outbox_id FROM provider_outbox WHERE state='unknown' ORDER BY outbox_id LIMIT 10"
                ).fetchall()
                for item in unknown:
                    reconciler.reconcile(item["outbox_id"])
            if self.cleanup_worker is not None:
                for _ in range(5):
                    if not self.cleanup_worker.run_once():
                        break
            if self.gateway is not None and (
                self.config.remote.approvals or self.config.allows(RuntimeMode.APPROVALS)
            ):
                for _ in range(20):
                    if not self.gateway.publish_next_request():
                        break
                self._drain_codex_notifications()
            if self.ingress is not None and self.controller is not None:
                self._process_ingress()
            self._expire_payloads()
            now = time.monotonic()
            if self.provisioning is not None and now - last_preflight >= 300:
                result = self.provisioning.run(live=True, remote=self.config.remote)
                last_preflight = now
                if not result.passed:
                    hard_failures = {
                        failure
                        for failure in result.failures
                        if failure in {"tenant_key_mismatch", "app_id_mismatch"}
                    }
                    if hard_failures:
                        self.logger.error(
                            "tenant_or_app_identity_mismatch",
                            extra={"fields": {"failures": sorted(hard_failures)}},
                        )
                        self.stop_event.set()
                        continue
                    self.logger.warning(
                        "tenant_provisioning_preflight_warning",
                        extra={"fields": {"failures": list(result.failures)}},
                    )
            if self.pilot is not None and now - last_pilot_sample >= 60:
                self.pilot.sample()
                last_pilot_sample = now
            self.stop_event.wait(0.25)

    def _drain_outbox(self) -> None:
        if self.outbox_worker is None:
            return
        for index in range(20):
            if not self.outbox_worker.run_once():
                break
            if (index + 1) % 5 == 0:
                self._write_status()

    def _suppress_internal_task_bindings(self) -> None:
        """Keep relay-only tasks out of every Feishu surface."""

        internal_thread_ids = getattr(self.config, "internal_thread_ids", frozenset())
        if not internal_thread_ids:
            return
        now = utc_now()
        with self.storage.immediate() as connection:
            for thread_id in internal_thread_ids:
                connection.execute(
                    "UPDATE task_bindings SET opted_in=0,lifecycle_state='archived',updated_at=? "
                    "WHERE thread_id=?",
                    (now, thread_id),
                )
                connection.execute(
                    "UPDATE remote_task_grants SET state='revoked',updated_at=? "
                    "WHERE thread_id=? AND state!='revoked'",
                    (now, thread_id),
                )
                connection.execute(
                    "UPDATE provider_outbox SET state='permanent',"
                    "last_error_code='internal_thread_suppressed',lease_owner=NULL,"
                    "lease_expires_at=NULL,updated_at=? WHERE thread_id=? "
                    "AND state IN ('pending','retryable')",
                    (now, thread_id),
                )

    def _ensure_task_bindings(self, sources: tuple[Any, ...]) -> None:
        assert self.config.feishu is not None
        anchors = TaskAnchorManager(self.storage)
        internal_thread_ids = getattr(self.config, "internal_thread_ids", frozenset())
        for source in sources:
            if source.thread_id in internal_thread_ids:
                continue
            task_title = source.task_title
            if self.title_reader is not None and task_title is None:
                task_title = self.title_reader.title_for(source.thread_id, source.project_root)
            chat_id = self.config.feishu.target_chat_id
            project_name = source.project_name or self.config.project_name
            if source.project_id is not None:
                group = self.storage.connection.execute(
                    "SELECT chat_id,state FROM project_groups WHERE project_id=?",
                    (source.project_id,),
                ).fetchone()
                if group is None or group["state"] != "active" or not group["chat_id"]:
                    continue
                chat_id = str(group["chat_id"])
            existing = self.storage.connection.execute(
                "SELECT project_root,chat_id,conversation_mode,lifecycle_state,opted_in "
                "FROM task_bindings WHERE thread_id=?",
                (source.thread_id,),
            ).fetchone()
            if existing is None:
                if (
                    self.title_reader is not None
                    and task_title is None
                    and not self.title_reader.completed_attempt(source.thread_id)
                ):
                    continue
                anchors.opt_in(
                    thread_id=source.thread_id,
                    project_root=source.project_root,
                    chat_id=chat_id,
                    conversation_mode=self.config.feishu.conversation_mode.value,
                    task_title=task_title,
                    project_name=project_name,
                )
            else:
                if (
                    existing["chat_id"] != chat_id
                    or existing["conversation_mode"]
                    != self.config.feishu.conversation_mode.value
                ):
                    self.storage.connection.execute(
                        "UPDATE remote_task_grants SET state='drifted',updated_at=? WHERE thread_id=?",
                        (utc_now(), source.thread_id),
                    )
                    self.storage.connection.execute(
                        "INSERT INTO circuit_breakers(breaker_name,state,reason,updated_at) "
                        "VALUES('task_binding_drift','open',?,?) ON CONFLICT(breaker_name) DO UPDATE SET "
                        "state='open',reason=excluded.reason,updated_at=excluded.updated_at",
                        ("thread:" + source.thread_id, utc_now()),
                    )
                    continue
                if (
                    Path(str(existing["project_root"])).resolve()
                    != Path(source.project_root).resolve()
                ):
                    now = utc_now()
                    self.storage.connection.execute(
                        "UPDATE task_bindings SET project_root=?,current_binding_epoch="
                        "current_binding_epoch+1,updated_at=? WHERE thread_id=?",
                        (str(Path(source.project_root).resolve()), now, source.thread_id),
                    )
                if existing["lifecycle_state"] == "archived" or not existing["opted_in"]:
                    anchors.reactivate(source.thread_id)
                if task_title is not None:
                    anchors.sync_title(source.thread_id, task_title, project_name)
            ownership = self.storage.connection.execute(
                "SELECT 1 FROM thread_bindings WHERE thread_id=?", (source.thread_id,)
            ).fetchone()
            if ownership is None:
                state = (
                    OwnershipState.BRIDGE_OWNED
                    if self.config.allows(RuntimeMode.INBOUND)
                    else OwnershipState.DESKTOP_MIRROR_ONLY
                )
                self.storage.create_thread(source.thread_id, state)
            if getattr(self.config, "remote", None) is not None and self.config.remote.enabled:
                self._authorize_remote_task(source.thread_id, source.project_root, chat_id)

    def _reconcile_archived_task_bindings(self, thread_ids: frozenset[str]) -> None:
        anchors = TaskAnchorManager(self.storage)
        for thread_id in sorted(thread_ids):
            if anchors.archive(thread_id):
                self.logger.info(
                    "task_binding_archived",
                    extra={"fields": {"thread_id": thread_id}},
                )

    def _authorize_remote_task(
        self, thread_id: str, project_root: Path, chat_id: str
    ) -> None:
        identity = self.storage.connection.execute(
            "SELECT binding_epoch,state FROM identity_bindings WHERE binding_key='owner'"
        ).fetchone()
        task = self.storage.connection.execute(
            "SELECT current_binding_epoch,identity_binding_epoch,project_root,chat_id,opted_in "
            "FROM task_bindings WHERE thread_id=?",
            (thread_id,),
        ).fetchone()
        if (
            identity is None
            or identity["state"] != "active"
            or task is None
            or not task["opted_in"]
            or Path(str(task["project_root"])).resolve() != Path(project_root).resolve()
            or task["chat_id"] != chat_id
        ):
            raise ServiceError("remote task authorization lost its exact binding")
        service = self.storage.connection.execute(
            "SELECT fencing_token,process_state FROM service_state WHERE singleton=1"
        ).fetchone()
        if service is None or service["process_state"] != "running":
            raise ServiceError("remote task authorization requires a running fenced service")
        capabilities = {
            "text": self.config.remote.text,
            "image": self.config.remote.images,
            "file": self.config.remote.files,
            "approvals": self.config.remote.approvals,
            "controls": self.config.remote.controls,
        }
        encoded = canonicalize(capabilities)
        now = utc_now()
        self.storage.connection.execute(
            "INSERT INTO remote_task_grants(thread_id,project_root,chat_id,task_binding_epoch,"
            "identity_binding_epoch,service_fencing_token,capabilities_json,capabilities_hash,state,authorized_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,'active',?,?) ON CONFLICT(thread_id) DO UPDATE SET "
            "project_root=excluded.project_root,chat_id=excluded.chat_id,"
            "task_binding_epoch=excluded.task_binding_epoch,"
            "identity_binding_epoch=excluded.identity_binding_epoch,"
            "service_fencing_token=excluded.service_fencing_token,"
            "capabilities_json=excluded.capabilities_json,capabilities_hash=excluded.capabilities_hash,"
            "state='active',updated_at=excluded.updated_at",
            (
                thread_id,
                str(Path(project_root).resolve()),
                chat_id,
                int(task["current_binding_epoch"]),
                int(identity["binding_epoch"]),
                int(service["fencing_token"]),
                encoded.decode("utf-8"),
                sha256(encoded).hexdigest(),
                now,
                now,
            ),
        )

    def _profile_controller(self, thread_id: str) -> ProfileController:
        task = self.storage.connection.execute(
            "SELECT project_root FROM task_bindings WHERE thread_id=? AND opted_in=1",
            (thread_id,),
        ).fetchone()
        if task is None:
            raise ServiceError("profile target is not an opted-in task")
        cwd = Path(str(task["project_root"])).resolve(strict=True)
        roots: list[Path] = [path.resolve() for path in self.config.project_allowlist]
        if self.project_catalog is not None:
            roots.extend(
                root.resolve()
                for project in self.project_catalog.projects()
                for root in project.root_paths
            )
        unique_roots = tuple(dict.fromkeys(roots))
        if not any(cwd == root or cwd.is_relative_to(root) for root in unique_roots):
            raise ServiceError("task cwd is outside current Codex project authority")
        default = ExecutionProfile(
            SandboxType.DANGER_FULL_ACCESS,
            cwd,
            approval_policy=ApprovalPolicy.NEVER,
            network_access=None,
            writable_roots=(),
        )
        return ProfileController(
            self.storage,
            unique_roots,
            default,
        )

    def _drain_codex_notifications(self) -> None:
        assert self.codex_connection is not None and self.gateway is not None
        while True:
            try:
                notification = self.codex_connection.notifications.get_nowait()
            except Exception:
                return
            if self.gateway.observe_resolution_notification(notification):
                continue
            if notification.get("method") == "turn/completed" and self.controller is not None:
                params = notification.get("params")
                if isinstance(params, dict):
                    thread_id = params.get("threadId")
                    if isinstance(thread_id, str):
                        turn = params.get("turn")
                        turn_id = turn.get("id") if isinstance(turn, dict) else None
                        matched_dispatch = False
                        ingress_message_id: str | None = None
                        if isinstance(turn_id, str):
                            dispatch = self.storage.connection.execute(
                                "SELECT ingress_message_id FROM dispatch_records "
                                "WHERE thread_id=? AND turn_id=? AND state='accepted'",
                                (thread_id, turn_id),
                            ).fetchone()
                            if dispatch is not None:
                                ingress_message_id = str(dispatch["ingress_message_id"])
                            updated = self.storage.connection.execute(
                                "UPDATE dispatch_records SET state='completed',updated_at=? "
                                "WHERE thread_id=? AND turn_id=? AND state='accepted'",
                                (utc_now(), thread_id, turn_id),
                            )
                            matched_dispatch = updated.rowcount == 1
                        # A shared App Server can surface notifications for
                        # Desktop-owned work. Only reconcile a turn that this
                        # fenced bridge instance previously accepted.
                        if matched_dispatch:
                            turn_error = turn.get("error") if isinstance(turn, dict) else None
                            if turn_error is not None and ingress_message_id is not None:
                                self.storage.connection.execute(
                                    "UPDATE ingress_messages SET last_dispatch_error='codex_turn_failed' "
                                    "WHERE message_id=?",
                                    (ingress_message_id,),
                                )
                                self._queue_control_ack(
                                    ingress_message_id,
                                    thread_id,
                                    _turn_failure_ack(turn_error),
                                )
                            else:
                                profile = self._profile_controller(thread_id).load(thread_id)
                                self.controller.reconcile_profile(thread_id, profile)

    def _process_ingress(self) -> None:
        assert self.ingress is not None and self.controller is not None and self.client is not None
        if getattr(self.config.remote, "uses_cli", False) and self.cli_dispatcher is not None:
            self.cli_dispatcher.recover_abandoned_prestarts()
        self._expire_stale_ingress()
        rows = self.storage.connection.execute(
            "SELECT i.*,p.text FROM ingress_messages i JOIN ingress_payloads p ON p.message_id=i.message_id "
            "LEFT JOIN dispatch_records d ON d.ingress_message_id=i.message_id "
            "LEFT JOIN executed_command_tombstones t ON t.tombstone_key=i.message_id "
            "WHERE d.ingress_message_id IS NULL AND t.tombstone_key IS NULL "
            "AND i.routing_state IN ('control','routed_current','routed_reply') "
            "AND (i.dispatch_not_before IS NULL OR i.dispatch_not_before<=datetime('now')) "
            "ORDER BY i.ingest_seq LIMIT 20"
        ).fetchall()
        for row in rows:
            if self._expire_ingress_row(row):
                continue
            if self.ingress.suppress_if_outbound_echo(str(row["message_id"])):
                continue
            command = parse_command(row["message_type"], row["text"])
            if command is not None:
                if row["target_thread_id"] is None:
                    self._reject_ingress(row, "控制命令必须在已知任务话题内发送。")
                    continue
                try:
                    profiles = self._profile_controller(row["target_thread_id"])
                    self._execute_control(row, command, profiles)
                except DispatchBusy:
                    self._defer_ingress(row)
                except (
                    ControlError,
                    DispatchError,
                    DesktopGatewayError,
                    ServiceError,
                    ValueError,
                ):
                    self._reject_ingress(row, "控制命令未通过当前任务状态或安全边界校验，未执行。")
                continue
            if row["target_thread_id"] is None:
                continue
            try:
                input_items, capability, dispatch_text = self._prepare_ingress_input(row)
            except ValueError:
                self._reject_ingress(row, "附件未通过下载或本地文件安全校验，未交给 Codex。")
                continue
            if input_items is None:
                continue
            try:
                if getattr(self.config.remote, "uses_cli", False):
                    if self.cli_dispatcher is None:
                        raise ServiceError("Codex CLI dispatcher is unavailable")
                    image_paths = tuple(
                        Path(str(item["path"]))
                        for item in input_items
                        if item.get("type") == "localImage"
                        and isinstance(item.get("path"), str)
                    )
                    result = self.cli_dispatcher.dispatch(
                        ingress_message_id=row["message_id"],
                        thread_id=row["target_thread_id"],
                        text=dispatch_text,
                        required_capability=capability,
                        image_paths=image_paths,
                    )
                elif getattr(self.config.remote, "uses_desktop_relay", False):
                    if self.desktop_relay_dispatcher is None:
                        raise ServiceError("desktop relay dispatcher is unavailable")
                    attachment_paths = tuple(
                        Path(str(item["path"]))
                        for item in input_items
                        if item.get("type") == "localImage"
                        and isinstance(item.get("path"), str)
                    )
                    if capability == "file":
                        attachment = self.storage.connection.execute(
                            "SELECT local_path FROM ingress_attachments WHERE message_id=?",
                            (row["message_id"],),
                        ).fetchone()
                        if attachment is None or not attachment["local_path"]:
                            raise ServiceError("desktop relay file attachment path is unavailable")
                        attachment_paths += (Path(str(attachment["local_path"])),)
                    result = self.desktop_relay_dispatcher.dispatch(
                        ingress_message_id=row["message_id"],
                        thread_id=row["target_thread_id"],
                        text=dispatch_text,
                        required_capability=capability,
                        attachment_paths=attachment_paths,
                    )
                elif self.config.remote.uses_desktop:
                    if self.desktop_dispatcher is None:
                        raise ServiceError("desktop Codex dispatcher is unavailable")
                    attachment_paths = tuple(
                        Path(str(item["path"]))
                        for item in input_items
                        if item.get("type") == "localImage"
                        and isinstance(item.get("path"), str)
                    )
                    if capability == "file":
                        attachment = self.storage.connection.execute(
                            "SELECT local_path FROM ingress_attachments WHERE message_id=?",
                            (row["message_id"],),
                        ).fetchone()
                        if attachment is None or not attachment["local_path"]:
                            raise ServiceError("desktop file attachment path is unavailable")
                        attachment_paths += (Path(str(attachment["local_path"])),)
                    result = self.desktop_dispatcher.dispatch(
                        ingress_message_id=row["message_id"],
                        thread_id=row["target_thread_id"],
                        text=dispatch_text,
                        required_capability=capability,
                        attachment_paths=attachment_paths,
                    )
                else:
                    active_turn_id = self._rollout_turn_hint(row["target_thread_id"])
                    profiles = self._profile_controller(row["target_thread_id"])
                    profile = profiles.load(row["target_thread_id"])
                    if (
                        self.config.remote.auto_approve
                        and profile.approval_policy is not ApprovalPolicy.NEVER
                    ):
                        profile = replace(profile, approval_policy=ApprovalPolicy.NEVER)
                    profile_hash = profiles.persist(row["target_thread_id"], profile)
                    result = self.controller.dispatch(
                        ingress_message_id=row["message_id"],
                        thread_id=row["target_thread_id"],
                        text=dispatch_text,
                        input_items=input_items,
                        required_capability=capability,
                        profile=profile,
                        profile_hash=profile_hash,
                        active_turn_id=active_turn_id,
                    )
                if result.state == "accepted" and result.turn_id is not None:
                    self.storage.connection.execute(
                        "UPDATE ingress_messages SET last_dispatch_error=NULL "
                        "WHERE tenant_key=? AND app_id=? AND message_id=?",
                        (row["tenant_key"], row["app_id"], row["message_id"]),
                    )
                    self._queue_dispatch_ack(
                        row["message_id"], row["target_thread_id"]
                    )
                    existing = self._active_rollout_turns.get(
                        row["target_thread_id"], frozenset()
                    )
                    self._active_rollout_turns[row["target_thread_id"]] = (
                        existing | frozenset({result.turn_id})
                    )
                elif result.state == "submitted_unconfirmed":
                    self._queue_submitted_unconfirmed_ack(
                        row["message_id"], row["target_thread_id"]
                    )
                elif result.state == "outcome_unknown":
                    self._queue_unconfirmed_ack(
                        row["message_id"], row["target_thread_id"]
                    )
            except DispatchBusy:
                self._defer_ingress(row)
            except ProtocolError as exc:
                # A pre-dispatch App Server timeout has not sent turn/start,
                # so retain the ingress row and retry instead of rejecting the
                # user's message or terminating the Feishu long connection.
                self._queue_pending_ack(
                    row["message_id"], row["target_thread_id"]
                )
                self.storage.connection.execute(
                    "UPDATE ingress_messages SET dispatch_not_before=datetime('now','+5 seconds'),"
                    "dispatch_attempt_count=dispatch_attempt_count+1,last_dispatch_error='app_server_unavailable' "
                    "WHERE tenant_key=? AND app_id=? AND message_id=?",
                    (row["tenant_key"], row["app_id"], row["message_id"]),
                )
                self.logger.error(
                    "codex_dispatch_deferred",
                    extra={
                        "fields": {
                            "message_id": row["message_id"],
                            "error": type(exc).__name__,
                        }
                    },
                )
            except (DispatchError, ControlError, ServiceError, ValueError):
                self._reject_ingress(
                    row,
                    "该任务当前不满足远程输入的身份、权限或状态条件，消息未执行。",
                )

    def _expire_stale_ingress(self) -> int:
        """Stop retrying an uplink that has not reached Codex within one minute."""

        rows = self.storage.connection.execute(
            "SELECT i.* FROM ingress_messages i "
            "LEFT JOIN dispatch_records d ON d.ingress_message_id=i.message_id "
            "LEFT JOIN executed_command_tombstones t ON t.tombstone_key=i.message_id "
            "WHERE t.tombstone_key IS NULL "
            "AND i.routing_state IN ('control','routed_current','routed_reply') "
            "AND datetime(i.received_at)<=datetime('now',?) "
            "AND (d.ingress_message_id IS NULL OR d.state NOT IN ('accepted','completed')) "
            "ORDER BY i.ingest_seq LIMIT 100",
            (f"-{_INGRESS_DELIVERY_TIMEOUT_SECONDS} seconds",),
        ).fetchall()
        return sum(int(self._expire_ingress_row(row)) for row in rows)

    def _expire_ingress_row(self, row: Any) -> bool:
        """Apply the terminal deadline again immediately before dispatch."""

        updated = self.storage.connection.execute(
            "UPDATE ingress_messages SET routing_state='dispatch_rejected',"
            "dispatch_not_before=NULL,last_dispatch_error='delivery_timeout' "
            "WHERE tenant_key=? AND app_id=? AND message_id=? "
            "AND routing_state IN ('control','routed_current','routed_reply') "
            "AND datetime(received_at)<=datetime('now',?)",
            (
                row["tenant_key"],
                row["app_id"],
                row["message_id"],
                f"-{_INGRESS_DELIVERY_TIMEOUT_SECONDS} seconds",
            ),
        )
        if updated.rowcount != 1:
            return False
        self._queue_control_ack(
            row["message_id"],
            row["target_thread_id"],
            "⚠ 上行消息投递 Codex 超过 1 分钟仍未成功，已丢弃并停止重试，请重新发送。",
        )
        return True

    def _has_blocking_active_turn(self, thread_id: str) -> bool:
        """Keep a thread queued while it has a live Desktop or bridge turn.

        The local App Server is terminated with its Job Object during restart. A
        task_started record from an older fencing generation can consequently
        remain without task_complete; that known orphan must not block the
        thread forever.  Unknown turns are Desktop-owned and always block.
        """
        try:
            return self._active_turn_for_steer(thread_id) is not None
        except DispatchBusy:
            return True

    def _rollout_turn_hint(self, thread_id: str) -> str | None:
        """Return an unambiguous rollout hint without blocking App Server lookup.

        Multiple unmatched ``task_started`` records are possible after an
        interrupted writer.  The controller resolves the authoritative
        in-progress turn from the schema-validated ``thread/resume`` response.
        """

        try:
            return self._active_turn_for_steer(thread_id)
        except DispatchBusy:
            return None

    def _active_turn_for_steer(self, thread_id: str) -> str | None:
        """Return the exact live turn that can receive ``turn/steer``.

        A stale turn from an older service fence is ignored. Multiple live or
        otherwise indistinguishable turns stay queued because the
        bridge cannot truthfully choose the Codex target precondition.
        """

        active_turns = self._active_rollout_turns.get(thread_id, frozenset())
        if not active_turns:
            return None
        service = self.storage.connection.execute(
            "SELECT fencing_token FROM service_state WHERE singleton=1"
        ).fetchone()
        if service is None:
            raise DispatchBusy("service fence is unavailable")
        current_fence = int(service["fencing_token"])
        candidates: list[str] = []
        for turn_id in active_turns:
            dispatch = self.storage.connection.execute(
                "SELECT fencing_token,state FROM dispatch_records "
                "WHERE thread_id=? AND turn_id=?",
                (thread_id, turn_id),
            ).fetchone()
            if dispatch is None:
                candidates.append(turn_id)
                continue
            if int(dispatch["fencing_token"]) >= current_fence and dispatch["state"] not in {
                "completed",
                "rejected",
            }:
                candidates.append(turn_id)
        if len(candidates) > 1:
            raise DispatchBusy("multiple active turns cannot be ordered safely")
        return candidates[0] if candidates else None

    def _prepare_ingress_input(
        self, row: Any
    ) -> tuple[tuple[dict[str, Any], ...] | None, str, str]:
        if row["message_type"] == "text":
            text = str(row["text"])
            return ({"type": "text", "text": text},), "text", text
        attachment = self.storage.connection.execute(
            "SELECT * FROM ingress_attachments WHERE message_id=?",
            (row["message_id"],),
        ).fetchone()
        if attachment is None:
            raise ValueError("attachment metadata missing")
        if attachment["state"] in {"pending", "retryable"}:
            due = self.storage.connection.execute(
                "SELECT julianday(next_attempt_at)<=julianday('now') "
                "FROM ingress_attachments WHERE message_id=?",
                (row["message_id"],),
            ).fetchone()[0]
            if not due:
                return None, str(attachment["resource_type"]), ""
            limit = (
                self.config.remote.max_image_bytes
                if attachment["resource_type"] == "image"
                else self.config.remote.max_file_bytes
            )
            result = self.client.download_message_resource(
                message_id=row["message_id"],
                file_key=attachment["resource_key"],
                resource_type=attachment["resource_type"],
                chat_id=row["chat_id"],
                max_bytes=limit,
            )
            if result.outcome.value == "confirmed" and result.content is not None:
                self.storage.connection.execute(
                    "UPDATE ingress_attachments SET mime_type=?,content=?,content_hash=?,state='ready',"
                    "attempt_count=attempt_count+1,last_error=NULL,updated_at=? WHERE message_id=? "
                    "AND state IN ('pending','retryable')",
                    (
                        result.content_type,
                        result.content,
                        sha256(result.content).hexdigest(),
                        utc_now(),
                        row["message_id"],
                    ),
                )
            elif result.outcome.value == "retryable":
                delay = result.retry_after_seconds or min(
                    300, 2 ** min(int(attachment["attempt_count"]) + 1, 8)
                )
                self.storage.connection.execute(
                    "UPDATE ingress_attachments SET state='retryable',attempt_count=attempt_count+1,"
                    "next_attempt_at=datetime('now',?),last_error=?,updated_at=? WHERE message_id=?",
                    (f"+{int(delay)} seconds", result.code, utc_now(), row["message_id"]),
                )
                return None, str(attachment["resource_type"]), ""
            else:
                self.storage.connection.execute(
                    "UPDATE ingress_attachments SET state='permanent',attempt_count=attempt_count+1,"
                    "last_error=?,updated_at=? WHERE message_id=?",
                    (result.code, utc_now(), row["message_id"]),
                )
                raise ValueError("attachment download failed permanently")
            attachment = self.storage.connection.execute(
                "SELECT * FROM ingress_attachments WHERE message_id=?", (row["message_id"],)
            ).fetchone()
        if attachment["state"] == "permanent":
            raise ValueError("attachment is permanently rejected")
        if attachment["state"] not in {"ready", "materialized"} or attachment["content"] is None:
            return None, str(attachment["resource_type"]), ""
        task = self.storage.connection.execute(
            "SELECT project_root FROM task_bindings WHERE thread_id=? AND opted_in=1",
            (row["target_thread_id"],),
        ).fetchone()
        if task is None:
            raise ValueError("attachment target task disappeared")
        materialized = materialize_attachment(
            project_root=Path(str(task["project_root"])),
            message_id=row["message_id"],
            resource_type=attachment["resource_type"],
            original_file_name=attachment["original_file_name"],
            mime_type=str(attachment["mime_type"] or "application/octet-stream"),
            content=bytes(attachment["content"]),
        )
        self.storage.connection.execute(
            "UPDATE ingress_attachments SET local_path=?,state='materialized',updated_at=? "
            "WHERE message_id=? AND state IN ('ready','materialized')",
            (str(materialized.path), utc_now(), row["message_id"]),
        )
        return materialized.input_items, str(attachment["resource_type"]), materialized.prompt_text

    def _reject_ingress(self, row: Any, acknowledgement: str) -> None:
        self.storage.connection.execute(
            "UPDATE ingress_messages SET routing_state='dispatch_rejected',last_dispatch_error='rejected',"
            "dispatch_attempt_count=dispatch_attempt_count+1 WHERE tenant_key=? AND app_id=? AND message_id=?",
            (row["tenant_key"], row["app_id"], row["message_id"]),
        )
        self._queue_control_ack(row["message_id"], row["target_thread_id"], acknowledgement)

    def _execute_control(self, row: Any, command: Any, profiles: ProfileController) -> None:
        if row["target_thread_id"] is None:
            raise ServiceError("control command lacks a task binding")
        self.controller.require_control_authorized(row["target_thread_id"])
        acknowledgement: str | None = None
        if command.name == "use":
            assert self.config.feishu is not None
            if self.config.feishu.conversation_mode.value == "topic_group":
                acknowledgement = "话题群按当前任务话题路由，无需使用 /use。"
            else:
                epoch = self.ingress.begin_selection(command.argument)
                self.ingress.queue_selection_confirmation(command.argument, epoch)
        elif command.name in {"sandbox", "network", "approval-policy", "cwd", "writable"}:
            if row["target_thread_id"] is None:
                raise ServiceError("profile command requires a selected task")
            if getattr(
                self.config.remote,
                "uses_host_writer",
                getattr(self.config.remote, "uses_desktop", False),
            ):
                raise ControlError(
                    "host-owned tasks keep execution settings in the Codex task host"
                )
            profile_hash = profiles.apply(row["target_thread_id"], command)
            acknowledgement = f"下一 turn 的执行配置已更新：{profile_hash[:12]}"
        elif command.name == "append":
            if getattr(self.config.remote, "uses_cli", False):
                if self.cli_dispatcher is None:
                    raise ServiceError("Codex CLI dispatcher is unavailable")
                result = self.cli_dispatcher.dispatch(
                    ingress_message_id=row["message_id"],
                    thread_id=row["target_thread_id"],
                    text=command.argument,
                    required_capability="controls",
                )
                if result.state != "accepted" or result.turn_id is None:
                    raise ServiceError("Codex append acceptance is unconfirmed")
                acknowledgement = f"已提交给 Codex turn {result.turn_id[:12]}"
            elif self.config.remote.uses_desktop:
                if self.desktop_dispatcher is None:
                    raise ServiceError("desktop Codex dispatcher is unavailable")
                result = self.desktop_dispatcher.dispatch(
                    ingress_message_id=row["message_id"],
                    thread_id=row["target_thread_id"],
                    text=command.argument,
                    required_capability="controls",
                )
                if result.state != "accepted" or result.turn_id is None:
                    raise ServiceError("desktop append acceptance is unconfirmed")
                acknowledgement = f"已提交给 Codex turn {result.turn_id[:12]}"
            else:
                active = self.storage.connection.execute(
                    "SELECT turn_id FROM dispatch_records WHERE thread_id=? AND state='accepted' "
                    "ORDER BY created_at DESC LIMIT 1", (row["target_thread_id"],)
                ).fetchone()
                if active is None:
                    raise ServiceError("append requires an active known turn")
                self.controller.append(
                    row["target_thread_id"], active["turn_id"], command.argument, row["message_id"]
                )
                acknowledgement = f"已追加到 turn {active['turn_id'][:12]}"
        elif command.name == "stop":
            if getattr(self.config.remote, "uses_host_writer", False):
                if self.desktop_gateway is None:
                    raise ServiceError("desktop Codex gateway is unavailable")
                self.desktop_gateway.stop(row["target_thread_id"])
                acknowledgement = "已通过 Codex 桌面请求停止当前回合。"
            else:
                active = self.storage.connection.execute(
                    "SELECT turn_id FROM dispatch_records WHERE thread_id=? AND state='accepted' "
                    "ORDER BY created_at DESC LIMIT 1", (row["target_thread_id"],)
                ).fetchone()
                if active is None:
                    raise ServiceError("stop requires an active known turn")
                self.controller.stop(row["target_thread_id"], active["turn_id"])
                acknowledgement = f"已请求中断 turn {active['turn_id'][:12]}"
        elif command.name == "hard-stop":
            EmergencyController(self.storage, self._terminate_codex).hard_stop("owner_command")
            self.stop_event.set()
        elif command.name == "tasks":
            tasks = self.storage.connection.execute(
                "SELECT thread_id,anchor_state FROM task_bindings WHERE opted_in=1 ORDER BY thread_id"
            ).fetchall()
            acknowledgement = "任务：\n" + "\n".join(
                f"- {item['thread_id']} [{item['anchor_state']}]" for item in tasks
            )
        elif command.name == "status":
            snapshot = self._capture_health()
            acknowledgement = (
                f"状态：{snapshot.process_state}；outbox={sum(snapshot.outbox.values())}；"
                f"unknown={snapshot.ingress_indeterminate}；breakers={len(snapshot.open_breakers)}"
            )
        elif command.name == "help":
            if getattr(
                self.config.remote,
                "uses_host_writer",
                getattr(self.config.remote, "uses_desktop", False),
            ):
                acknowledgement = (
                    "命令：/tasks /status /append <text> /stop /hard-stop。"
                    "执行权限、目录和网络设置由 Codex 桌面当前任务管理。"
                )
            else:
                acknowledgement = (
                    "命令：/tasks /use <task> /status /sandbox <mode> /writable <path> "
                    "/network on|off /cwd <path> /approval-policy <policy> /append <text> /stop /hard-stop"
                )
        # Read-only help/status/tasks are acknowledged by status/card UX; they
        # never dispatch a Codex turn.
        self.storage.connection.execute(
            "INSERT OR IGNORE INTO executed_command_tombstones(tombstone_key,content_hash,target_thread_id,"
            "dispatch_attempt_id,retain_until) VALUES(?,?,?,?,datetime('now','+365 days'))",
            (
                row["message_id"],
                row["content_hash"],
                row["target_thread_id"] or "control",
                "control:" + row["message_id"],
            ),
        )
        if acknowledgement is not None:
            self._queue_control_ack(
                row["message_id"], row["target_thread_id"], acknowledgement
            )

    def _queue_control_ack(
        self, message_id: str, target_thread_id: str | None, text: str
    ) -> None:
        queue_ingress_status(
            self.storage,
            self.config.feishu,
            message_id=message_id,
            target_thread_id=target_thread_id,
            status="control",
            text=text,
            priority=180,
        )

    def _queue_dispatch_ack(
        self, message_id: str, target_thread_id: str | None
    ) -> None:
        queue_ingress_status(
            self.storage,
            self.config.feishu,
            message_id=message_id,
            target_thread_id=target_thread_id,
            status="submitted",
            text="✓ 已提交 Codex，正在处理。",
            priority=210,
        )

    def _queue_pending_ack(
        self, message_id: str, target_thread_id: str | None
    ) -> None:
        queue_ingress_status(
            self.storage,
            self.config.feishu,
            message_id=message_id,
            target_thread_id=target_thread_id,
            status="pending",
            text="⏳ 尚未提交 Codex：当前任务忙碌或服务暂不可用，正在排队重试。",
            priority=210,
        )

    def _defer_ingress(self, row: Any) -> None:
        """Retain an undispatched ingress message until Codex releases its writer."""
        self._queue_pending_ack(row["message_id"], row["target_thread_id"])
        self.storage.connection.execute(
            "UPDATE ingress_messages SET dispatch_not_before=datetime('now','+15 seconds'),"
            "dispatch_attempt_count=dispatch_attempt_count+1,last_dispatch_error='thread_busy' "
            "WHERE tenant_key=? AND app_id=? AND message_id=?",
            (row["tenant_key"], row["app_id"], row["message_id"]),
        )

    def _queue_submitted_unconfirmed_ack(
        self, message_id: str, target_thread_id: str | None
    ) -> None:
        queue_ingress_status(
            self.storage,
            self.config.feishu,
            message_id=message_id,
            target_thread_id=target_thread_id,
            status="submitted",
            text="✓ 已提交 Codex。",
            priority=210,
        )

    def _queue_unconfirmed_ack(
        self, message_id: str, target_thread_id: str | None
    ) -> None:
        queue_ingress_status(
            self.storage,
            self.config.feishu,
            message_id=message_id,
            target_thread_id=target_thread_id,
            status="unconfirmed",
            text="⚠ 未能确认已提交 Codex：请先在 Codex 任务中核对，避免重复发送。",
            priority=210,
        )

    def _expire_payloads(self) -> None:
        self.storage.connection.execute(
            "DELETE FROM ingress_payloads WHERE expires_at<datetime('now') AND message_id IN ("
            "SELECT tombstone_key FROM executed_command_tombstones)"
        )

    def _write_status(self) -> None:
        if self.config.status_path is not None:
            write_status_atomic(self.config.status_path, self._capture_health())

    def _remote_connection_state(self) -> str | None:
        if self.long_connection is None or self.long_connection_thread is None:
            return None
        if not self.long_connection_thread.is_alive():
            return "thread_stopped"
        return self.long_connection.connection_state()

    def _capture_health(self) -> Any:
        return capture_health(
            self.storage, remote_connection_state=self._remote_connection_state()
        )

    def _monitor_long_connection(self) -> None:
        state = self._remote_connection_state()
        if state is None:
            return
        now = time.monotonic()
        if state == "connected":
            self.long_connection_disconnected_since = None
            return
        if self.long_connection_disconnected_since is None:
            self.long_connection_disconnected_since = now
        if state == "thread_stopped" or now - self.long_connection_disconnected_since >= 300:
            self.storage.connection.execute(
                "INSERT INTO circuit_breakers(breaker_name,state,reason,updated_at) "
                "VALUES('feishu_long_connection','open',?,?) ON CONFLICT(breaker_name) DO UPDATE SET "
                "state='open',reason=excluded.reason,updated_at=excluded.updated_at",
                (state, utc_now()),
            )
            EmergencyController(self.storage, self._terminate_codex).soft_quiesce(
                "feishu_long_connection_" + state
            )
            self.stop_event.set()

    def _install_signal_handlers(self) -> None:
        def stop_handler(*_: object) -> None:
            self.stop_event.set()

        signal.signal(signal.SIGINT, stop_handler)
        signal.signal(signal.SIGTERM, stop_handler)

    def _terminate_codex(self) -> None:
        if self.codex_connection is not None:
            self.codex_connection.close()

    def _shutdown(self) -> None:
        self.storage.connection.execute(
            "UPDATE service_state SET process_state='quiescing',updated_at=? WHERE singleton=1",
            (utc_now(),),
        )
        deadline = time.monotonic() + 10
        while self.outbox_worker is not None and time.monotonic() < deadline:
            if not self.outbox_worker.run_once():
                break
        self.storage.connection.execute(
            "UPDATE service_state SET process_state='stopping',updated_at=? WHERE singleton=1",
            (utc_now(),),
        )
        self._terminate_codex()
        if self.title_reader is not None:
            self.title_reader.close()
        if self.client is not None:
            self.client.close()
        self.storage.connection.execute(
            "UPDATE service_state SET process_state='stopped',instance_id=NULL,updated_at=? WHERE singleton=1",
            (utc_now(),),
        )
        self._write_status()
        self.storage.close()


def _turn_failure_ack(error: object) -> str:
    material = json.dumps(error, ensure_ascii=False, separators=(",", ":")).casefold()
    if "timed out" in material or "timeout" in material:
        reason = "模型连接超时"
    elif "unauthorized" in material or "401" in material:
        reason = "模型账户认证失败"
    elif "usage" in material or "limit" in material or "429" in material:
        reason = "模型额度或速率受限"
    else:
        reason = "Codex 回合执行失败"
    return f"远程消息已送入 Codex，但{reason}，没有生成回复。该次回合已结束，请重新发送。"


def _load_or_create_local_key(path: Path) -> bytes:
    # The blob is encrypted to the current Windows user so it is not stored as
    # plaintext in the runtime directory.
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        value = unprotect_current_user(path.read_bytes())
    else:
        import secrets

        value = secrets.token_bytes(32)
        protected = protect_current_user(value)
        with path.open("xb") as handle:
            handle.write(protected)
            handle.flush()
    if len(value) != 32:
        raise ServiceError("local approval key has an invalid length")
    return value
