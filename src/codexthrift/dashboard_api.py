from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from .app_server_session import codex_runtime_diagnostics
from .application.mode_switch_service import ModeSwitchService
from .application.quota_service import QuotaService
from .application.session_migration_service import SessionMigrationService
from .application.storage_maintenance_service import StorageMaintenanceService
from .content_capture import encryption_key
from .controller import RouterController
from .dashboard_store import DashboardStore
from .launchd import install_launch_agent, uninstall_launch_agent
from .quota_adapter import QuotaAdapter
from .util import ControlError

ROUTERD_API_SCHEMA_VERSION = 24


def _payload_import_delay_seconds() -> float:
    try:
        configured = float(os.environ.get("CODEXTHRIFT_PAYLOAD_IMPORT_DELAY_SECONDS", "10"))
    except ValueError:
        configured = 10.0
    return max(0.25, min(configured, 300.0))


def _payload_import_enabled() -> bool:
    return os.environ.get("CODEXTHRIFT_PAYLOAD_IMPORT_ENABLED", "0").lower() not in {
        "0",
        "false",
        "no",
    }


class ConfirmedAction(BaseModel):
    confirmed: bool = False
    allow_unverified: bool = False


class RestoreNativeAction(BaseModel):
    confirmed: bool = False
    backup_path: str | None = None


class CodexModeAction(BaseModel):
    confirm: str = Field(min_length=1, max_length=64)
    allow_unverified: bool = False


class SessionMigrationAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: str = Field(min_length=1, max_length=64)


class CodexClientSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pid: int = Field(gt=0)
    create_time: float = Field(gt=0)
    kind: str = Field(pattern="^(codex_cli|codex_app_server|vscode_app_server)$")
    command_fingerprint: str = Field(min_length=64, max_length=64)


class CodexModeSwitchCreateAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str = Field(pattern="^(native|routed)$")
    client_policy: str = Field(pattern="^(wait|defer|terminate_selected)$")
    selected_clients: list[CodexClientSelection] = Field(default_factory=list, max_length=200)
    replace_active: bool = True
    # The managed switch performs a live Models + Responses route preflight.
    # The subscription model matrix remains diagnostic and may be unavailable
    # on first use, so the switch is permissive by default.
    allow_unverified: bool = True
    confirm: str = Field(min_length=1, max_length=64)


class CodexModeSwitchCancelAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: str = Field(min_length=1, max_length=64)


class CodexModeSwitchRetryAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    force: bool = False
    confirm: str = Field(min_length=1, max_length=64)


class ResetCreditAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed: bool = False
    idempotency_key: str = Field(min_length=8, max_length=128)
    profile_id: str | None = Field(default=None, max_length=120)


class LoginStatusAction(BaseModel):
    login_id: str = Field(min_length=1, max_length=300)


class LogoutAction(BaseModel):
    confirmed: bool = False


class PricingRuleAction(BaseModel):
    model_pattern: str = Field(min_length=1, max_length=300)
    input_usd_per_million: float = Field(ge=0, le=1_000_000)
    output_usd_per_million: float = Field(ge=0, le=1_000_000)


class StorageRetentionPreviewAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit_gib: float = Field(ge=0.5, le=1024)
    cleanup_ratio: float = Field(default=0.8, ge=0.5, le=0.95)
    minimum_retention_hours: int = Field(default=168, ge=1, le=24 * 365)
    delete_mode: str = Field(default="payload_only", pattern="^(payload_only|full_call)$")


class StorageRetentionExecuteAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_id: str = Field(min_length=32, max_length=64)
    confirm: str = Field(min_length=1, max_length=64)


class StorageMaintenanceCancelAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: str = Field(min_length=1, max_length=64)


class StorageCompactionExecuteAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_id: str = Field(min_length=32, max_length=64)
    confirmed: bool = False
    # Keep the old field during the Desk/Core rollout; new clients use the boolean.
    confirm: str | None = Field(default=None, max_length=64)


class StorageArchiveCompressionExecuteAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_id: str = Field(min_length=32, max_length=64)
    confirmed: bool = False


class StorageMaintenanceSettingsAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    limit_gib: float = Field(default=10, ge=0.5, le=1024)
    cleanup_ratio: float = Field(default=0.8, ge=0.5, le=0.95)
    minimum_retention_hours: int = Field(default=168, ge=1, le=24 * 365)
    delete_mode: str = Field(default="payload_only", pattern="^(payload_only|full_call)$")
    check_interval_hours: int = Field(default=24, ge=1, le=24 * 30)


