from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager, closing, nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..content_capture import (
    ARCHIVE_FORMAT_V2_IDENTITY,
    ARCHIVE_FORMAT_V2_ZLIB,
    decrypt_payload,
    encrypt_archive_payload,
)
from ..dashboard_store import DashboardStore
from ..paths import WorkspacePaths
from ..payload_archive import read_payload_envelope
from ..storage.atomic_io import atomic_write_json, read_json
from ..util import atomic_write_text

GIB = 1024 ** 3
PREVIEW_TTL_SECONDS = 600
DEFAULT_MAINTENANCE_SETTINGS = {
    "enabled": False,
    "limit_gib": 10.0,
    "cleanup_ratio": 0.8,
    "minimum_retention_hours": 168,
    "delete_mode": "payload_only",
    "check_interval_hours": 24,
}
MAINTENANCE_EVENT_MAX_BYTES = 2 * 1024 * 1024
MAINTENANCE_EVENT_BACKUPS = 3
MAINTENANCE_LOG_MAX_BYTES = 5 * 1024 * 1024
MAINTENANCE_LOG_BACKUPS = 3
_EVENT_METRIC_SUFFIXES = ("_bytes", "_count", "_seconds")
_EVENT_METRIC_NAMES = {
    "processed",
    "imported",
    "skipped",
    "indexed",
    "migrated",
    "inferred",
    "remaining",
}


def _bounded_directory_size(
    root: Path,
    *,
    max_entries: int = 10_000,
    max_seconds: float = 0.05,
) -> dict[str, Any]:
    if not root.exists():
        return {"bytes": 0, "entries": 0, "complete": True}
    started = time.monotonic()
    total = 0
    entries = 0
    stack = [root]
    complete = True
    while stack:
        directory = stack.pop()
        try:
            children = list(os.scandir(directory))
        except OSError:
            continue
        for child in children:
            entries += 1
            if entries > max_entries or time.monotonic() - started >= max_seconds:
                complete = False
                stack.clear()
                break
            try:
                if child.is_symlink():
                    continue
                if child.is_dir(follow_symlinks=False):
                    stack.append(Path(child.path))
                elif child.is_file(follow_symlinks=False):
                    total += child.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    return {"bytes": total, "entries": entries, "complete": complete}