class DashboardService:
    def __init__(
        self,
        controller: RouterController,
        store: DashboardStore,
        quota: QuotaAdapter | QuotaService,
    ):
        self.controller = controller
        self.store = store
        self.quota = quota
        self._quota_lock = threading.Lock()
        self._audit_lock = threading.Lock()
        self._audit_refresh_lock = threading.Lock()
        self._audit_refresh_thread: threading.Thread | None = None
        self._audit_completed_fingerprint: tuple[int, int] | None = None
        self._audit_completed_fingerprint_known = False
        self._audit_refresh_result: dict[str, int | bool] = {
            "imported": 0,
            "skipped": 0,
            "offset": 0,
        }
        self._audit_data_generation = 0
        self._audit_refresh_error_code: str | None = None
        self._dashboard_cache_lock = threading.Lock()
        self._dashboard_cache_epoch = 0
        self._dashboard_cache: dict[tuple[Any, ...], tuple[int, int, float, Any]] = {}
        self._dashboard_cache_key_locks: dict[tuple[Any, ...], threading.Lock] = {}
        self._payload_import_lock = threading.Lock()
        self._payload_import_thread: threading.Thread | None = None
        self._payload_import_last_started = 0.0
        self._payload_import_result = {"imported": 0, "skipped": 0}
        self._payload_import_delay_seconds = _payload_import_delay_seconds()
        self._payload_import_enabled = _payload_import_enabled()
        self._payload_index_lock = threading.Lock()
        self._payload_index_thread: threading.Thread | None = None
        self._payload_index_result = {"indexed": 0, "skipped": 0}
        self._semantics_migration_lock = threading.Lock()
        self._semantics_migration_thread: threading.Thread | None = None
        self._semantics_migration_result = {"migrated": 0, "inferred": 0, "skipped": 0, "remaining": 0}
        self._database_write_freeze = threading.Event()
        self._database_write_freeze_lock = threading.Lock()
        self.storage = StorageMaintenanceService(
            controller.paths,
            store,
            controller,
            database_write_freeze=self.freeze_database_writes,
        )
        self.mode_switch = ModeSwitchService(controller)
        self.session_migration = SessionMigrationService(controller.paths)
        self.store.initialize()

    def create_mode_switch(
        self,
        action: CodexModeSwitchCreateAction,
        *,
        request_id: str,
    ) -> dict[str, Any]:
        result = self.mode_switch.create(
            target=action.target,
            client_policy=action.client_policy,
            selected_clients=[item.model_dump() for item in action.selected_clients],
            confirm=action.confirm,
            request_id=request_id,
            replace_active=action.replace_active,
            allow_unverified=action.allow_unverified,
        )
        self.store.record_event(
            event_type="codex_mode_switch_created",
            component="routing",
            request_id=request_id,
            details={
                "job_id": result.get("job_id"),
                "target": action.target,
                "client_policy": action.client_policy,
                "reused": result.get("reused") is True,
                "replace_active": action.replace_active,
                "waiting_for_safe_boundary": result.get("waiting_for_safe_boundary") is True,
            },
        )
        superseded = result.get("superseded_job")
        if isinstance(superseded, dict):
            self.store.record_event(
                event_type="codex_mode_switch_superseded",
                component="routing",
                request_id=request_id,
                details={
                    "job_id": superseded.get("job_id"),
                    "superseded_by": result.get("job_id"),
                    "previous_target": superseded.get("target"),
                    "target": action.target,
                    "waiting_for_safe_boundary": result.get("waiting_for_safe_boundary") is True,
                },
            )
        return result

    def cancel_mode_switch(
        self,
        job_id: str,
        action: CodexModeSwitchCancelAction,
        *,
        request_id: str,
    ) -> dict[str, Any]:
        result = self.mode_switch.cancel(job_id, confirm=action.confirm)
        self.store.record_event(
            event_type="codex_mode_switch_canceled",
            component="routing",
            request_id=request_id,
            details={"job_id": job_id, "state": result.get("state")},
        )
        return result

    def retry_mode_switch_clients(
        self,
        job_id: str,
        action: CodexModeSwitchRetryAction,
        *,
        request_id: str,
    ) -> dict[str, Any]:
        result = self.mode_switch.retry_clients(
            job_id,
            force=action.force,
            confirm=action.confirm,
        )
        self.store.record_event(
            event_type="codex_mode_switch_clients_retried",
            component="routing",
            request_id=request_id,
            details={"job_id": job_id, "force": action.force},
        )
        return result

    def refresh_audit(self) -> dict[str, int | bool]:
        with self._audit_lock:
            if self.store.payload_key is None:
                self.store.payload_key = encryption_key(self.controller.paths, create=False)
            audit = self.store.import_audit_jsonl(self.controller.paths.audit_log)
        payload_index, index_pending = self._schedule_payload_index()
        payloads, pending = self._schedule_payload_import()
        semantics, semantics_pending = self._schedule_semantics_migration()
        return {
            **audit,
            "payload_indexed": payload_index["indexed"],
            "payload_index_skipped": payload_index["skipped"],
            "payload_index_pending": index_pending,
            "payload_imported": payloads["imported"],
            "payload_skipped": payloads["skipped"],
            "payload_import_pending": pending,
            "semantics_migrated": semantics["migrated"],
            "semantics_inferred": semantics["inferred"],
            "semantics_skipped": semantics["skipped"],
            "semantics_remaining": semantics["remaining"],
            "semantics_migration_pending": semantics_pending,
        }

    def audit_read_model_status(self) -> dict[str, int | bool | str | None]:
        with self._audit_refresh_lock:
            return self._audit_read_model_status_locked()

    def schedule_audit_refresh(self) -> dict[str, int | bool | str | None]:
        """Start one background audit import and immediately return the current read model."""
        fingerprint = self._audit_fingerprint()
        with self._audit_refresh_lock:
            if self._database_write_freeze.is_set():
                return self._audit_read_model_status_locked()
            if (
                self._audit_completed_fingerprint_known
                and self._audit_completed_fingerprint == fingerprint
            ):
                return self._audit_read_model_status_locked()
            if self._audit_refresh_thread is not None and self._audit_refresh_thread.is_alive():
                return self._audit_read_model_status_locked()
            thread = threading.Thread(
                target=self._refresh_audit_background,
                args=(fingerprint,),
                name="routerd-audit-refresh",
                daemon=True,
            )
            self._audit_refresh_thread = thread
            thread.start()
            return self._audit_read_model_status_locked()

    def wait_for_audit_refresh(self, timeout: float = 2.0) -> bool:
        with self._audit_refresh_lock:
            thread = self._audit_refresh_thread
        if thread is None:
            return True
        thread.join(timeout=max(0.0, timeout))
        return not thread.is_alive()

    def cached_dashboard_read(
        self,
        key: tuple[Any, ...],
        loader: Callable[[], Any],
        *,
        ttl_seconds: float = 5.0,
    ) -> Any:
        generation = self._audit_generation()
        now = time.monotonic()
        with self._dashboard_cache_lock:
            cache_epoch = self._dashboard_cache_epoch
            cached = self._dashboard_cache.get(key)
            if (
                cached is not None
                and cached[0] == generation
                and cached[1] == cache_epoch
                and cached[2] > now
            ):
                return deepcopy(cached[3])
            key_lock = self._dashboard_cache_key_locks.setdefault(key, threading.Lock())

        with key_lock:
            generation = self._audit_generation()
            now = time.monotonic()
            with self._dashboard_cache_lock:
                cache_epoch = self._dashboard_cache_epoch
                cached = self._dashboard_cache.get(key)
                if (
                    cached is not None
                    and cached[0] == generation
                    and cached[1] == cache_epoch
                    and cached[2] > now
                ):
                    return deepcopy(cached[3])
            result = loader()
            if self._audit_generation() == generation:
                with self._dashboard_cache_lock:
                    if self._dashboard_cache_epoch != cache_epoch:
                        return result
                    if len(self._dashboard_cache) >= 256:
                        self._dashboard_cache = {
                            item_key: item
                            for item_key, item in self._dashboard_cache.items()
                            if item[0] == generation
                            and item[1] == cache_epoch
                            and item[2] > now
                        }
                    self._dashboard_cache[key] = (
                        generation,
                        cache_epoch,
                        time.monotonic() + max(0.1, ttl_seconds),
                        deepcopy(result),
                    )
            return result

    def _audit_generation(self) -> int:
        with self._audit_refresh_lock:
            return self._audit_data_generation

    def invalidate_dashboard_cache(self) -> None:
        with self._dashboard_cache_lock:
            self._dashboard_cache_epoch += 1
            self._dashboard_cache.clear()

    def _audit_fingerprint(self) -> tuple[int, int] | None:
        try:
            stat = self.controller.paths.audit_log.stat()
        except FileNotFoundError:
            return None
        return stat.st_size, stat.st_mtime_ns

    def _audit_read_model_status_locked(self) -> dict[str, int | bool | str | None]:
        thread = self._audit_refresh_thread
        return {
            "ingestion_pending": thread is not None and thread.is_alive(),
            "data_generation": self._audit_data_generation,
            "imported_through_offset": int(self._audit_refresh_result.get("offset", 0)),
            "last_imported": int(self._audit_refresh_result.get("imported", 0)),
            "last_skipped": int(self._audit_refresh_result.get("skipped", 0)),
            "last_error_code": self._audit_refresh_error_code,
        }

    def _refresh_audit_background(self, fingerprint: tuple[int, int] | None) -> None:
        result: dict[str, int | bool] | None = None
        error_code: str | None = None
        try:
            result = self.refresh_audit()
        except (OSError, sqlite3.Error, ValueError) as error:
            error_code = type(error).__name__
        with self._audit_refresh_lock:
            if result is not None:
                self._audit_refresh_result = dict(result)
                if int(result.get("imported", 0)) > 0:
                    self._audit_data_generation += 1
                self._audit_completed_fingerprint = fingerprint
                self._audit_completed_fingerprint_known = True
            self._audit_refresh_error_code = error_code
            self._audit_refresh_thread = None

    def _schedule_semantics_migration(self) -> tuple[dict[str, int], bool]:
        if self._database_write_freeze.is_set():
            return dict(self._semantics_migration_result), False
        status = self.store.output_semantics_migration_status()
        if status.get("complete"):
            result = {**self._semantics_migration_result, "remaining": 0}
            return result, False
        with self._semantics_migration_lock:
            if self._database_write_freeze.is_set():
                return dict(self._semantics_migration_result), False
            if self._semantics_migration_thread is not None and self._semantics_migration_thread.is_alive():
                return dict(self._semantics_migration_result), True
            thread = threading.Thread(
                target=self._migrate_semantics_background,
                name="routerd-output-semantics-migration",
                daemon=True,
            )
            self._semantics_migration_thread = thread
            thread.start()
            return dict(self._semantics_migration_result), True

    def _migrate_semantics_background(self) -> None:
        totals = {"migrated": 0, "inferred": 0, "skipped": 0, "remaining": 0}
        try:
            while True:
                batch = self.store.migrate_output_semantics()
                for key in ("migrated", "inferred", "skipped"):
                    totals[key] += int(batch[key])
                totals["remaining"] = int(batch["remaining"])
                if batch.get("complete"):
                    break
                if self._database_write_freeze.wait(self._payload_import_delay_seconds):
                    break
        except (OSError, sqlite3.Error, ValueError):
            totals["skipped"] += 1
        with self._semantics_migration_lock:
            self._semantics_migration_result = totals
            self._semantics_migration_thread = None

    def _schedule_payload_index(self) -> tuple[dict[str, int], bool]:
        if self._database_write_freeze.is_set():
            return dict(self._payload_index_result), False
        status = self.store.payload_archive_status()
        if status.get("complete"):
            return dict(self._payload_index_result), False
        with self._payload_index_lock:
            if self._database_write_freeze.is_set():
                return dict(self._payload_index_result), False
            if self._payload_index_thread is not None and self._payload_index_thread.is_alive():
                return dict(self._payload_index_result), True
            thread = threading.Thread(
                target=self._index_payloads_background,
                name="routerd-payload-index",
                daemon=True,
            )
            self._payload_index_thread = thread
            thread.start()
            return dict(self._payload_index_result), True

    def _index_payloads_background(self) -> None:
        indexed = 0
        skipped = 0
        try:
            while True:
                batch = self.store.index_payload_archive()
                indexed += int(batch["indexed"])
                skipped += int(batch["skipped"])
                if batch.get("complete"):
                    break
                if self._database_write_freeze.wait(0.05):
                    break
        except (OSError, sqlite3.Error, ValueError):
            skipped += 1
        with self._payload_index_lock:
            self._payload_index_result = {"indexed": indexed, "skipped": skipped}
            self._payload_index_thread = None

    def _schedule_payload_import(self) -> tuple[dict[str, int], bool]:
        if not self._payload_import_enabled or self._database_write_freeze.is_set():
            return dict(self._payload_import_result), False
        with self._payload_import_lock:
            if self._database_write_freeze.is_set():
                return dict(self._payload_import_result), False
            if self._payload_import_thread is not None and self._payload_import_thread.is_alive():
                return dict(self._payload_import_result), True
            now = time.monotonic()
            if now - self._payload_import_last_started < 1.0:
                return dict(self._payload_import_result), False
            self._payload_import_last_started = now
            thread = threading.Thread(
                target=self._import_payloads_background,
                name="routerd-payload-import",
                daemon=True,
            )
            self._payload_import_thread = thread
            thread.start()
            return dict(self._payload_import_result), True

    def _import_payloads_background(self) -> None:
        imported = 0
        skipped = 0
        try:
            while True:
                batch = self.store.import_encrypted_payloads(max_records=1)
                imported += int(batch["imported"])
                skipped += int(batch["skipped"])
                if batch.get("complete"):
                    break
                # Historical payload imports must never monopolize the Dashboard process.
                if self._database_write_freeze.wait(self._payload_import_delay_seconds):
                    break
            result = {"imported": imported, "skipped": skipped}
        except Exception:
            result = {"imported": imported, "skipped": skipped}
        with self._payload_import_lock:
            self._payload_import_result = result
            self._payload_import_thread = None

    @contextmanager
    def freeze_database_writes(self, timeout: float):
        """Pause Dashboard-owned background writers while compaction reads a stable source."""
        if timeout <= 0:
            raise ValueError("Database write-freeze timeout must be positive.")
        with self._database_write_freeze_lock:
            self._database_write_freeze.set()
            try:
                deadline = time.monotonic() + timeout
                while True:
                    with self._audit_refresh_lock:
                        audit_thread = self._audit_refresh_thread
                    with self._payload_import_lock:
                        payload_import_thread = self._payload_import_thread
                    with self._payload_index_lock:
                        payload_index_thread = self._payload_index_thread
                    with self._semantics_migration_lock:
                        semantics_thread = self._semantics_migration_thread
                    active = [
                        thread
                        for thread in (
                            audit_thread,
                            payload_import_thread,
                            payload_index_thread,
                            semantics_thread,
                        )
                        if thread is not None and thread.is_alive()
                    ]
                    if not active:
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Timed out waiting for Dashboard database writers to stop.")
                    active[0].join(min(0.1, remaining))
                yield
            finally:
                self._database_write_freeze.clear()

    def overview(self) -> dict[str, Any]:
        imported = self.schedule_audit_refresh()
        status = self.controller.full_status()
        return {
            "timestamp": status["timestamp"],
            "mode": self._mode(status),
            "proxy": status["proxy"],
            "codex_config": status["codex_config"],
            "codex_mode": status["codex_mode"],
            "active_phase": status["active_phase"],
            "active_profile": status["active_profile"],
            "active_policy": status["active_policy"],
            "oauth": {"auth_file_exists": status["oauth"]["auth_file_exists"]},
            "database": self.store.health(),
            "codex_runtime": codex_runtime_diagnostics(),
            "import": imported,
            "quota": self.store.latest_quota(),
        }

    @staticmethod
    def _mode(status: dict[str, Any]) -> str:
        proxy = status.get("proxy") or {}
        codex_mode = status.get("codex_mode") or {}
        if proxy.get("running") and proxy.get("health", {}).get("ok") and codex_mode.get("client_mode") == "smart_router":
            return "routing_active"
        if proxy.get("running"):
            return "ready_native"
        if codex_mode.get("client_mode") == "config_drift":
            return "degraded"
        return "native"

    def start_proxy(self, *, allow_unverified: bool, request_id: str) -> dict[str, Any]:
        result = self.controller.start_proxy(confirm="APPLY", allow_unverified=allow_unverified)
        self.store.record_event(
            event_type="proxy_started" if result.get("ok") else "proxy_start_failed",
            component="proxy",
            severity="info" if result.get("ok") else "error",
            request_id=request_id,
            details={"allow_unverified": allow_unverified, "pid": result.get("pid")},
        )
        return result

    def activate_routing(self, *, allow_unverified: bool, request_id: str) -> dict[str, Any]:
        result = self.controller.enable_smart_routing(
            confirm="ENABLE_ROUTER",
            allow_unverified=allow_unverified,
        )
        self.store.record_event(
            event_type="routing_activated" if result.get("ok") else "routing_activation_failed",
            component="routing",
            severity="info" if result.get("ok") else "error",
            request_id=request_id,
            details={"generation": result.get("generation"), "proxy": result.get("proxy", {}).get("ok")},
        )
        return result

    def stop_proxy(self, *, request_id: str) -> dict[str, Any]:
        status = self.controller.full_status()
        if (status.get("codex_config") or {}).get("configured"):
            raise ControlError("Codex 仍指向本地 RouterLLM。请先执行“切回原生 Codex”，再停止 Proxy。")
        result = self.controller.stop_proxy(confirm="APPLY")
        self.store.record_event(
            event_type="proxy_stopped",
            component="proxy",
            request_id=request_id,
            details={"pid": result.get("stopped_pid")},
        )
        return result

    def restore_native(self, *, backup_path: str | None, request_id: str) -> dict[str, Any]:
        if backup_path:
            raise ControlError("完整备份恢复已不再用于切换原生 Codex；请使用灾难恢复 CLI。")
        result = self.controller.switch_to_native_codex(confirm="SWITCH_NATIVE")
        self.store.record_event(
            event_type="native_mode_switched",
            component="routing",
            request_id=request_id,
            details={"generation": result.get("generation"), "proxy_running": result.get("mode", {}).get("proxy_running")},
        )
        return result

    def codex_mode(self) -> dict[str, Any]:
        return self.controller.codex_mode_status()

    def enable_smart_mode(self, *, allow_unverified: bool, request_id: str) -> dict[str, Any]:
        return self.activate_routing(allow_unverified=allow_unverified, request_id=request_id)

    def enable_native_mode(self, *, request_id: str) -> dict[str, Any]:
        return self.restore_native(backup_path=None, request_id=request_id)

    def recover_session_unification(self, *, request_id: str) -> dict[str, Any]:
        result = self.enable_native_mode(request_id=request_id)
        self.store.record_event(
            event_type="session_unification_config_recovered",
            component="routing",
            request_id=request_id,
            details={"session_data_modified": False, "client_files_modified": False},
        )
        return {
            **result,
            "recovery": {
                "managed_config_restored": True,
                "session_data_modified": False,
                "migration_copies_preserved": True,
                "client_files_modified": False,
            },
        }

    def _latest_codex_backup(self) -> str | None:
        backup_items = self.controller.list_backups().get("backups") or []
        for item in backup_items:
            name = str(item.get("name") or "")
            path = str(item.get("path") or "")
            if name.startswith("codex-config-") and name.endswith(".toml.bak"):
                return path
        return None

    def refresh_quota(self, *, profile_id: str | None, request_id: str) -> dict[str, Any]:
        with self._quota_lock:
            snapshot = self.quota.read_snapshot(profile_id=profile_id)
            cached = self.store.latest_quota()
            if snapshot["status"] == "available":
                saved = self.store.record_quota_snapshot(snapshot)
            elif cached:
                saved = {
                    **cached,
                    "status": "stale",
                    "error_code": snapshot.get("error_code"),
                    "error_message": snapshot.get("error_message"),
                }
            else:
                saved = self.store.record_quota_snapshot(snapshot)
        self.store.record_event(
            event_type="quota_refreshed" if snapshot["status"] == "available" else "quota_unavailable",
            component="quota",
            severity="info" if snapshot["status"] == "available" else "warning",
            request_id=request_id,
            details={"status": snapshot["status"], "error_code": snapshot.get("error_code")},
        )
        return saved

    def subscription_overview(self, *, request_id: str, force: bool = False) -> dict[str, Any]:
        cached = self.store.latest_quota()
        # The overview endpoint is intentionally cache-first so the desktop can
        # render the last known quota before scheduling a slower live refresh.
        if cached is not None and not force:
            return {"snapshot": cached, "refresh_after_seconds": 300, "cache_state": "cached"}
        snapshot = self.refresh_quota(profile_id=None, request_id=request_id)
        return {"snapshot": snapshot, "refresh_after_seconds": 300, "cache_state": "refreshed"}

    def subscription_section(self, section: str, *, request_id: str, force: bool = False) -> dict[str, Any]:
        if section not in {"account", "limits", "usage"}:
            raise ControlError("Unknown subscription section.")
        cached = self.store.latest_quota()
        if cached is not None and not force:
            return {
                "snapshot": _quota_section_snapshot(cached, section),
                "refresh_after_seconds": 300,
                "cache_state": "cached",
            }

        reader = getattr(self.quota, f"read_{section}_section")
        live = reader(profile_id=None)
        # The App Server read can be slow, so it stays outside the quota lock.
        # Only the short read/merge/append operation is serialized. This makes
        # independently completed sections cumulative instead of allowing a
        # late account or usage response to persist an older limits snapshot.
        with self._quota_lock:
            latest = self.store.latest_quota()
            snapshot = _merge_quota_section(latest, live, section)
            if live.get("status") == "available":
                snapshot = self.store.record_quota_snapshot(snapshot)
            elif latest is not None:
                snapshot["status"] = "stale"
            else:
                snapshot = self.store.record_quota_snapshot(snapshot)
        return {
            "snapshot": _quota_section_snapshot(snapshot, section),
            "refresh_after_seconds": 300,
            "cache_state": "refreshed",
        }

    def start_subscription_login(self, *, request_id: str) -> dict[str, Any]:
        result = self.quota.start_login()
        self.store.record_event(
            event_type="subscription_login_started" if result.get("status") == "pending" else "subscription_login_failed",
            component="account",
            severity="info" if result.get("status") == "pending" else "warning",
            request_id=request_id,
            details={"status": result.get("status"), "error_code": result.get("error_code")},
        )
        return result

    def subscription_login_status(self, login_id: str, *, request_id: str) -> dict[str, Any]:
        result = self.quota.login_status(login_id)
        if result.get("status") == "completed":
            result["snapshot"] = self.refresh_quota(profile_id=None, request_id=request_id)
        return result

    def cancel_subscription_login(self, login_id: str, *, request_id: str) -> dict[str, Any]:
        result = self.quota.cancel_login(login_id)
        self.store.record_event(
            event_type="subscription_login_cancelled",
            component="account",
            severity="info",
            request_id=request_id,
            details={"status": result.get("status")},
        )
        return result

    def logout_subscription(self, *, request_id: str) -> dict[str, Any]:
        result = self.quota.logout()
        if result.get("status") == "logged_out":
            self.store.clear_account_profile()
        self.store.record_event(
            event_type="subscription_logged_out" if result.get("status") == "logged_out" else "subscription_logout_failed",
            component="account",
            severity="info" if result.get("status") == "logged_out" else "warning",
            request_id=request_id,
            details={"status": result.get("status"), "error_code": result.get("error_code")},
        )
        return result

    @staticmethod
    def _snapshot_is_stale(snapshot: dict[str, Any]) -> bool:
        observed_at = snapshot.get("observed_at")
        if not isinstance(observed_at, str):
            return True
        try:
            parsed = datetime.fromisoformat(observed_at)
        except ValueError:
            return True
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return datetime.now(UTC) - parsed > timedelta(minutes=5)

    def consume_reset(self, action: ResetCreditAction, *, request_id: str) -> dict[str, Any]:
        if not self.store.start_redemption(
            idempotency_key=action.idempotency_key,
            profile_id=action.profile_id,
            credit_id=None,
        ):
            return {"ok": True, "outcome": "already_submitted", "idempotency_key": action.idempotency_key}
        result = self.quota.consume_reset(
            idempotency_key=action.idempotency_key,
            credit_id=None,
        )
        self.store.finish_redemption(
            idempotency_key=action.idempotency_key,
            outcome=str(result.get("outcome") or "unknown"),
            error_code=result.get("error_code"),
        )
        self.store.record_event(
            event_type="reset_credit_consumed",
            component="quota",
            severity="info" if result.get("outcome") in {"reset", "alreadyRedeemed"} else "warning",
            request_id=request_id,
            details={"outcome": result.get("outcome")},
        )
        if result.get("outcome") in {"reset", "alreadyRedeemed"}:
            self.refresh_quota(profile_id=action.profile_id, request_id=request_id)
        return {"ok": result.get("outcome") != "error", **result}