class StorageMaintenanceService:
    """Read-only storage inventory and bounded retention-bucket backfill."""

    def __init__(
        self,
        paths: WorkspacePaths,
        store: DashboardStore,
        controller: Any,
        *,
        database_write_freeze: Callable[[float], AbstractContextManager[None]] | None = None,
    ) -> None:
        self.paths = paths
        self.store = store
        self._controller = controller
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_result: dict[str, Any] = {"processed": 0, "complete": False}
        self._job_thread: threading.Thread | None = None
        self._active_job_id: str | None = None
        self._cancel_requested = False
        self._maintenance_lock_path = self.paths.storage_maintenance_dir / "maintenance.lock"
        self._settings_path = self.paths.storage_maintenance_dir / "settings.json"
        self._automatic_check_path = self.paths.storage_maintenance_dir / "automatic-check.json"
        self._policy_thread: threading.Thread | None = None
        self._database_write_freeze = database_write_freeze or (lambda _timeout: nullcontext())
        self._event_lock = threading.Lock()
        self._recover_maintenance_lock()

    def overview(self) -> dict[str, Any]:
        bucket_status = self.store.storage_bucket_status()
        pending = self._schedule_bucket_refresh() if not bucket_status["complete"] else False
        database = self.store.storage_overview()
        cache = _bounded_directory_size(self.paths.cache_dir)
        logs = _bounded_directory_size(self.paths.logs_dir)
        backups = _bounded_directory_size(self.paths.backups_dir)
        archive_status = self.store.payload_archive_status()
        settings = self.maintenance_settings()
        automatic_check = self.automatic_check_status()
        if settings["enabled"] and bucket_status["complete"] and archive_status["complete"]:
            self._schedule_automatic_check(settings, automatic_check)
        return {
            "data_root": str(self.paths.root),
            "database": database,
            "payload_archive": {
                **archive_status,
                "path": str(self.paths.encrypted_payload_log),
            },
            "cache": {"path": str(self.paths.cache_dir), **cache},
            "logs": {"path": str(self.paths.logs_dir), **logs},
            "backups": {"path": str(self.paths.backups_dir), **backups},
            "retention_buckets": {
                **bucket_status,
                "pending": pending,
                "last_batch": dict(self._last_result),
            },
            "maintenance": self.maintenance_status(),
            "maintenance_settings": settings,
            "automatic_check": automatic_check,
            "data_location": self.paths.data_location_status(),
        }

    def preview_retention(
        self,
        *,
        limit_gib: float,
        cleanup_ratio: float,
        minimum_retention_hours: int,
        delete_mode: str,
        persist: bool = True,
    ) -> dict[str, Any]:
        if limit_gib < 0.5 or limit_gib > 1024:
            raise ValueError("limit_gib must be between 0.5 and 1024.")
        if cleanup_ratio < 0.5 or cleanup_ratio > 0.95:
            raise ValueError("cleanup_ratio must be between 0.5 and 0.95.")
        if minimum_retention_hours < 1 or minimum_retention_hours > 24 * 365:
            raise ValueError("minimum_retention_hours must be between 1 and 8760.")
        if delete_mode not in {"payload_only", "full_call"}:
            raise ValueError("delete_mode must be payload_only or full_call.")

        bucket_status = self.store.storage_bucket_status()
        archive_status = self.store.payload_archive_status()
        if not bucket_status["complete"] or not archive_status["complete"]:
            return {
                "state": "indexing_required",
                "reason": "storage_index_incomplete",
                "retention_buckets": bucket_status,
                "payload_archive": archive_status,
            }

        database = self.store.storage_overview()
        archive_bytes = int(archive_status["file_size"])
        current_total = int(database["bytes"]) + int(database["wal_bytes"]) + archive_bytes
        limit_bytes = int(limit_gib * GIB)
        target_bytes = int(limit_bytes * cleanup_ratio)
        needs_cleanup = current_total > limit_bytes
        required_release = max(0, current_total - target_bytes) if needs_cleanup else 0
        minimum_cutoff = datetime.now(UTC) - timedelta(hours=minimum_retention_hours)

        selected: list[dict[str, Any]] = []
        released = 0
        for bucket in self.store.storage_retention_buckets():
            bucket_end = datetime.fromisoformat(str(bucket["bucket_end"]))
            if bucket_end > minimum_cutoff:
                break
            bucket_release = int(bucket["embedded_payload_bytes"]) + int(bucket["archive_payload_bytes"])
            if delete_mode == "full_call":
                bucket_release += int(bucket["call_count"]) * 2048
            selected.append(bucket)
            released += bucket_release
            if released >= required_release:
                break

        cutoff_at = selected[-1]["bucket_end"] if selected else None
        achievable = not needs_cleanup or released >= required_release
        estimated_after = max(0, current_total - released)
        retained_archive = max(0, archive_bytes - sum(int(row["archive_payload_bytes"]) for row in selected))
        retained_database = max(
            0,
            int(database["bytes"]) - sum(int(row["embedded_payload_bytes"]) for row in selected),
        )
        archive_release = sum(int(row["archive_payload_bytes"]) for row in selected)
        sqlite_reclaimable = sum(int(row["embedded_payload_bytes"]) for row in selected)
        temporary_bytes = int((retained_archive + retained_database) * 1.2)
        now = datetime.now(UTC)
        preview_id = uuid.uuid4().hex
        preview = {
            "preview_id": preview_id,
            "state": "ready" if achievable else "retention_limit_blocked",
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=PREVIEW_TTL_SECONDS)).isoformat(),
            "settings": {
                "limit_gib": limit_gib,
                "limit_bytes": limit_bytes,
                "cleanup_ratio": cleanup_ratio,
                "target_bytes": target_bytes,
                "minimum_retention_hours": minimum_retention_hours,
                "delete_mode": delete_mode,
            },
            "current": {
                "total_bytes": current_total,
                "database_bytes": int(database["bytes"]),
                "wal_bytes": int(database["wal_bytes"]),
                "archive_bytes": archive_bytes,
            },
            "plan": {
                "needs_cleanup": needs_cleanup,
                "achievable": achievable,
                "cutoff_at": cutoff_at,
                "minimum_cutoff_at": minimum_cutoff.isoformat(),
                "bucket_count": len(selected),
                "call_count": sum(int(row["call_count"]) for row in selected),
                "embedded_payload_count": sum(int(row["embedded_payload_count"]) for row in selected),
                "archive_payload_count": sum(int(row["archive_payload_count"]) for row in selected),
                "estimated_release_bytes": released,
                "estimated_after_bytes": estimated_after,
                "estimated_immediate_release_bytes": archive_release,
                "estimated_sqlite_reclaimable_bytes": sqlite_reclaimable,
                "estimated_after_retention_bytes": max(0, current_total - archive_release),
                "estimated_after_compaction_bytes": max(
                    0,
                    current_total - archive_release - sqlite_reclaimable,
                ),
                "temporary_space_required_bytes": temporary_bytes,
            },
            "source": self._source_fingerprint(database, archive_status, bucket_status),
        }
        if persist:
            self._write_preview(preview)
        return preview

    def maintenance_settings(self) -> dict[str, Any]:
        stored = read_json(self._settings_path, {})
        settings = {**DEFAULT_MAINTENANCE_SETTINGS}
        if isinstance(stored, dict):
            for key in settings:
                if key in stored:
                    settings[key] = stored[key]
        return settings

    def update_maintenance_settings(
        self,
        *,
        enabled: bool,
        limit_gib: float,
        cleanup_ratio: float,
        minimum_retention_hours: int,
        delete_mode: str,
        check_interval_hours: int,
    ) -> dict[str, Any]:
        if limit_gib < 0.5 or limit_gib > 1024:
            raise ValueError("limit_gib must be between 0.5 and 1024.")
        if cleanup_ratio < 0.5 or cleanup_ratio > 0.95:
            raise ValueError("cleanup_ratio must be between 0.5 and 0.95.")
        if minimum_retention_hours < 1 or minimum_retention_hours > 24 * 365:
            raise ValueError("minimum_retention_hours must be between 1 and 8760.")
        if delete_mode not in {"payload_only", "full_call"}:
            raise ValueError("delete_mode must be payload_only or full_call.")
        if check_interval_hours < 1 or check_interval_hours > 24 * 30:
            raise ValueError("check_interval_hours must be between 1 and 720.")
        settings = {
            "enabled": bool(enabled),
            "limit_gib": float(limit_gib),
            "cleanup_ratio": float(cleanup_ratio),
            "minimum_retention_hours": int(minimum_retention_hours),
            "delete_mode": delete_mode,
            "check_interval_hours": int(check_interval_hours),
        }
        atomic_write_json(self._settings_path, settings, mode=0o600)
        if settings["enabled"]:
            self._schedule_automatic_check(settings, {}, force=True)
        return {"settings": settings, "automatic_check": self.automatic_check_status()}

    def automatic_check_status(self) -> dict[str, Any]:
        value = read_json(self._automatic_check_path, {})
        return value if isinstance(value, dict) else {}

    def execute_retention(self, *, preview_id: str, confirm: str) -> dict[str, Any]:
        preview = self._load_preview(preview_id)
        if preview.get("state") != "ready" or not preview.get("plan", {}).get("needs_cleanup"):
            raise ValueError("The retention preview does not require an executable cleanup.")
        delete_mode = str(preview.get("settings", {}).get("delete_mode") or "")
        required_confirmation = {
            "payload_only": "DELETE_OLD_CALL_DATA",
            "full_call": "DELETE_COMPLETE_CALL_HISTORY",
        }.get(delete_mode)
        if required_confirmation is None:
            raise ValueError("The retention preview has an unsupported delete mode.")
        if confirm != required_confirmation:
            raise ValueError(f"This action requires confirm='{required_confirmation}'.")
        self._validate_preview(preview)
        with self._lock:
            if self._job_thread is not None and self._job_thread.is_alive():
                raise ValueError("A storage maintenance job is already running.")
            job_id = uuid.uuid4().hex
            self._active_job_id = job_id
            self._cancel_requested = False
            manifest = {
                "job_id": job_id,
                "operation": "full_call_retention" if delete_mode == "full_call" else "payload_retention",
                "preview_id": preview_id,
                "state": "queued",
                "stage": "queued",
                "created_at": datetime.now(UTC).isoformat(),
                "updated_at": datetime.now(UTC).isoformat(),
                "cutoff_at": preview["plan"]["cutoff_at"],
                "delete_mode": delete_mode,
            }
            try:
                self._acquire_maintenance_lock(manifest)
                manifest = self._create_job(manifest)
                self._job_thread = threading.Thread(
                    target=self._run_payload_cleanup,
                    args=(preview, manifest),
                    name=f"codexthrift-retention-{job_id}",
                    daemon=True,
                )
                self._job_thread.start()
            except Exception:
                self._active_job_id = None
                self._job_thread = None
                self._cancel_requested = False
                self._release_maintenance_lock(job_id)
                raise
        return manifest

    def preview_compaction(self) -> dict[str, Any]:
        archive_identity = self.store.reconcile_payload_archive_identity()
        database = self.store.storage_overview()
        projection = self.store.payload_projection_audit()
        projection["archive_identity_reconciliation"] = archive_identity
        database_path = Path(database["path"])
        disk = shutil.disk_usage(database_path.parent)
        live_bytes = max(
            int(database["page_size"]) * max(0, int(database["page_count"]) - int(database["freelist_count"])),
            4096,
        )
        removable_payload_bytes = int(projection["removable_payload_bytes"])
        estimated_compacted_bytes = max(4096, live_bytes - removable_payload_bytes)
        # VACUUM INTO first creates a live-size copy. Compacting that copy can
        # temporarily require another final-size SQLite file at the same time.
        temporary_space_required = live_bytes + estimated_compacted_bytes + 128 * 1024 * 1024
        now = datetime.now(UTC)
        if projection["state"] not in {"ready", "ready_with_retained_items", "not_needed"}:
            state = str(projection["state"])
        elif disk.free < temporary_space_required:
            state = "insufficient_space"
        else:
            state = "ready_with_retained_items" if projection["state"] == "ready_with_retained_items" else "ready"
        preview = {
            "preview_id": uuid.uuid4().hex,
            "state": state,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=PREVIEW_TTL_SECONDS)).isoformat(),
            "database": database,
            "projection": projection,
            "estimated_compacted_bytes": estimated_compacted_bytes,
            "estimated_release_bytes": max(
                0,
                int(database["bytes"]) + int(database["wal_bytes"]) - estimated_compacted_bytes,
            ),
            "temporary_space_required_bytes": temporary_space_required,
            "available_bytes": disk.free,
            "source": self._database_fingerprint(database_path),
            "projection_source": self._projection_fingerprint(projection),
        }
        self._write_compaction_preview(preview)
        return preview

    def preview_archive_compression(self) -> dict[str, Any]:
        self.store.reconcile_payload_archive_identity()
        archive_status = self.store.payload_archive_status()
        inventory = self.store.payload_archive_compression_inventory()
        archive_path = self.paths.encrypted_payload_log
        archive_size = int(inventory.get("archive_size") or 0)
        eligible_bytes = int(inventory.get("eligible_archive_bytes") or 0)
        sampled_bytes = 0
        sampled_rewritten_bytes = 0
        sampled_legacy_count = 0
        sampled_v2_count = 0
        sampled_unreadable_count = 0
        for sample in inventory.pop("samples", []):
            try:
                envelope = read_payload_envelope(
                    Path(str(sample["source_path"])),
                    byte_offset=int(sample["byte_offset"]),
                    byte_length=int(sample["byte_length"]),
                    expected_payload_ref=str(sample["payload_ref"]),
                )
                sampled_bytes += int(sample["byte_length"])
                if envelope.get("format") in {ARCHIVE_FORMAT_V2_IDENTITY, ARCHIVE_FORMAT_V2_ZLIB}:
                    sampled_v2_count += 1
                    sampled_rewritten_bytes += int(sample["byte_length"])
                    continue
                if self.store.payload_key is None:
                    raise ValueError("Payload key is unavailable.")
                plaintext = decrypt_payload(self.store.payload_key, envelope)
                rewritten = {
                    "payload_ref": str(sample["payload_ref"]),
                    **encrypt_archive_payload(self.store.payload_key, plaintext),
                }
                sampled_rewritten_bytes += len(self._encoded_archive_line(rewritten))
                sampled_legacy_count += 1
            except Exception:
                sampled_unreadable_count += 1
                sampled_rewritten_bytes += int(sample["byte_length"])
        ratio = min(1.0, sampled_rewritten_bytes / sampled_bytes) if sampled_bytes else 1.0
        estimated_eligible_bytes = int(eligible_bytes * ratio)
        estimated_archive_bytes = max(0, archive_size - eligible_bytes + estimated_eligible_bytes)
        temporary_space_required = archive_size + 64 * 1024 * 1024
        available_bytes = shutil.disk_usage(archive_path.parent).free if archive_path.parent.exists() else 0
        if not inventory.get("available"):
            state = "archive_unavailable"
        elif not archive_status.get("complete"):
            state = "indexing_required"
        elif self.store.payload_key is None:
            state = "key_unavailable"
        elif int(inventory.get("eligible_payload_count") or 0) == 0:
            state = "no_eligible_payloads"
        elif sampled_bytes and estimated_archive_bytes >= archive_size and not sampled_unreadable_count:
            state = "no_eligible_payloads"
        elif available_bytes < temporary_space_required:
            state = "insufficient_space"
        else:
            state = "ready"
        now = datetime.now(UTC)
        preview = {
            "preview_id": uuid.uuid4().hex,
            "state": state,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=PREVIEW_TTL_SECONDS)).isoformat(),
            "archive": {
                **inventory,
                "complete": bool(archive_status.get("complete")),
                "path": str(archive_path),
            },
            "sample": {
                "bytes": sampled_bytes,
                "rewritten_bytes": sampled_rewritten_bytes,
                "legacy_payload_count": sampled_legacy_count,
                "v2_payload_count": sampled_v2_count,
                "unreadable_payload_count": sampled_unreadable_count,
                "estimated_ratio": ratio,
            },
            "estimated_archive_bytes": estimated_archive_bytes,
            "estimated_release_bytes": max(0, archive_size - estimated_archive_bytes),
            "temporary_space_required_bytes": temporary_space_required,
            "available_bytes": available_bytes,
            "source": self._archive_compression_fingerprint(inventory),
        }
        self._write_archive_compression_preview(preview)
        return preview

    def execute_compaction(
        self,
        *,
        preview_id: str,
        confirmed: bool = False,
        confirm: str | None = None,
    ) -> dict[str, Any]:
        if not confirmed and confirm != "COMPACT_SQLITE_DATABASE":
            raise ValueError("SQLite compaction requires confirmed=true.")
        mode = self._controller.codex_mode_status()
        if mode.get("client_mode") != "native_openai":
            raise ValueError("codex_mode_required")
        preview = self._load_compaction_preview(preview_id)
        if preview.get("state") not in {"ready", "ready_with_retained_items"}:
            raise ValueError("The compaction preview is not executable.")
        # Do not silently replace the user's checkpoint when normal Core activity
        # changes the database after preview generation.
        if self._database_fingerprint(self.store.database_path) != preview.get("source"):
            raise ValueError("preview_stale")
        with self._lock:
            if self._job_thread is not None and self._job_thread.is_alive():
                raise ValueError("A storage maintenance job is already running.")
            job_id = uuid.uuid4().hex
            self._active_job_id = job_id
            self._cancel_requested = False
            manifest = {
                "job_id": job_id,
                "operation": "sqlite_compaction",
                "preview_id": preview_id,
                "state": "queued",
                "stage": "queued",
                "created_at": datetime.now(UTC).isoformat(),
                "updated_at": datetime.now(UTC).isoformat(),
            }
            try:
                self._acquire_maintenance_lock(manifest)
                manifest = self._create_job(manifest)
                self._job_thread = threading.Thread(
                    target=self._run_database_compaction,
                    args=(preview, manifest),
                    name=f"codexthrift-compaction-{job_id}",
                    daemon=True,
                )
                self._job_thread.start()
            except Exception:
                self._active_job_id = None
                self._job_thread = None
                self._cancel_requested = False
                self._release_maintenance_lock(job_id)
                raise
        return manifest

    def execute_archive_compression(self, *, preview_id: str, confirmed: bool = False) -> dict[str, Any]:
        if not confirmed:
            raise ValueError("Archive compression requires confirmed=true.")
        mode = self._controller.codex_mode_status()
        if mode.get("client_mode") != "native_openai":
            raise ValueError("codex_mode_required")
        preview = self._load_archive_compression_preview(preview_id)
        if preview.get("state") != "ready":
            raise ValueError("The archive compression preview is not executable.")
        inventory = self.store.payload_archive_compression_inventory()
        inventory.pop("samples", None)
        if self._archive_compression_fingerprint(inventory) != preview.get("source"):
            raise ValueError("preview_stale")
        with self._lock:
            if self._job_thread is not None and self._job_thread.is_alive():
                raise ValueError("A storage maintenance job is already running.")
            job_id = uuid.uuid4().hex
            self._active_job_id = job_id
            self._cancel_requested = False
            manifest = {
                "job_id": job_id,
                "operation": "archive_compression",
                "preview_id": preview_id,
                "state": "queued",
                "stage": "queued",
                "created_at": datetime.now(UTC).isoformat(),
                "updated_at": datetime.now(UTC).isoformat(),
            }
            try:
                self._acquire_maintenance_lock(manifest)
                manifest = self._create_job(manifest)
                self._job_thread = threading.Thread(
                    target=self._run_archive_compression,
                    args=(preview, manifest),
                    name=f"codexthrift-archive-compression-{job_id}",
                    daemon=True,
                )
                self._job_thread.start()
            except Exception:
                self._active_job_id = None
                self._job_thread = None
                self._cancel_requested = False
                self._release_maintenance_lock(job_id)
                raise
        return manifest

    def maintenance_status(self, job_id: str | None = None) -> dict[str, Any]:
        selected = job_id or self._active_job_id
        if selected:
            manifest = read_json(self._job_path(selected), None)
            if isinstance(manifest, dict):
                return manifest
        return {"state": "idle", "active_job_id": None}

    def maintenance_events(
        self,
        job_id: str,
        *,
        after_seq: int = 0,
        limit: int = 200,
    ) -> dict[str, Any]:
        if not job_id or any(character not in "0123456789abcdef" for character in job_id):
            raise ValueError("Invalid job_id.")
        if after_seq < 0:
            raise ValueError("after_seq must not be negative.")
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000.")
        items: list[dict[str, Any]] = []
        paths = [
            self._job_events_path(job_id).with_name(f"{self._job_events_path(job_id).name}.{index}")
            for index in range(MAINTENANCE_EVENT_BACKUPS, 0, -1)
        ]
        paths.append(self._job_events_path(job_id))
        for path in paths:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                continue
            for line in lines:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(item, dict) or int(item.get("seq") or 0) <= after_seq:
                    continue
                items.append(item)
        items.sort(key=lambda item: int(item["seq"]))
        has_more = len(items) > limit
        selected = items[:limit]
        next_after_seq = int(selected[-1]["seq"]) if selected else after_seq
        return {
            "job_id": job_id,
            "items": selected,
            "next_after_seq": next_after_seq,
            "has_more": has_more,
        }

    def cancel_maintenance(self, *, job_id: str, confirm: str) -> dict[str, Any]:
        if confirm != "CANCEL_STORAGE_MAINTENANCE":
            raise ValueError("This action requires confirm='CANCEL_STORAGE_MAINTENANCE'.")
        manifest = self.maintenance_status(job_id)
        if manifest.get("state") not in {"queued", "running"}:
            return manifest
        if manifest.get("stage") in {"replace_archive", "update_database", "replace_database", "completed"}:
            raise ValueError("This maintenance stage can no longer be canceled.")
        self._cancel_requested = True
        return {**manifest, "cancel_requested": True}

    def _load_preview(self, preview_id: str) -> dict[str, Any]:
        if not preview_id or any(character not in "0123456789abcdef" for character in preview_id):
            raise ValueError("Invalid preview_id.")
        preview = read_json(self.paths.storage_maintenance_dir / f"preview-{preview_id}.json", None)
        if not isinstance(preview, dict):
            raise ValueError("Retention preview was not found.")
        expires_at = datetime.fromisoformat(str(preview["expires_at"]))
        if expires_at <= datetime.now(UTC):
            raise ValueError("Retention preview has expired.")
        return preview

    def _validate_preview(self, preview: dict[str, Any]) -> None:
        database = self.store.storage_overview()
        archive = self.store.payload_archive_status()
        buckets = self.store.storage_bucket_status()
        current = self._source_fingerprint(database, archive, buckets)
        if current != preview.get("source"):
            raise ValueError("Storage changed after preview; calculate a new retention preview.")

    def _run_payload_cleanup(self, preview: dict[str, Any], manifest: dict[str, Any]) -> None:
        proxy_was_running = False
        archive_backup: Path | None = None
        archive_temp: Path | None = None
        try:
            manifest = self._update_job(manifest, state="running", stage="pause_proxy")
            proxy_status = self._controller.proxy_status()
            proxy_was_running = bool(proxy_status.get("running"))
            if proxy_was_running:
                self._controller.services.proxy.stop_proxy(confirm="APPLY")
            if self._cancel_requested:
                raise InterruptedError("Storage maintenance was canceled.")

            cutoff_at = str(preview["plan"]["cutoff_at"])
            delete_mode = str(preview["settings"]["delete_mode"])
            if delete_mode == "full_call":
                deleted = {
                    str(item["payload_ref"])
                    for item in self.store.full_call_retention_refs(cutoff_at)
                    if item.get("payload_ref")
                }
            else:
                preflight = self.store.retention_payload_preflight(cutoff_at)
                manifest = self._update_job(
                    manifest,
                    stage="validate_retention_candidates",
                    candidate_call_count=int(preflight["candidate_call_count"]),
                    unique_payload_count=int(preflight["unique_payload_count"]),
                    duplicate_reference_count=int(preflight["duplicate_reference_count"]),
                    protected_shared_payload_count=int(preflight["protected_shared_payload_count"]),
                )
                deleted = {str(item["payload_ref"]) for item in preflight["payloads"]}
            archive_temp, entries = self._rewrite_archive(deleted, manifest)
            if self._cancel_requested:
                raise InterruptedError("Storage maintenance was canceled.")

            archive_path = self.paths.encrypted_payload_log
            source_archive_bytes = archive_path.stat().st_size if archive_path.exists() else 0
            manifest = self._update_job(manifest, stage="replace_archive")
            if archive_path.exists():
                archive_backup = self.paths.storage_maintenance_dir / f"archive-{manifest['job_id']}.bak"
                os.replace(archive_path, archive_backup)
            if archive_temp is not None:
                os.replace(archive_temp, archive_path)
            archive_stat = archive_path.stat() if archive_path.exists() else None

            manifest = self._update_job(manifest, stage="update_database")
            if delete_mode == "full_call":
                result = self.store.apply_full_call_retention(
                    cutoff_at=cutoff_at,
                    job_id=str(manifest["job_id"]),
                    archive_entries=entries,
                    archive_stat=archive_stat,
                )
            else:
                result = self.store.apply_payload_retention(
                    cutoff_at=cutoff_at,
                    job_id=str(manifest["job_id"]),
                    archive_entries=entries,
                    archive_stat=archive_stat,
                )
                final_archive_bytes = archive_path.stat().st_size if archive_path.exists() else 0
                result.update(
                    {
                        "source_archive_bytes": source_archive_bytes,
                        "final_archive_bytes": final_archive_bytes,
                        "released_bytes": max(0, source_archive_bytes - final_archive_bytes),
                    }
                )
            if archive_backup is not None:
                archive_backup.unlink(missing_ok=True)
            manifest = self._update_job(
                manifest,
                state="completed",
                stage="completed",
                completed_at=datetime.now(UTC).isoformat(),
                result=result,
            )
        except InterruptedError:
            if archive_temp is not None:
                archive_temp.unlink(missing_ok=True)
            self._update_job(manifest, state="canceled", stage="canceled", error_code="canceled")
        except Exception as error:
            archive_path = self.paths.encrypted_payload_log
            if archive_backup is not None and archive_backup.exists():
                if archive_path.exists():
                    archive_path.unlink()
                os.replace(archive_backup, archive_path)
            if archive_temp is not None:
                archive_temp.unlink(missing_ok=True)
            self._update_job(
                manifest,
                state="failed",
                stage="failed",
                failed_stage=manifest.get("stage"),
                error_code=type(error).__name__,
            )
        finally:
            if proxy_was_running:
                try:
                    self._controller.services.proxy.start_proxy(confirm="APPLY", allow_unverified=True)
                except Exception:
                    pass
            with self._lock:
                self._active_job_id = None
                self._job_thread = None
                self._cancel_requested = False
            self._release_maintenance_lock(str(manifest["job_id"]))

    def _run_database_compaction(self, preview: dict[str, Any], manifest: dict[str, Any]) -> None:
        proxy_was_running = False
        database_path = self.store.database_path
        temp_path = self.paths.storage_maintenance_dir / f"database-{manifest['job_id']}.sqlite3.tmp"
        backup_path = self.paths.storage_maintenance_dir / f"database-{manifest['job_id']}.sqlite3.bak"
        try:
            manifest = self._update_job(manifest, state="running", stage="pause_proxy")
            proxy_was_running = bool(self._controller.proxy_status().get("running"))
            if proxy_was_running:
                self._controller.services.proxy.stop_proxy(confirm="APPLY")
            if self._cancel_requested:
                raise InterruptedError("Storage maintenance was canceled.")

            # Dashboard reads stay available during the expensive build. Only
            # Dashboard-owned writers are frozen, and the store is exclusive
            # for the short snapshot and pathname-swap boundaries.
            with self._database_write_freeze(30):
                with self.store.exclusive_database_maintenance(timeout=30):
                    manifest = self._update_job(manifest, stage="checkpoint_database")
                    with closing(sqlite3.connect(database_path, timeout=30)) as connection:
                        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                    checkpoint_busy = int(checkpoint[0]) if checkpoint else 0
                    if checkpoint_busy:
                        raise RuntimeError("Database checkpoint remained busy.")
                    source_fingerprint = self._database_fingerprint(database_path)
                    source_check = self._database_health(database_path)
                    if source_check["quick_check"] != "ok" or source_check["foreign_key_errors"]:
                        raise RuntimeError("Source database integrity check failed.")
                    current_projection = self.store.payload_projection_audit()
                    if current_projection["state"] not in {"ready", "ready_with_retained_items", "not_needed"}:
                        raise RuntimeError("Archive projection is not executable.")
                    if self._projection_fingerprint(current_projection) != preview.get("projection_source"):
                        raise RuntimeError("Payload projection changed after preview.")
                    source_table_counts = self._database_table_counts(database_path)

                    current_database = self.store.storage_overview()
                    live_bytes = max(
                        int(current_database["page_size"])
                        * max(0, int(current_database["page_count"]) - int(current_database["freelist_count"])),
                        4096,
                    )
                    estimated_compacted_bytes = max(
                        4096,
                        live_bytes - int(current_projection["removable_payload_bytes"]),
                    )
                    required = live_bytes + estimated_compacted_bytes + 128 * 1024 * 1024
                    disk = shutil.disk_usage(database_path.parent)
                    if disk.free < required:
                        raise RuntimeError("Insufficient temporary disk space.")
                    if self._cancel_requested:
                        raise InterruptedError("Storage maintenance was canceled.")

                manifest = self._update_job(
                    manifest,
                    stage="vacuum_into",
                    source_bytes=database_path.stat().st_size,
                    temporary_space_required_bytes=required,
                )
                quoted_temp = str(temp_path).replace("'", "''")
                with closing(sqlite3.connect(database_path, timeout=30)) as connection:
                    connection.set_progress_handler(lambda: 1 if self._cancel_requested else 0, 10_000)
                    try:
                        connection.execute(f"VACUUM INTO '{quoted_temp}'")
                    except sqlite3.OperationalError as error:
                        if self._cancel_requested:
                            raise InterruptedError("Storage maintenance was canceled.") from error
                        raise
                if self._cancel_requested:
                    raise InterruptedError("Storage maintenance was canceled.")

                manifest = self._update_job(manifest, stage="verify_archive_coverage")
                verified = self._verify_embedded_payload_archive_copies(temp_path, manifest)
                if (
                    int(verified["payload_count"])
                    + int(verified["retained_payload_count"])
                    != int(current_projection["embedded_payload_count"])
                ):
                    raise RuntimeError("Archive verification count changed during compaction.")
                if self._cancel_requested:
                    raise InterruptedError("Storage maintenance was canceled.")

                manifest = self._update_job(
                    manifest,
                    stage="remove_embedded_payload_copies",
                    verified_payload_count=int(verified["payload_count"]),
                    verified_payload_bytes=int(verified["payload_bytes"]),
                    retained_payload_count=int(verified["retained_payload_count"]),
                    retained_payload_bytes=int(verified["retained_payload_bytes"]),
                )
                remaining_space_required = estimated_compacted_bytes + 64 * 1024 * 1024
                if shutil.disk_usage(temp_path.parent).free < remaining_space_required:
                    raise RuntimeError("Insufficient temporary disk space for final compaction.")
                with closing(sqlite3.connect(temp_path, timeout=30, isolation_level=None)) as connection:
                    connection.execute("PRAGMA foreign_keys=ON")
                    payload_ids = list(verified["payload_ids"])
                    for offset in range(0, len(payload_ids), 500):
                        batch = payload_ids[offset:offset + 500]
                        placeholders = ",".join("?" for _ in batch)
                        connection.execute(
                            f"DELETE FROM call_payloads WHERE id IN ({placeholders})",
                            batch,
                        )
                    if self._cancel_requested:
                        raise InterruptedError("Storage maintenance was canceled.")
                    manifest = self._update_job(
                        manifest,
                        stage="compact_temporary_database",
                        remaining_space_required_bytes=remaining_space_required,
                    )
                    connection.set_progress_handler(lambda: 1 if self._cancel_requested else 0, 10_000)
                    try:
                        connection.execute("VACUUM")
                    except sqlite3.OperationalError as error:
                        if self._cancel_requested:
                            raise InterruptedError("Storage maintenance was canceled.") from error
                        raise
                    connection.set_progress_handler(None, 0)
                    connection.execute("ANALYZE")
                    connection.execute("PRAGMA optimize")

                manifest = self._update_job(
                    manifest,
                    stage="validate_compacted_database",
                    compacted_bytes=temp_path.stat().st_size,
                )
                compacted_check = self._database_health(temp_path)
                if compacted_check["quick_check"] != "ok" or compacted_check["foreign_key_errors"]:
                    raise RuntimeError("Compacted database integrity check failed.")
                compacted_table_counts = self._database_table_counts(temp_path)
                if compacted_table_counts != source_table_counts:
                    raise RuntimeError("Non-payload table counts changed during compaction.")

                if self._database_fingerprint(database_path) != source_fingerprint:
                    raise RuntimeError("Database changed during compaction.")
                with self.store.exclusive_database_maintenance(timeout=30):
                    if self._database_fingerprint(database_path) != source_fingerprint:
                        raise RuntimeError("Database changed before replacement.")
                    manifest = self._update_job(manifest, stage="replace_database")
                    try:
                        # The store-wide maintenance lease proves that every ordinary
                        # SQLite connection is closed before either pathname changes.
                        os.replace(database_path, backup_path)
                        os.replace(temp_path, database_path)
                        self._remove_sqlite_sidecars(database_path)
                        with closing(sqlite3.connect(database_path, timeout=30)) as connection:
                            connection.execute("PRAGMA journal_mode=WAL")
                            connection.execute("PRAGMA optimize")
                        final_check = self._database_health(database_path)
                        if final_check["quick_check"] != "ok" or final_check["foreign_key_errors"]:
                            raise RuntimeError("Final database integrity check failed.")
                        if int(verified["payload_count"]):
                            archive_canary = self.store.payload_archive_canary_status(limit=3)
                            if not archive_canary["passed"]:
                                raise RuntimeError("Archive detail canary failed after replacement.")
                        else:
                            archive_canary = {"tested": 0, "available": 0, "passed": True}
                        backup_path.unlink(missing_ok=True)
                    except Exception:
                        temp_path.unlink(missing_ok=True)
                        if backup_path.exists():
                            if database_path.exists():
                                database_path.unlink()
                            self._remove_sqlite_sidecars(database_path)
                            os.replace(backup_path, database_path)
                        raise

            final_bytes = database_path.stat().st_size
            manifest = self._update_job(
                manifest,
                state="completed",
                stage="completed",
                completed_at=datetime.now(UTC).isoformat(),
                result={
                    "source_bytes": int(manifest.get("source_bytes") or 0),
                    "compacted_bytes": final_bytes,
                    "released_bytes": max(0, int(manifest.get("source_bytes") or 0) - final_bytes),
                    "quick_check": final_check["quick_check"],
                    "foreign_key_errors": len(final_check["foreign_key_errors"]),
                    "embedded_payloads_removed": int(verified["payload_count"]),
                    "embedded_payload_bytes_removed": int(verified["payload_bytes"]),
                    "embedded_payloads_retained": int(verified["retained_payload_count"]),
                    "embedded_payload_bytes_retained": int(verified["retained_payload_bytes"]),
                    "archive_verified_payloads": int(verified["payload_count"]),
                    "archive_verified_bytes": int(verified["archive_bytes"]),
                    "archive_canary_tested": int(archive_canary["tested"]),
                    "archive_canary_available": int(archive_canary["available"]),
                    "source_inode": int(source_fingerprint["inode"]),
                    "compacted_inode": database_path.stat().st_ino,
                },
            )
        except InterruptedError:
            temp_path.unlink(missing_ok=True)
            self._update_job(manifest, state="canceled", stage="canceled", error_code="canceled")
        except Exception as error:
            temp_path.unlink(missing_ok=True)
            if backup_path.exists():
                with self.store.exclusive_database_maintenance(timeout=30):
                    if database_path.exists():
                        database_path.unlink()
                    self._remove_sqlite_sidecars(database_path)
                    os.replace(backup_path, database_path)
            self._update_job(
                manifest,
                state="failed",
                stage="failed",
                failed_stage=manifest.get("stage"),
                error_code=type(error).__name__,
            )
        finally:
            if proxy_was_running:
                try:
                    self._controller.services.proxy.start_proxy(confirm="APPLY", allow_unverified=True)
                except Exception:
                    pass
            with self._lock:
                self._active_job_id = None
                self._job_thread = None
                self._cancel_requested = False
            self._release_maintenance_lock(str(manifest["job_id"]))

    def _run_archive_compression(self, preview: dict[str, Any], manifest: dict[str, Any]) -> None:
        proxy_was_running = False
        archive_path = self.paths.encrypted_payload_log
        temp_path = self.paths.storage_maintenance_dir / f"archive-compressed-{manifest['job_id']}.tmp"
        backup_path = self.paths.storage_maintenance_dir / f"archive-compressed-{manifest['job_id']}.bak"
        original_entries: list[dict[str, Any]] = []
        replacement_started = False
        try:
            manifest = self._update_job(manifest, state="running", stage="pause_proxy")
            proxy_was_running = bool(self._controller.proxy_status().get("running"))
            if proxy_was_running:
                self._controller.services.proxy.stop_proxy(confirm="APPLY")
            if self._cancel_requested:
                raise InterruptedError("Storage maintenance was canceled.")

            with self.store.exclusive_database_maintenance(timeout=30):
                inventory = self.store.payload_archive_compression_inventory()
                inventory.pop("samples", None)
                if self._archive_compression_fingerprint(inventory) != preview.get("source"):
                    raise RuntimeError("Archive changed after preview.")
                if self.store.payload_key is None:
                    raise RuntimeError("Payload key is unavailable.")
                original_entries = self.store.payload_archive_index_entries()
                manifest = self._update_job(manifest, stage="compress_archive", source_bytes=archive_path.stat().st_size)
                rewritten = self._rewrite_compressed_archive(
                    temp_path,
                    manifest,
                    indexed_entries=original_entries,
                )
                if self._cancel_requested:
                    raise InterruptedError("Storage maintenance was canceled.")
                manifest = self._update_job(
                    manifest,
                    stage="validate_compressed_archive",
                    compacted_bytes=temp_path.stat().st_size,
                    compressed_payload_count=rewritten["compressed_payload_count"],
                    already_compressed_payload_count=rewritten["already_compressed_payload_count"],
                    embedded_payload_count=rewritten["embedded_payload_count"],
                    unreadable_payload_count=rewritten["unreadable_payload_count"],
                )
                if int(rewritten["entry_count"]) != len(original_entries):
                    raise RuntimeError("Archive entry count changed during compression.")
                if int(rewritten["compressed_payload_count"]) == 0:
                    temp_path.unlink(missing_ok=True)
                    result = {
                        **{key: value for key, value in rewritten.items() if key != "entries"},
                        "source_bytes": archive_path.stat().st_size,
                        "compacted_bytes": archive_path.stat().st_size,
                        "released_bytes": 0,
                        "source_inode": archive_path.stat().st_ino,
                        "compacted_inode": archive_path.stat().st_ino,
                        "archive_canary_tested": 0,
                        "archive_canary_available": 0,
                    }
                else:
                    manifest = self._update_job(manifest, stage="replace_archive")
                    source_stat = archive_path.stat()
                    os.replace(archive_path, backup_path)
                    replacement_started = True
                    os.replace(temp_path, archive_path)
                    archive_stat = archive_path.stat()
                    manifest = self._update_job(manifest, stage="update_database")
                    self.store.replace_payload_archive_index(rewritten["entries"], archive_stat)
                    canary = self.store.payload_archive_canary_status(limit=3)
                    if canary["tested"] and not canary["passed"]:
                        raise RuntimeError("Archive detail canary failed after compression.")
                    backup_path.unlink(missing_ok=True)
                    replacement_started = False
                    result = {
                        key: value for key, value in rewritten.items() if key != "entries"
                    }
                    result.update({
                        "source_bytes": source_stat.st_size,
                        "compacted_bytes": archive_stat.st_size,
                        "released_bytes": max(0, source_stat.st_size - archive_stat.st_size),
                        "source_inode": source_stat.st_ino,
                        "compacted_inode": archive_stat.st_ino,
                        "archive_canary_tested": int(canary["tested"]),
                        "archive_canary_available": int(canary["available"]),
                    })
            self._update_job(
                manifest,
                state="completed",
                stage="completed",
                completed_at=datetime.now(UTC).isoformat(),
                result=result,
            )
        except InterruptedError:
            temp_path.unlink(missing_ok=True)
            self._update_job(manifest, state="canceled", stage="canceled", error_code="canceled")
        except Exception as error:
            temp_path.unlink(missing_ok=True)
            if replacement_started and backup_path.exists():
                if archive_path.exists():
                    archive_path.unlink()
                os.replace(backup_path, archive_path)
                if original_entries:
                    self.store.replace_payload_archive_index(original_entries, archive_path.stat())
            self._update_job(
                manifest,
                state="failed",
                stage="failed",
                failed_stage=manifest.get("stage"),
                error_code=type(error).__name__,
            )
        finally:
            if proxy_was_running:
                try:
                    self._controller.services.proxy.start_proxy(confirm="APPLY", allow_unverified=True)
                except Exception:
                    pass
            with self._lock:
                self._active_job_id = None
                self._job_thread = None
                self._cancel_requested = False
            self._release_maintenance_lock(str(manifest["job_id"]))

    def _rewrite_compressed_archive(
        self,
        temp_path: Path,
        manifest: dict[str, Any],
        *,
        indexed_entries: list[dict[str, Any]],
    ) -> dict[str, Any]:
        archive_path = self.paths.encrypted_payload_log
        key = self.store.payload_key
        if key is None:
            raise RuntimeError("Payload key is unavailable.")
        embedded_refs = self.store.embedded_payload_refs()
        indexed_locations = {
            str(entry["payload_ref"]): (
                int(entry["byte_offset"]),
                int(entry["byte_length"]),
            )
            for entry in indexed_entries
        }
        entries: list[dict[str, Any]] = []
        processed_bytes = 0
        written_bytes = 0
        compressed_payload_count = 0
        already_compressed_payload_count = 0
        embedded_payload_count = 0
        unreadable_payload_count = 0
        unindexed_payload_count = 0
        with archive_path.open("rb") as source, temp_path.open("wb") as target:
            while True:
                source_offset = source.tell()
                raw_line = source.readline()
                if not raw_line:
                    break
                processed_bytes += len(raw_line)
                output_line = raw_line
                payload_ref: str | None = None
                is_indexed_entry = False
                try:
                    envelope = json.loads(raw_line)
                    candidate = envelope.get("payload_ref") if isinstance(envelope, dict) else None
                    payload_ref = candidate if isinstance(candidate, str) and candidate else None
                    if payload_ref is None or not raw_line.endswith(b"\n"):
                        raise ValueError("Archive envelope is invalid.")
                    is_indexed_entry = indexed_locations.get(payload_ref) == (
                        source_offset,
                        len(raw_line),
                    )
                    if not is_indexed_entry:
                        # Historical archives may contain an older duplicate for a
                        # payload_ref. The live index intentionally points at one
                        # exact line, so preserve every other line byte-for-byte
                        # without treating it as an additional indexed payload.
                        unindexed_payload_count += 1
                    elif payload_ref in embedded_refs:
                        embedded_payload_count += 1
                    elif envelope.get("format") in {ARCHIVE_FORMAT_V2_IDENTITY, ARCHIVE_FORMAT_V2_ZLIB}:
                        already_compressed_payload_count += 1
                    else:
                        plaintext = decrypt_payload(key, envelope)
                        decoded = json.loads(plaintext)
                        if not isinstance(decoded, dict) or decoded.get("payload_ref") != payload_ref:
                            raise ValueError("Archive plaintext reference mismatch.")
                        rewritten = {"payload_ref": payload_ref, **encrypt_archive_payload(key, plaintext)}
                        if decrypt_payload(key, rewritten) != plaintext:
                            raise ValueError("Archive compression round trip failed.")
                        output_line = self._encoded_archive_line(rewritten)
                        compressed_payload_count += 1
                except Exception:
                    unreadable_payload_count += 1
                    output_line = raw_line
                offset = target.tell()
                target.write(output_line)
                written_bytes += len(output_line)
                if is_indexed_entry and payload_ref is not None and output_line.endswith(b"\n"):
                    entries.append({
                        "payload_ref": payload_ref,
                        "byte_offset": offset,
                        "byte_length": len(output_line),
                    })
                if self._cancel_requested:
                    break
                if processed_bytes % (64 * 1024 * 1024) < len(raw_line):
                    self._update_job(
                        manifest,
                        stage="compress_archive",
                        processed_bytes=processed_bytes,
                        retained_bytes=written_bytes,
                        compressed_payload_count=compressed_payload_count,
                    )
            target.flush()
            os.fsync(target.fileno())
        return {
            "entries": entries,
            "entry_count": len(entries),
            "processed_bytes": processed_bytes,
            "written_bytes": written_bytes,
            "compressed_payload_count": compressed_payload_count,
            "already_compressed_payload_count": already_compressed_payload_count,
            "embedded_payload_count": embedded_payload_count,
            "unreadable_payload_count": unreadable_payload_count,
            "unindexed_payload_count": unindexed_payload_count,
        }

    def _rewrite_archive(
        self,
        deleted_refs: set[str],
        manifest: dict[str, Any],
    ) -> tuple[Path | None, list[dict[str, Any]]]:
        archive_path = self.paths.encrypted_payload_log
        if not archive_path.exists():
            return None, []
        temp_path = self.paths.storage_maintenance_dir / f"archive-{manifest['job_id']}.tmp"
        entries: list[dict[str, Any]] = []
        written = 0
        processed = 0
        with archive_path.open("rb") as source, temp_path.open("wb") as target:
            while True:
                raw_line = source.readline()
                if not raw_line:
                    break
                processed += len(raw_line)
                try:
                    envelope = json.loads(raw_line)
                    payload_ref = envelope.get("payload_ref") if isinstance(envelope, dict) else None
                except (UnicodeDecodeError, json.JSONDecodeError):
                    payload_ref = None
                if isinstance(payload_ref, str) and payload_ref in deleted_refs:
                    continue
                offset = target.tell()
                target.write(raw_line)
                if isinstance(payload_ref, str) and raw_line.endswith(b"\n"):
                    entries.append({"payload_ref": payload_ref, "byte_offset": offset, "byte_length": len(raw_line)})
                written += len(raw_line)
                if self._cancel_requested:
                    break
            target.flush()
            os.fsync(target.fileno())
        self._update_job(manifest, stage="rewrite_archive", processed_bytes=processed, retained_bytes=written)
        return temp_path, entries

    def _job_path(self, job_id: str) -> Path:
        return self.paths.storage_maintenance_dir / f"job-{job_id}.json"

    def _job_events_path(self, job_id: str) -> Path:
        return self.paths.storage_maintenance_dir / f"job-{job_id}.events.jsonl"

    def _write_preview(self, preview: dict[str, Any]) -> None:
        atomic_write_text(
            self.paths.storage_maintenance_dir / f"preview-{preview['preview_id']}.json",
            json.dumps(preview, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
            mode=0o600,
        )

    def _write_compaction_preview(self, preview: dict[str, Any]) -> None:
        atomic_write_text(
            self.paths.storage_maintenance_dir / f"compaction-preview-{preview['preview_id']}.json",
            json.dumps(preview, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
            mode=0o600,
        )

    def _write_archive_compression_preview(self, preview: dict[str, Any]) -> None:
        atomic_write_text(
            self.paths.storage_maintenance_dir / f"archive-compression-preview-{preview['preview_id']}.json",
            json.dumps(preview, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
            mode=0o600,
        )

    def _load_compaction_preview(self, preview_id: str) -> dict[str, Any]:
        if not preview_id or any(character not in "0123456789abcdef" for character in preview_id):
            raise ValueError("Invalid preview_id.")
        preview = read_json(self.paths.storage_maintenance_dir / f"compaction-preview-{preview_id}.json", None)
        if not isinstance(preview, dict):
            raise ValueError("Compaction preview was not found.")
        if datetime.fromisoformat(str(preview["expires_at"])) <= datetime.now(UTC):
            raise ValueError("Compaction preview has expired.")
        return preview

    def _load_archive_compression_preview(self, preview_id: str) -> dict[str, Any]:
        if not preview_id or any(character not in "0123456789abcdef" for character in preview_id):
            raise ValueError("Invalid preview_id.")
        preview = read_json(
            self.paths.storage_maintenance_dir / f"archive-compression-preview-{preview_id}.json",
            None,
        )
        if not isinstance(preview, dict):
            raise ValueError("Archive compression preview was not found.")
        if datetime.fromisoformat(str(preview["expires_at"])) <= datetime.now(UTC):
            raise ValueError("Archive compression preview has expired.")
        return preview

    def _write_job(self, manifest: dict[str, Any]) -> None:
        atomic_write_text(
            self._job_path(str(manifest["job_id"])),
            json.dumps(manifest, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
            mode=0o600,
        )

    def _create_job(self, manifest: dict[str, Any]) -> dict[str, Any]:
        created = {**manifest, "event_seq": 1}
        event = self._safe_job_event(created, seq=1)
        with self._event_lock:
            self._append_job_event(event)
            self._write_job(created)
        return created

    def _update_job(self, manifest: dict[str, Any], **changes: Any) -> dict[str, Any]:
        with self._event_lock:
            current = read_json(self._job_path(str(manifest["job_id"])), {})
            current_manifest = current if isinstance(current, dict) else {}
            seq = max(
                int(manifest.get("event_seq") or 0),
                int(current_manifest.get("event_seq") or 0),
            ) + 1
            updated = {
                **manifest,
                **current_manifest,
                **changes,
                "event_seq": seq,
                "updated_at": datetime.now(UTC).isoformat(),
            }
            self._append_job_event(self._safe_job_event(updated, seq=seq))
            self._write_job(updated)
        return updated

    def _safe_job_event(self, manifest: dict[str, Any], *, seq: int) -> dict[str, Any]:
        def safe_code(value: Any, fallback: str) -> str:
            text = str(value or fallback)
            sanitized = "".join(character for character in text if character.isalnum() or character in "_.-")
            return sanitized[:80] or fallback

        metrics: dict[str, int | float | bool] = {}
        sources = [manifest]
        result = manifest.get("result")
        if isinstance(result, dict):
            sources.append(result)
        for source in sources:
            for key, value in source.items():
                if not isinstance(value, (int, float, bool)):
                    continue
                if key in _EVENT_METRIC_NAMES or key.endswith(_EVENT_METRIC_SUFFIXES):
                    metrics[key] = value
        state = safe_code(manifest.get("state"), "unknown")
        stage = safe_code(manifest.get("stage"), "unknown")
        event = {
            "seq": seq,
            "timestamp": str(manifest.get("updated_at") or datetime.now(UTC).isoformat()),
            "job_id": safe_code(manifest.get("job_id"), "unknown"),
            "operation": safe_code(manifest.get("operation"), "unknown"),
            "state": state,
            "stage": stage,
            "message_code": f"maintenance.{state}.{stage}",
            "metrics": metrics,
        }
        if manifest.get("failed_stage"):
            event["failed_stage"] = safe_code(manifest.get("failed_stage"), "unknown")
        if manifest.get("error_code"):
            event["error_code"] = safe_code(manifest.get("error_code"), "unknown_error")
        return event

    def _append_job_event(self, event: dict[str, Any]) -> None:
        encoded = (json.dumps(event, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode()
        event_path = self._job_events_path(str(event["job_id"]))
        self._append_rotating(
            event_path,
            encoded,
            max_bytes=MAINTENANCE_EVENT_MAX_BYTES,
            backups=MAINTENANCE_EVENT_BACKUPS,
        )
        try:
            self._append_rotating(
                self.paths.logs_dir / "storage-maintenance.log",
                encoded,
                max_bytes=MAINTENANCE_LOG_MAX_BYTES,
                backups=MAINTENANCE_LOG_BACKUPS,
            )
        except OSError:
            # The per-job stream is authoritative. A global diagnostic mirror
            # must never make an otherwise safe rollback fail.
            pass

    @staticmethod
    def _append_rotating(path: Path, payload: bytes, *, max_bytes: int, backups: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        current_size = path.stat().st_size if path.exists() else 0
        if current_size and current_size + len(payload) > max_bytes:
            oldest = path.with_name(f"{path.name}.{backups}")
            oldest.unlink(missing_ok=True)
            for index in range(backups - 1, 0, -1):
                source = path.with_name(f"{path.name}.{index}")
                if source.exists():
                    os.replace(source, path.with_name(f"{path.name}.{index + 1}"))
            os.replace(path, path.with_name(f"{path.name}.1"))
        descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "ab") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

    def _verify_embedded_payload_archive_copies(
        self,
        database_path: Path,
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        payload_count = 0
        payload_bytes = 0
        archive_bytes = 0
        retained_payload_count = 0
        retained_payload_bytes = 0
        payload_ids: list[int] = []
        last_reported_bytes = 0
        with closing(sqlite3.connect(database_path, timeout=30)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """SELECT p.id, p.payload_ref, p.nonce, p.ciphertext,
                          i.source_path, i.source_inode, i.byte_offset, i.byte_length
                     FROM call_payloads p
                     LEFT JOIN payload_archive_index i ON i.payload_ref=p.payload_ref
                    ORDER BY p.id"""
            )
            for row in rows:
                if self._cancel_requested:
                    raise InterruptedError("Storage maintenance was canceled.")
                row_bytes = len(str(row["nonce"])) + len(str(row["ciphertext"]))
                equivalent = False
                if row["source_path"] is not None:
                    try:
                        source_path = Path(str(row["source_path"]))
                        source_stat = source_path.stat()
                        if source_stat.st_ino != int(row["source_inode"]):
                            raise ValueError("Payload archive identity changed during verification.")
                        envelope = read_payload_envelope(
                            source_path,
                            byte_offset=int(row["byte_offset"]),
                            byte_length=int(row["byte_length"]),
                            expected_payload_ref=str(row["payload_ref"]),
                        )
                        equivalent = (
                            envelope.get("nonce") == row["nonce"]
                            and envelope.get("ciphertext") == row["ciphertext"]
                        )
                    except (OSError, TypeError, ValueError, json.JSONDecodeError):
                        equivalent = False
                if equivalent:
                    payload_ids.append(int(row["id"]))
                    payload_count += 1
                    payload_bytes += row_bytes
                    archive_bytes += int(row["byte_length"])
                else:
                    retained_payload_count += 1
                    retained_payload_bytes += row_bytes
                processed_bytes = payload_bytes + retained_payload_bytes
                if processed_bytes - last_reported_bytes >= 64 * 1024 * 1024:
                    self._update_job(
                        manifest,
                        stage="verify_archive_coverage",
                        verified_payload_count=payload_count,
                        verified_payload_bytes=payload_bytes,
                        retained_payload_count=retained_payload_count,
                        retained_payload_bytes=retained_payload_bytes,
                    )
                    last_reported_bytes = processed_bytes
        return {
            "payload_count": payload_count,
            "payload_bytes": payload_bytes,
            "archive_bytes": archive_bytes,
            "retained_payload_count": retained_payload_count,
            "retained_payload_bytes": retained_payload_bytes,
            "payload_ids": payload_ids,
        }

    @staticmethod
    def _database_table_counts(path: Path) -> dict[str, int]:
        with closing(sqlite3.connect(path, timeout=30)) as connection:
            table_names = [
                str(row[0])
                for row in connection.execute(
                    """SELECT name FROM sqlite_schema
                        WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name<>'call_payloads'
                        ORDER BY name"""
                ).fetchall()
            ]
            counts: dict[str, int] = {}
            for table_name in table_names:
                quoted_name = table_name.replace('"', '""')
                counts[table_name] = int(connection.execute(
                    f'SELECT COUNT(*) FROM "{quoted_name}"'
                ).fetchone()[0])
        return counts

    @staticmethod
    def _projection_fingerprint(projection: dict[str, Any]) -> dict[str, Any]:
        return {
            "state": projection.get("state"),
            "embedded_payload_count": int(projection.get("embedded_payload_count") or 0),
            "embedded_payload_bytes": int(projection.get("embedded_payload_bytes") or 0),
            "archive_covered_payload_count": int(projection.get("archive_covered_payload_count") or 0),
            "missing_archive_payload_count": int(projection.get("missing_archive_payload_count") or 0),
            "archive_path": projection.get("archive_path"),
            "archive_inode": projection.get("archive_inode"),
            "archive_size": int(projection.get("archive_size") or 0),
            "archive_mtime_ns": projection.get("archive_mtime_ns"),
            "max_indexed_end": int(projection.get("max_indexed_end") or 0),
            "decryption_key_available": bool(projection.get("decryption_key_available")),
        }

    def _archive_compression_fingerprint(self, inventory: dict[str, Any]) -> dict[str, Any]:
        return {
            "archive_inode": inventory.get("archive_inode"),
            "archive_size": int(inventory.get("archive_size") or 0),
            "archive_mtime_ns": inventory.get("archive_mtime_ns"),
            "indexed_payload_count": int(inventory.get("indexed_payload_count") or 0),
            "eligible_payload_count": int(inventory.get("eligible_payload_count") or 0),
            "eligible_archive_bytes": int(inventory.get("eligible_archive_bytes") or 0),
            "embedded_payload_count": int(inventory.get("embedded_payload_count") or 0),
            "indexed_archive_bytes": int(inventory.get("indexed_archive_bytes") or 0),
            "rewrite_set_digest": inventory.get("rewrite_set_digest"),
        }

    @staticmethod
    def _encoded_archive_line(envelope: dict[str, Any]) -> bytes:
        return (json.dumps(envelope, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")

    @staticmethod
    def _database_fingerprint(path: Path) -> dict[str, Any]:
        stat = path.stat()
        wal_path = Path(f"{path}-wal")
        wal_stat = wal_path.stat() if wal_path.exists() else None
        return {
            "inode": stat.st_ino,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "wal_size": wal_stat.st_size if wal_stat else 0,
            "wal_mtime_ns": wal_stat.st_mtime_ns if wal_stat and wal_stat.st_size else None,
        }

    @staticmethod
    def _database_health(path: Path) -> dict[str, Any]:
        with closing(sqlite3.connect(path, timeout=30)) as connection:
            quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            foreign_key_errors = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check").fetchall()]
        return {"quick_check": quick_check, "foreign_key_errors": foreign_key_errors}

    def _acquire_maintenance_lock(self, manifest: dict[str, Any]) -> None:
        self.paths.storage_maintenance_dir.mkdir(parents=True, exist_ok=True)
        payload = {"job_id": manifest["job_id"], "operation": manifest["operation"], "pid": os.getpid()}
        try:
            descriptor = os.open(self._maintenance_lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            existing = read_json(self._maintenance_lock_path, {})
            existing_pid = int(existing.get("pid") or 0) if isinstance(existing, dict) else 0
            try:
                if existing_pid > 0:
                    os.kill(existing_pid, 0)
                    raise ValueError("A storage maintenance lock is already active.")
            except ProcessLookupError:
                self._maintenance_lock_path.unlink(missing_ok=True)
                return self._acquire_maintenance_lock(manifest)
            except PermissionError as error:
                raise ValueError("A storage maintenance lock is already active.") from error
            raise ValueError("A storage maintenance lock is already active.")
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _release_maintenance_lock(self, job_id: str) -> None:
        existing = read_json(self._maintenance_lock_path, {})
        if isinstance(existing, dict) and existing.get("job_id") == job_id:
            self._maintenance_lock_path.unlink(missing_ok=True)

    def _recover_maintenance_lock(self) -> None:
        existing = read_json(self._maintenance_lock_path, None)
        if not isinstance(existing, dict):
            self._maintenance_lock_path.unlink(missing_ok=True)
            return
        existing_pid = int(existing.get("pid") or 0)
        if existing_pid > 0:
            try:
                os.kill(existing_pid, 0)
                return
            except PermissionError:
                return
            except ProcessLookupError:
                pass
        job_id = str(existing.get("job_id") or "")
        manifest = read_json(self._job_path(job_id), None) if job_id else None
        if isinstance(manifest, dict) and manifest.get("state") in {"queued", "running"}:
            self._update_job(manifest, state="failed", stage="failed", error_code="maintenance_interrupted")
        self._maintenance_lock_path.unlink(missing_ok=True)

    @staticmethod
    def _remove_sqlite_sidecars(database_path: Path) -> None:
        Path(f"{database_path}-wal").unlink(missing_ok=True)
        Path(f"{database_path}-shm").unlink(missing_ok=True)

    def _source_fingerprint(
        self,
        database: dict[str, Any],
        archive_status: dict[str, Any],
        bucket_status: dict[str, Any],
    ) -> dict[str, Any]:
        database_stat = Path(database["path"]).stat()
        archive_path = self.paths.encrypted_payload_log
        archive_stat = archive_path.stat() if archive_path.exists() else None
        return {
            "database_inode": database_stat.st_ino,
            "database_size": database_stat.st_size,
            "database_mtime_ns": database_stat.st_mtime_ns,
            "archive_inode": archive_stat.st_ino if archive_stat else None,
            "archive_size": archive_stat.st_size if archive_stat else 0,
            "archive_mtime_ns": archive_stat.st_mtime_ns if archive_stat else None,
            "archive_offset": int(archive_status.get("offset") or 0),
            "bucket_count": int(bucket_status["bucket_count"]),
            "call_count": int(database["call_count"]),
        }

    def _schedule_bucket_refresh(self) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            self._thread = threading.Thread(
                target=self._refresh_buckets_background,
                name="codexthrift-storage-bucket-backfill",
                daemon=True,
            )
            self._thread.start()
            return True

    def _schedule_automatic_check(
        self,
        settings: dict[str, Any],
        previous: dict[str, Any],
        *,
        force: bool = False,
    ) -> bool:
        last_checked = previous.get("checked_at")
        if not force and isinstance(last_checked, str):
            try:
                due_at = datetime.fromisoformat(last_checked) + timedelta(hours=int(settings["check_interval_hours"]))
                if due_at > datetime.now(UTC):
                    return False
            except (TypeError, ValueError):
                pass
        with self._lock:
            if self._policy_thread is not None and self._policy_thread.is_alive():
                return True
            self._policy_thread = threading.Thread(
                target=self._run_automatic_check,
                args=(dict(settings),),
                name="codexthrift-storage-policy-check",
                daemon=True,
            )
            self._policy_thread.start()
            return True

    def _run_automatic_check(self, settings: dict[str, Any]) -> None:
        try:
            preview = self.preview_retention(
                limit_gib=float(settings["limit_gib"]),
                cleanup_ratio=float(settings["cleanup_ratio"]),
                minimum_retention_hours=int(settings["minimum_retention_hours"]),
                delete_mode=str(settings["delete_mode"]),
                persist=False,
            )
            plan = preview.get("plan") if isinstance(preview.get("plan"), dict) else {}
            database = self.store.storage_overview()
            result = {
                "checked_at": datetime.now(UTC).isoformat(),
                "state": preview.get("state"),
                "needs_maintenance": bool(plan.get("needs_cleanup")),
                "cutoff_at": plan.get("cutoff_at"),
                "estimated_release_bytes": int(plan.get("estimated_release_bytes") or 0),
                "estimated_after_bytes": int(plan.get("estimated_after_bytes") or 0),
                "compaction_recommended": int(database["reclaimable_bytes"]) >= 256 * 1024 * 1024
                or int(database["wal_bytes"]) >= 512 * 1024 * 1024,
                "delete_mode": settings["delete_mode"],
            }
        except Exception as error:
            result = {
                "checked_at": datetime.now(UTC).isoformat(),
                "state": "failed",
                "needs_maintenance": False,
                "error_code": type(error).__name__,
            }
        atomic_write_json(self._automatic_check_path, result, mode=0o600)
        with self._lock:
            self._policy_thread = None

    def _refresh_buckets_background(self) -> None:
        totals = {"processed": 0, "complete": False}
        try:
            while True:
                batch = self.store.refresh_storage_buckets()
                totals["processed"] += int(batch["processed"])
                totals["complete"] = bool(batch["complete"])
                if batch["complete"]:
                    break
                time.sleep(0.05)
        except (OSError, ValueError):
            totals["error"] = "storage_bucket_backfill_failed"
        with self._lock:
            self._last_result = totals
            self._thread = None