def _quota_section_snapshot(snapshot: dict[str, Any], section: str) -> dict[str, Any]:
    """Return only the fields needed by one independently loading usage panel."""
    result: dict[str, Any] = {
        "profile_id": snapshot.get("profile_id"),
        "observed_at": snapshot.get("observed_at"),
        "status": snapshot.get("status"),
    }
    section_errors = snapshot.get("section_errors")
    if not isinstance(section_errors, dict):
        section_errors = snapshot.get("section_error")
    if isinstance(section_errors, dict) and section in section_errors:
        result["section_errors"] = {section: section_errors[section]}
        result["error_message"] = section_errors[section]
    summary = snapshot.get("summary") if isinstance(snapshot.get("summary"), dict) else {}
    if section == "account":
        result["account"] = snapshot.get("account")
        result["summary"] = {
            key: summary.get(key)
            for key in ("plan_type", "auth_mode", "account_label", "account_identifier", "account_state", "requires_openai_auth")
            if key in summary
        }
    elif section == "limits":
        result["limits"] = snapshot.get("limits")
        result["windows"] = snapshot.get("windows")
        result["reset_credits"] = snapshot.get("reset_credits")
        result["summary"] = {
            key: summary.get(key)
            for key in ("plan_type", "available_reset_credits", "credits")
            if key in summary
        }
    else:
        result["usage"] = snapshot.get("usage")
    return result


def _merge_quota_section(
    cached: dict[str, Any] | None,
    section: dict[str, Any],
    section_name: str,
) -> dict[str, Any]:
    """Merge one live section without erasing unrelated cached sections."""
    result = dict(cached or {})
    result.update(
        {
            key: section[key]
            for key in ("profile_id", "observed_at", "status", "error_code", "error_message")
            if key in section and section[key] is not None
        }
    )
    if isinstance(section.get("summary"), dict) and (
        section.get("status") == "available" or "summary" not in result
    ):
        summary = dict(result.get("summary") or {})
        summary.update(section["summary"])
        result["summary"] = summary
    owned_fields = {
        "account": ("account",),
        "limits": ("limits", "windows", "reset_credits"),
        "usage": ("usage",),
    }[section_name]
    for key in owned_fields:
        if key in section and (section.get("status") == "available" or key not in result):
            result[key] = section[key]
    section_errors = result.get("section_errors")
    if not isinstance(section_errors, dict):
        section_errors = result.get("section_error")
    errors = dict(section_errors) if isinstance(section_errors, dict) else {}
    if section.get("status") == "unavailable":
        errors[section_name] = section.get("error_message")
    else:
        errors.pop(section_name, None)
    result.pop("section_error", None)
    if errors:
        result["section_errors"] = errors
    else:
        result.pop("section_errors", None)
    return result


def create_dashboard_app(
    *,
    controller: RouterController | None = None,
    store: DashboardStore | None = None,
    quota: QuotaAdapter | None = None,
    access_token: str | None = None,
    build_id: str = "dev",
) -> FastAPI:
    controller = controller or RouterController()
    controller.paths.ensure_mutable()
    store = store or DashboardStore(
        controller.paths.dashboard_db,
        payload_path=controller.paths.encrypted_payload_log,
        payload_key=encryption_key(controller.paths, create=False),
    )
    service = DashboardService(controller, store, quota or QuotaService())
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        service.mode_switch.close()
        service.quota.close()

    app = FastAPI(
        title="CodexThrift RouterD",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.storage_service = service.storage
    app.state.dashboard_service = service
    app.state.access_token = access_token
    app.state.build_id = build_id

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "X-Request-Id"],
    )

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next: Any) -> Response:
        request.state.request_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        return response

    def require_access(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        token = app.state.access_token
        if token is None:
            return
        if authorization != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="RouterD access token is required.")

    def require_confirmation(action: ConfirmedAction) -> None:
        if not action.confirmed:
            raise HTTPException(status_code=400, detail="This action requires confirmed=true.")

    def debug_payload_section(
        call_id: int,
        section: str,
        request: Request,
        *,
        confirmed: bool,
        **section_options: Any,
    ) -> dict[str, Any]:
        audit_details = {"call_id": call_id, "read_type": section}
        if not confirmed:
            store.record_event(
                event_type="call_debug_read_rejected",
                component="dashboard_debug",
                severity="warning",
                request_id=request.state.request_id,
                details={**audit_details, "reason": "confirmation_required"},
            )
            raise HTTPException(status_code=400, detail="Debug read requires confirmed=true.")
        try:
            item = store.call_payload_section(call_id, section, **section_options)
        except ValueError as error:
            store.record_event(
                event_type="call_debug_read_failed",
                component="dashboard_debug",
                severity="warning",
                request_id=request.state.request_id,
                details={**audit_details, "reason": "invalid_request"},
            )
            raise HTTPException(status_code=400, detail=str(error)) from error
        if item is None:
            store.record_event(
                event_type="call_debug_read_failed",
                component="dashboard_debug",
                severity="warning",
                request_id=request.state.request_id,
                details={**audit_details, "reason": "call_not_found"},
            )
            raise HTTPException(status_code=404, detail="Call not found.")
        store.record_event(
            event_type="call_debug_read_succeeded",
            component="dashboard_debug",
            request_id=request.state.request_id,
            details={**audit_details, "result_state": item.get("state")},
        )
        return item

    def cost_window(start_at: datetime | None, end_at: datetime | None) -> tuple[str, str]:
        end = end_at.astimezone(UTC) if end_at else datetime.now(UTC)
        start = start_at.astimezone(UTC) if start_at else end - timedelta(hours=24)
        if start > end:
            raise HTTPException(status_code=400, detail="start_at must not be later than end_at.")
        return start.isoformat(), end.isoformat()

    @app.get("/healthz", dependencies=[Depends(require_access)])
    def healthz() -> dict[str, Any]:
        return {
            "ok": True,
            "api_schema_version": ROUTERD_API_SCHEMA_VERSION,
            "build_id": app.state.build_id,
            **store.health(),
        }

    @app.get("/v1/overview", dependencies=[Depends(require_access)])
    def overview() -> dict[str, Any]:
        return service.overview()

    @app.get("/v1/data/read-model", dependencies=[Depends(require_access)])
    def data_read_model() -> dict[str, int | bool | str | None]:
        return service.schedule_audit_refresh()

    @app.get("/v1/codex-mode", dependencies=[Depends(require_access)])
    def codex_mode() -> dict[str, Any]:
        return service.codex_mode()

    @app.get("/v1/codex-sessions/migration-preview", dependencies=[Depends(require_access)])
    def codex_session_migration_preview() -> dict[str, Any]:
        return service.session_migration.preview()

    @app.post("/v1/codex-sessions/migrate-next", dependencies=[Depends(require_access)])
    def codex_session_migrate_next(action: SessionMigrationAction) -> dict[str, Any]:
        return service.session_migration.migrate_next(confirm=action.confirm)

    @app.post("/v1/codex-sessions/recover-unification", dependencies=[Depends(require_access)])
    def codex_session_recover_unification(
        action: SessionMigrationAction,
        request: Request,
    ) -> dict[str, Any]:
        if action.confirm != "RESTORE_SESSION_UNIFICATION":
            raise HTTPException(
                status_code=400,
                detail="This action requires confirm='RESTORE_SESSION_UNIFICATION'.",
            )
        return _action_response(
            lambda: service.recover_session_unification(request_id=request.state.request_id)
        )

    @app.get("/v1/codex-mode/switch/preview", dependencies=[Depends(require_access)])
    def codex_mode_switch_preview(
        target: str = Query(pattern="^(native|routed)$"),
    ) -> dict[str, Any]:
        return service.mode_switch.preview(target=target)

    @app.get("/v1/codex-mode/switch/current", dependencies=[Depends(require_access)])
    def codex_mode_switch_current() -> dict[str, Any]:
        return service.mode_switch.current()

    @app.post("/v1/codex-mode/switch/jobs", dependencies=[Depends(require_access)])
    def codex_mode_switch_create(
        action: CodexModeSwitchCreateAction,
        request: Request,
    ) -> dict[str, Any]:
        return _action_response(
            lambda: service.create_mode_switch(action, request_id=request.state.request_id)
        )

    @app.get("/v1/codex-mode/switch/jobs/{job_id}", dependencies=[Depends(require_access)])
    def codex_mode_switch_job(job_id: str) -> dict[str, Any]:
        return service.mode_switch.status(job_id)

    @app.post("/v1/codex-mode/switch/jobs/{job_id}/cancel", dependencies=[Depends(require_access)])
    def codex_mode_switch_cancel(
        job_id: str,
        action: CodexModeSwitchCancelAction,
        request: Request,
    ) -> dict[str, Any]:
        return _action_response(
            lambda: service.cancel_mode_switch(
                job_id, action, request_id=request.state.request_id
            )
        )

    @app.post("/v1/codex-mode/switch/jobs/{job_id}/retry-clients", dependencies=[Depends(require_access)])
    def codex_mode_switch_retry_clients(
        job_id: str,
        action: CodexModeSwitchRetryAction,
        request: Request,
    ) -> dict[str, Any]:
        return _action_response(
            lambda: service.retry_mode_switch_clients(
                job_id, action, request_id=request.state.request_id
            )
        )

    @app.get("/v1/storage/overview", dependencies=[Depends(require_access)])
    def storage_overview() -> dict[str, Any]:
        return service.storage.overview()

    @app.post("/v1/storage/retention/preview", dependencies=[Depends(require_access)])
    def storage_retention_preview(action: StorageRetentionPreviewAction) -> dict[str, Any]:
        try:
            return service.storage.preview_retention(**action.model_dump())
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/v1/storage/retention/execute", dependencies=[Depends(require_access)])
    def storage_retention_execute(action: StorageRetentionExecuteAction) -> dict[str, Any]:
        try:
            return service.storage.execute_retention(**action.model_dump())
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/v1/storage/retention/jobs/{job_id}", dependencies=[Depends(require_access)])
    def storage_retention_job(job_id: str) -> dict[str, Any]:
        return service.storage.maintenance_status(job_id)

    @app.get("/v1/storage/retention/jobs/{job_id}/events", dependencies=[Depends(require_access)])
    def storage_retention_job_events(
        job_id: str,
        after_seq: int = Query(default=0, ge=0),
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> dict[str, Any]:
        try:
            return service.storage.maintenance_events(job_id, after_seq=after_seq, limit=limit)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/v1/storage/retention/jobs/{job_id}/cancel", dependencies=[Depends(require_access)])
    def storage_retention_cancel(job_id: str, action: StorageMaintenanceCancelAction) -> dict[str, Any]:
        try:
            return service.storage.cancel_maintenance(job_id=job_id, confirm=action.confirm)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/v1/storage/compaction/preview", dependencies=[Depends(require_access)])
    def storage_compaction_preview() -> dict[str, Any]:
        return service.storage.preview_compaction()

    @app.post("/v1/storage/compaction/execute", dependencies=[Depends(require_access)])
    def storage_compaction_execute(action: StorageCompactionExecuteAction) -> dict[str, Any]:
        try:
            return service.storage.execute_compaction(**action.model_dump())
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/v1/storage/archive-compression/preview", dependencies=[Depends(require_access)])
    def storage_archive_compression_preview() -> dict[str, Any]:
        return service.storage.preview_archive_compression()

    @app.post("/v1/storage/archive-compression/execute", dependencies=[Depends(require_access)])
    def storage_archive_compression_execute(
        action: StorageArchiveCompressionExecuteAction,
    ) -> dict[str, Any]:
        try:
            return service.storage.execute_archive_compression(**action.model_dump())
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/v1/storage/maintenance/settings", dependencies=[Depends(require_access)])
    def storage_maintenance_settings() -> dict[str, Any]:
        return service.storage.maintenance_settings()

    @app.post("/v1/storage/maintenance/settings", dependencies=[Depends(require_access)])
    def storage_maintenance_settings_update(action: StorageMaintenanceSettingsAction) -> dict[str, Any]:
        try:
            return service.storage.update_maintenance_settings(**action.model_dump())
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/v1/calls", dependencies=[Depends(require_access)])
    def calls(
        limit: int = Query(default=100, ge=1, le=500),
        status: str | None = None,
        model: str | None = None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        sort_by: str = Query(default="occurred_at"),
        sort_order: str = Query(default="desc", pattern="^(asc|desc)$"),
    ) -> dict[str, Any]:
        service.schedule_audit_refresh()
        start = start_at.astimezone(UTC).isoformat() if start_at else None
        end = end_at.astimezone(UTC).isoformat() if end_at else None
        return service.cached_dashboard_read(
            ("calls", limit, status, model, start, end, sort_by, sort_order),
            lambda: {"items": store.recent_calls(
                limit=limit,
                status=status,
                model=model,
                start_at=start,
                end_at=end,
                sort_by=sort_by,
                sort_order=sort_order,
            )},
        )

    @app.get("/v1/calls/{call_id}", dependencies=[Depends(require_access)])
    def call_detail(call_id: int) -> dict[str, Any]:
        service.schedule_audit_refresh()
        item = store.call_detail(call_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Call not found.")
        return item

    @app.get("/v1/content/index/status", dependencies=[Depends(require_access)])
    def content_index_status() -> dict[str, Any]:
        return store.payload_archive_status()

    @app.get("/v1/calls/{call_id}/input", dependencies=[Depends(require_access)])
    def call_input(
        call_id: int,
        origin: str | None = None,
        cursor: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        try:
            item = store.call_payload_section(
                call_id,
                "classified-input" if origin is not None else "input",
                origin=origin or "all",
                cursor=cursor,
                limit=limit,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        if item is None:
            raise HTTPException(status_code=404, detail="Call not found.")
        return item

    @app.get("/v1/calls/{call_id}/tool-results", dependencies=[Depends(require_access)])
    def call_tool_results(
        call_id: int,
        cursor: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        item = store.call_payload_section(call_id, "tool-results", cursor=cursor, limit=limit)
        if item is None:
            raise HTTPException(status_code=404, detail="Call not found.")
        return item

    @app.get("/v1/calls/{call_id}/user-input", dependencies=[Depends(require_access)])
    def call_user_input(call_id: int) -> dict[str, Any]:
        item = store.call_payload_section(call_id, "user-input")
        if item is None:
            raise HTTPException(status_code=404, detail="Call not found.")
        return item

    @app.get("/v1/calls/{call_id}/input-context", dependencies=[Depends(require_access)])
    def call_input_context(call_id: int) -> dict[str, Any]:
        item = store.call_payload_section(call_id, "input-context")
        if item is None:
            raise HTTPException(status_code=404, detail="Call not found.")
        return item

    @app.get("/v1/calls/{call_id}/output-items", dependencies=[Depends(require_access)])
    def call_output_items(call_id: int) -> dict[str, Any]:
        item = store.call_payload_section(call_id, "output-items")
        if item is None:
            raise HTTPException(status_code=404, detail="Call not found.")
        return item

    @app.get("/v1/calls/{call_id}/visible-output", dependencies=[Depends(require_access)])
    def call_visible_output(call_id: int) -> dict[str, Any]:
        item = store.call_payload_section(call_id, "visible-output")
        if item is None:
            raise HTTPException(status_code=404, detail="Call not found.")
        return item

    @app.get("/v1/calls/{call_id}/raw-request", dependencies=[Depends(require_access)])
    def call_raw_request(call_id: int, request: Request, confirmed: bool = False) -> dict[str, Any]:
        return debug_payload_section(call_id, "raw-request", request, confirmed=confirmed)

    @app.get("/v1/calls/{call_id}/normalized-output", dependencies=[Depends(require_access)])
    def call_normalized_output(call_id: int, request: Request, confirmed: bool = False) -> dict[str, Any]:
        return debug_payload_section(call_id, "normalized-output", request, confirmed=confirmed)

    @app.get("/v1/calls/{call_id}/terminal-response", dependencies=[Depends(require_access)])
    def call_terminal_response(call_id: int, request: Request, confirmed: bool = False) -> dict[str, Any]:
        return debug_payload_section(call_id, "terminal-response", request, confirmed=confirmed)

    @app.get("/v1/calls/{call_id}/stream-events", dependencies=[Depends(require_access)])
    def call_stream_events(
        call_id: int,
        request: Request,
        cursor: int = Query(default=0, ge=0),
        limit: int = Query(default=200, ge=1, le=500),
        type: str = Query(default="all"),
        confirmed: bool = False,
    ) -> dict[str, Any]:
        return debug_payload_section(
            call_id,
            "stream-events",
            request,
            confirmed=confirmed,
            cursor=cursor,
            limit=limit,
            event_type=type,
        )

    @app.get("/v1/calls/{call_id}/export", dependencies=[Depends(require_access)])
    def call_export(
        call_id: int,
        request: Request,
        level: str = Query(default="summary", pattern="^(summary|debug)$"),
        confirmed: bool = False,
    ) -> dict[str, Any]:
        if level == "debug" and not confirmed:
            store.record_event(
                event_type="call_debug_export_rejected",
                component="dashboard_export",
                severity="warning",
                request_id=request.state.request_id,
                details={"call_id": call_id, "level": level, "reason": "confirmation_required"},
            )
            raise HTTPException(status_code=400, detail="Debug export requires confirmed=true.")
        try:
            item = store.call_export(call_id, level=level)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        if item is None:
            raise HTTPException(status_code=404, detail="Call not found.")
        if level == "debug":
            store.record_event(
                event_type="call_debug_exported",
                component="dashboard_export",
                request_id=request.state.request_id,
                details={"call_id": call_id, "level": level, "result_state": item.get("debug", {}).get("state")},
            )
        return item

    @app.get("/v1/costs/summary", dependencies=[Depends(require_access)])
    def cost_summary(
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        model: str | None = None,
        billing_mode: str | None = None,
        estimate_status: str | None = None,
    ) -> dict[str, Any]:
        service.schedule_audit_refresh()
        start, end = cost_window(start_at, end_at)
        return service.cached_dashboard_read(
            ("cost_summary", start, end, model, billing_mode, estimate_status),
            lambda: store.cost_summary(
                start_at=start,
                end_at=end,
                model=model,
                billing_mode=billing_mode,
                estimate_status=estimate_status,
            ),
        )

    @app.get("/v1/costs/timeline", dependencies=[Depends(require_access)])
    def cost_timeline(
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        timezone: str = Query(default="UTC", min_length=1, max_length=100),
        bucket: str = Query(default="auto", pattern="^(auto|5m|hour|day|week)$"),
        model: str | None = None,
        billing_mode: str | None = None,
        estimate_status: str | None = None,
    ) -> dict[str, Any]:
        service.schedule_audit_refresh()
        start, end = cost_window(start_at, end_at)
        try:
            return service.cached_dashboard_read(
                ("cost_timeline", start, end, timezone, bucket, model, billing_mode, estimate_status),
                lambda: {
                    "items": store.cost_timeline(
                        start_at=start,
                        end_at=end,
                        timezone_name=timezone,
                        bucket=bucket,
                        model=model,
                        billing_mode=billing_mode,
                        estimate_status=estimate_status,
                    ),
                    "start_at": start,
                    "end_at": end,
                    "timezone": timezone,
                    "bucket": bucket,
                },
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/v1/costs/models", dependencies=[Depends(require_access)])
    def cost_models(
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        billing_mode: str | None = None,
        estimate_status: str | None = None,
    ) -> dict[str, Any]:
        service.schedule_audit_refresh()
        start, end = cost_window(start_at, end_at)
        return service.cached_dashboard_read(
            ("cost_models", start, end, billing_mode, estimate_status),
            lambda: {"items": store.cost_models(
                start_at=start,
                end_at=end,
                billing_mode=billing_mode,
                estimate_status=estimate_status,
            )},
        )

    @app.get("/v1/costs/calls", dependencies=[Depends(require_access)])
    def cost_calls(
        limit: int = Query(default=100, ge=1, le=200),
        cursor: str | None = None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        model: str | None = None,
        billing_mode: str | None = None,
        estimate_status: str | None = None,
        sort_by: str = Query(default="occurred_at"),
        sort_order: str = Query(default="desc", pattern="^(asc|desc)$"),
    ) -> dict[str, Any]:
        service.schedule_audit_refresh()
        start, end = cost_window(start_at, end_at)
        try:
            return service.cached_dashboard_read(
                (
                    "cost_calls", start, end, limit, cursor, model,
                    billing_mode, estimate_status, sort_by, sort_order,
                ),
                lambda: store.cost_calls(
                    start_at=start,
                    end_at=end,
                    limit=limit,
                    cursor=cursor,
                    model=model,
                    billing_mode=billing_mode,
                    estimate_status=estimate_status,
                    sort_by=sort_by,
                    sort_order=sort_order,
                ),
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/v1/costs/calls/{call_id}", dependencies=[Depends(require_access)])
    def cost_call_detail(call_id: int) -> dict[str, Any]:
        service.schedule_audit_refresh()
        item = store.cost_call_detail(call_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Call not found.")
        return item

    @app.get("/v1/pricing/catalog", dependencies=[Depends(require_access)])
    def pricing_catalog() -> dict[str, Any]:
        return {"items": store.pricing_catalog()}

    @app.get("/v1/pricing/rules", dependencies=[Depends(require_access)])
    def pricing_rules() -> dict[str, Any]:
        return {"items": store.pricing_rules()}

    @app.post("/v1/pricing/rules", dependencies=[Depends(require_access)])
    def save_pricing_rule(action: PricingRuleAction) -> dict[str, Any]:
        result = store.upsert_pricing_rule(
            model_pattern=action.model_pattern,
            input_microusd_per_million=round(action.input_usd_per_million * 1_000_000),
            output_microusd_per_million=round(action.output_usd_per_million * 1_000_000),
        )
        service.invalidate_dashboard_cache()
        return result

    @app.get("/v1/turns", dependencies=[Depends(require_access)])
    def turns(
        limit: int = Query(default=100, ge=1, le=500),
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        sort_by: str = Query(default="total_tokens"),
        sort_order: str = Query(default="desc", pattern="^(asc|desc)$"),
    ) -> dict[str, Any]:
        service.schedule_audit_refresh()
        return {"items": store.turns(
            limit=limit,
            start_at=start_at.astimezone(UTC).isoformat() if start_at else None,
            end_at=end_at.astimezone(UTC).isoformat() if end_at else None,
            sort_by=sort_by,
            sort_order=sort_order,
        )}

    @app.get("/v1/tasks", dependencies=[Depends(require_access)])
    def tasks(
        limit: int = Query(default=100, ge=1, le=200),
        cursor: str | None = None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        sort_by: str = Query(default="last_activity_at"),
        sort_order: str = Query(default="desc", pattern="^(asc|desc)$"),
    ) -> dict[str, Any]:
        service.schedule_audit_refresh()
        start = start_at.astimezone(UTC).isoformat() if start_at else None
        end = end_at.astimezone(UTC).isoformat() if end_at else None
        try:
            return service.cached_dashboard_read(
                ("tasks", limit, cursor, start, end, sort_by, sort_order),
                lambda: store.tasks(
                    limit=limit,
                    cursor=cursor,
                    start_at=start,
                    end_at=end,
                    sort_by=sort_by,
                    sort_order=sort_order,
                ),
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/v1/tasks/{task_key}", dependencies=[Depends(require_access)])
    def task_detail(task_key: str) -> dict[str, Any]:
        service.schedule_audit_refresh()
        item = store.task_detail(task_key)
        if item is None:
            raise HTTPException(status_code=404, detail="Task not found.")
        return item

    @app.get("/v1/routing/stats", dependencies=[Depends(require_access)])
    def routing_stats(
        limit: int = Query(default=200, ge=1, le=500),
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        sort_by: str = Query(default="call_count"),
        sort_order: str = Query(default="desc", pattern="^(asc|desc)$"),
    ) -> dict[str, Any]:
        service.schedule_audit_refresh()
        start = start_at.astimezone(UTC).isoformat() if start_at else None
        end = end_at.astimezone(UTC).isoformat() if end_at else None
        return service.cached_dashboard_read(
            ("routing_stats", limit, start, end, sort_by, sort_order),
            lambda: {"items": store.routing_stats(
                limit=limit,
                start_at=start,
                end_at=end,
                sort_by=sort_by,
                sort_order=sort_order,
            )},
        )

    @app.get("/v1/analytics/daily", dependencies=[Depends(require_access)])
    def daily(days: int = Query(default=30, ge=1, le=365)) -> dict[str, Any]:
        service.schedule_audit_refresh()
        return service.cached_dashboard_read(
            ("daily", days),
            lambda: {"items": store.daily_metrics(days=days)},
        )

    @app.get("/v1/analytics/weekly", dependencies=[Depends(require_access)])
    def weekly(weeks: int = Query(default=12, ge=1, le=52)) -> dict[str, Any]:
        service.schedule_audit_refresh()
        return service.cached_dashboard_read(
            ("weekly", weeks),
            lambda: {"items": store.weekly_metrics(weeks=weeks)},
        )

    @app.get("/v1/events", dependencies=[Depends(require_access)])
    def events(limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
        return {"items": store.recent_events(limit=limit)}

    @app.get("/v1/quota", dependencies=[Depends(require_access)])
    def quota_snapshot() -> dict[str, Any]:
        return {"snapshot": store.latest_quota()}

    @app.post("/v1/quota/refresh", dependencies=[Depends(require_access)])
    def refresh_quota(request: Request) -> dict[str, Any]:
        return service.refresh_quota(profile_id=None, request_id=request.state.request_id)

    @app.get("/v1/subscription/overview", dependencies=[Depends(require_access)])
    def subscription_overview(request: Request) -> dict[str, Any]:
        return service.subscription_overview(request_id=request.state.request_id)

    @app.post("/v1/subscription/refresh", dependencies=[Depends(require_access)])
    def subscription_refresh(request: Request) -> dict[str, Any]:
        return service.subscription_overview(request_id=request.state.request_id, force=True)

    @app.post("/v1/subscription/login/start", dependencies=[Depends(require_access)])
    def subscription_login_start(request: Request) -> dict[str, Any]:
        return service.start_subscription_login(request_id=request.state.request_id)

    @app.post("/v1/subscription/login/status", dependencies=[Depends(require_access)])
    def subscription_login_status(action: LoginStatusAction, request: Request) -> dict[str, Any]:
        return service.subscription_login_status(action.login_id, request_id=request.state.request_id)

    @app.post("/v1/subscription/login/cancel", dependencies=[Depends(require_access)])
    def subscription_login_cancel(action: LoginStatusAction, request: Request) -> dict[str, Any]:
        return service.cancel_subscription_login(action.login_id, request_id=request.state.request_id)

    @app.post("/v1/subscription/logout", dependencies=[Depends(require_access)])
    def subscription_logout(action: LogoutAction, request: Request) -> dict[str, Any]:
        if not action.confirmed:
            raise HTTPException(status_code=400, detail="This action requires confirmed=true.")
        return service.logout_subscription(request_id=request.state.request_id)

    @app.get("/v1/subscription/{section}", dependencies=[Depends(require_access)])
    def subscription_section(section: str, request: Request) -> dict[str, Any]:
        return service.subscription_section(section, request_id=request.state.request_id)

    @app.post("/v1/subscription/{section}", dependencies=[Depends(require_access)])
    def subscription_section_refresh(section: str, request: Request) -> dict[str, Any]:
        return service.subscription_section(section, request_id=request.state.request_id, force=True)

    @app.post("/v1/actions/proxy/start", dependencies=[Depends(require_access)])
    def start_proxy(action: ConfirmedAction, request: Request) -> dict[str, Any]:
        require_confirmation(action)
        return _action_response(lambda: service.start_proxy(
            allow_unverified=action.allow_unverified, request_id=request.state.request_id
        ))

    @app.post("/v1/actions/routing/activate", dependencies=[Depends(require_access)])
    def activate_routing(action: ConfirmedAction, request: Request) -> dict[str, Any]:
        require_confirmation(action)
        return _action_response(lambda: service.activate_routing(
            allow_unverified=action.allow_unverified, request_id=request.state.request_id
        ))

    @app.post("/v1/codex-mode/smart", dependencies=[Depends(require_access)])
    def enable_smart_mode(action: CodexModeAction, request: Request) -> dict[str, Any]:
        if action.confirm != "ENABLE_ROUTER":
            raise HTTPException(status_code=400, detail="This action requires confirm='ENABLE_ROUTER'.")
        return _action_response(lambda: service.enable_smart_mode(
            allow_unverified=action.allow_unverified,
            request_id=request.state.request_id,
        ))

    @app.post("/v1/actions/proxy/stop", dependencies=[Depends(require_access)])
    def stop_proxy(action: ConfirmedAction, request: Request) -> dict[str, Any]:
        require_confirmation(action)
        return _action_response(lambda: service.stop_proxy(request_id=request.state.request_id))

    @app.post("/v1/actions/routing/restore-native", dependencies=[Depends(require_access)])
    def restore_native(action: RestoreNativeAction, request: Request) -> dict[str, Any]:
        if not action.confirmed:
            raise HTTPException(status_code=400, detail="This action requires confirmed=true.")
        return _action_response(lambda: service.restore_native(
            backup_path=action.backup_path, request_id=request.state.request_id
        ))

    @app.post("/v1/codex-mode/native", dependencies=[Depends(require_access)])
    def enable_native_mode(action: CodexModeAction, request: Request) -> dict[str, Any]:
        if action.confirm != "SWITCH_NATIVE":
            raise HTTPException(status_code=400, detail="This action requires confirm='SWITCH_NATIVE'.")
        return _action_response(lambda: service.enable_native_mode(request_id=request.state.request_id))

    @app.post("/v1/actions/quota/consume-reset", dependencies=[Depends(require_access)])
    def consume_reset(action: ResetCreditAction, request: Request) -> dict[str, Any]:
        if not action.confirmed:
            raise HTTPException(status_code=400, detail="This action requires confirmed=true.")
        return service.consume_reset(action, request_id=request.state.request_id)

    @app.get("/v1/backups", dependencies=[Depends(require_access)])
    def backups() -> dict[str, Any]:
        return controller.list_backups()

    @app.post("/v1/actions/launch-agent/install", dependencies=[Depends(require_access)])
    def launch_agent_install(action: ConfirmedAction, request: Request) -> dict[str, Any]:
        require_confirmation(action)
        executable = os.environ.get("CODEXTHRIFT_DAEMON_EXECUTABLE")
        if not executable:
            raise HTTPException(status_code=409, detail="Packaged codexthriftd executable is not configured.")
        return _action_response(lambda: install_launch_agent(
            plist_path=controller.paths.launch_agent_plist,
            executable=executable,
            working_directory=controller.paths.root,
            log_path=controller.paths.daemon_log,
        ))

    @app.post("/v1/actions/launch-agent/uninstall", dependencies=[Depends(require_access)])
    def launch_agent_uninstall(action: ConfirmedAction) -> dict[str, Any]:
        require_confirmation(action)
        return _action_response(lambda: uninstall_launch_agent(plist_path=controller.paths.launch_agent_plist))

    return app


def _action_response(operation: Any) -> dict[str, Any]:
    try:
        return operation()
    except ControlError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
