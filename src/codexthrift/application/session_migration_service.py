from __future__ import annotations

import hashlib
import json
import threading
from collections import Counter
from collections.abc import Callable
from typing import Any, Protocol

from ..app_server_session import CodexAppServerSession
from ..paths import WorkspacePaths
from ..util import atomic_write_json, utc_now_iso


class AppServerSession(Protocol):
    def request(self, method: str, params: Any = None) -> dict[str, Any]: ...

    def close(self) -> None: ...


class SessionMigrationService:
    """Migrate legacy Provider threads through the official App Server only."""

    def __init__(
        self,
        paths: WorkspacePaths,
        *,
        session_factory: Callable[[], AppServerSession] | None = None,
    ) -> None:
        self._paths = paths
        self._session_factory = session_factory or (
            lambda: CodexAppServerSession(experimental_api=True)
        )
        self._lock = threading.RLock()

    def preview(self) -> dict[str, Any]:
        with self._lock:
            session: AppServerSession | None = None
            try:
                session = self._session_factory()
                threads = self._inventory(session)
                return self._preview_from_inventory(threads)
            except Exception as error:  # noqa: BLE001 - capability failure is returned as data
                result = self._unavailable_preview()
                result["diagnostic_code"] = type(error).__name__
                return result
            finally:
                if session is not None:
                    session.close()

    def migrate_next(self, *, confirm: str) -> dict[str, Any]:
        if confirm != "FORK_LEGACY_CODEX_SESSIONS":
            return self._migration_error("confirmation_required", retryable=False)
        with self._lock:
            ledger, ledger_state = self._load_ledger()
            if ledger_state == "invalid":
                return self._migration_error("migration_ledger_invalid", retryable=False)

            session: AppServerSession | None = None
            try:
                session = self._session_factory()
                threads = self._inventory(session)
                legacy_ids = sorted(
                    thread_id
                    for thread_id, item in threads.items()
                    if item["provider"] == "litellm"
                )
                completed = self._verified_completed_hashes(ledger, threads)
                attempted = self._attempted_hashes(ledger)
                existing_forks = self._existing_openai_forks(threads, set(legacy_ids))

                pending_ids = [
                    old_id
                    for old_id in legacy_ids
                    if self._hash_id(old_id) not in completed
                ]
                candidate = next(
                    (
                        old_id
                        for old_id in pending_ids
                        if self._hash_id(old_id) not in attempted
                    ),
                    pending_ids[0] if pending_ids else None,
                )
                recovered_copy: str | None = None
                if candidate is not None:
                    recovered_copy = existing_forks.get(candidate)
                    prior_record = self._ledger_record(ledger, candidate)
                    if prior_record and prior_record.get("state") == "completed":
                        self._mark_ledger_record(
                            ledger,
                            prior_record,
                            state="failed",
                            result_code="copy_not_persisted",
                        )
                        return self._migration_error(
                            "copy_not_persisted",
                            retryable=False,
                        )
                if candidate is None:
                    return {
                        "ok": True,
                        "result_code": "nothing_to_migrate",
                        "attempted_count": 0,
                        "completed_count": 0,
                        "failed_count": 0,
                        "retryable": False,
                        "preview": self._preview_from_inventory(threads),
                    }

                stage = "verify_existing_copy" if recovered_copy else "read_source"
                new_id = recovered_copy
                try:
                    source_turn_count = self._read_turn_count(session, candidate)
                    if new_id is None:
                        stage = "fork"
                        response = session.request(
                            "thread/fork",
                            {
                                "threadId": candidate,
                                "modelProvider": "openai",
                                "ephemeral": False,
                            },
                        )
                        new_id = self._thread_id(response.get("thread"))
                        if new_id is None:
                            raise RuntimeError("thread/fork response omitted thread id")
                    stage = "verify_copy"
                    copy_turn_count, copy_provider = self._read_thread_verification(
                        session, new_id
                    )
                    if copy_provider != "openai":
                        raise RuntimeError("forked thread provider was not openai")
                    if copy_turn_count != source_turn_count:
                        raise RuntimeError("forked thread turn count did not match source")
                    self._upsert_ledger(
                        ledger,
                        old_id=candidate,
                        new_id=new_id,
                        state="completed",
                        source_turn_count=source_turn_count,
                        copy_turn_count=copy_turn_count,
                        result_code="recovered_existing_copy" if recovered_copy else "fork_verified",
                    )
                    result_code = "recovered_existing_copy" if recovered_copy else "fork_verified"
                    completed_count = 1
                    failed_count = 0
                    ok = True
                except Exception:  # noqa: BLE001 - only a non-sensitive stage code leaves the service
                    self._upsert_ledger(
                        ledger,
                        old_id=candidate,
                        new_id=new_id,
                        state="failed",
                        source_turn_count=None,
                        copy_turn_count=None,
                        result_code=f"{stage}_failed",
                    )
                    result_code = f"{stage}_failed"
                    completed_count = 0
                    failed_count = 1
                    ok = False
            except Exception as error:  # noqa: BLE001 - App Server failure is structured data
                return self._migration_error(
                    "codex_app_server_unavailable",
                    retryable=True,
                    diagnostic_code=type(error).__name__,
                )
            finally:
                if session is not None:
                    session.close()

            if ok and new_id is not None and not self._copy_is_persisted(new_id):
                self._upsert_ledger(
                    ledger,
                    old_id=candidate,
                    new_id=new_id,
                    state="failed",
                    source_turn_count=None,
                    copy_turn_count=None,
                    result_code="copy_not_persisted",
                )
                result_code = "copy_not_persisted"
                completed_count = 0
                failed_count = 1
                ok = False

            preview = self.preview()
            return {
                "ok": ok,
                "result_code": result_code,
                "attempted_count": 1,
                "completed_count": completed_count,
                "failed_count": failed_count,
                "retryable": not ok,
                "preview": preview,
            }

    def _inventory(self, session: AppServerSession) -> dict[str, dict[str, str | None]]:
        threads: dict[str, dict[str, str | None]] = {}
        self._collect_threads(session, archived=False, threads=threads)
        self._collect_threads(session, archived=True, threads=threads)
        return threads

    def _preview_from_inventory(
        self,
        threads: dict[str, dict[str, str | None]],
    ) -> dict[str, Any]:
        provider_counts = Counter(str(item["provider"]) for item in threads.values())
        legacy_ids = {
            thread_id
            for thread_id, item in threads.items()
            if item["provider"] == "litellm"
        }
        ledger, ledger_state = self._load_ledger()
        completed_hashes = self._verified_completed_hashes(ledger, threads)
        ledger_completed_ids = {
            thread_id
            for thread_id in legacy_ids
            if self._hash_id(thread_id) in completed_hashes
        }
        existing_forks = self._existing_openai_forks(threads, legacy_ids)
        unrecorded_forks = set(existing_forks) - ledger_completed_ids
        migrated_count = len(ledger_completed_ids | set(existing_forks))
        legacy_count = len(legacy_ids)
        return {
            "available": True,
            "source": "codex_app_server",
            "total_count": len(threads),
            "provider_counts": dict(sorted(provider_counts.items())),
            "openai_count": provider_counts.get("openai", 0),
            "legacy_litellm_count": legacy_count,
            "other_provider_count": len(threads)
            - provider_counts.get("openai", 0)
            - legacy_count,
            "already_migrated_count": migrated_count,
            "needs_migration_count": legacy_count - migrated_count,
            "projected_openai_copies": legacy_count - migrated_count,
            "needs_verification_count": len(unrecorded_forks),
            "ledger_state": ledger_state,
            "migration_ready": ledger_state != "invalid",
            "originals_preserved": True,
            "client_files_modified": False,
            "content_read": False,
            "preview_created_sessions": False,
            "scope": "active_and_archived",
        }

    @staticmethod
    def _collect_threads(
        session: AppServerSession,
        *,
        archived: bool,
        threads: dict[str, dict[str, str | None]],
    ) -> None:
        cursor: str | None = None
        for _ in range(100):
            params: dict[str, Any] = {
                "archived": archived,
                "limit": 100,
                "modelProviders": [],
            }
            if cursor:
                params["cursor"] = cursor
            response = session.request("thread/list", params)
            items = response.get("data")
            if not isinstance(items, list):
                items = response.get("threads")
            if not isinstance(items, list):
                items = response.get("items")
            if not isinstance(items, list):
                items = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                thread_id = item.get("id") or item.get("threadId")
                if not isinstance(thread_id, str) or not thread_id or thread_id in threads:
                    continue
                provider = (
                    item.get("modelProvider")
                    or item.get("model_provider")
                    or item.get("provider")
                    or "unknown"
                )
                forked_from = item.get("forkedFromId") or item.get("forked_from_id")
                threads[thread_id] = {
                    "provider": str(provider).strip().lower() or "unknown",
                    "forked_from": forked_from if isinstance(forked_from, str) else None,
                }
            next_cursor = response.get("nextCursor") or response.get("next_cursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                return
            cursor = next_cursor
        raise RuntimeError("Codex thread/list pagination exceeded 100 pages")

    def _load_ledger(self) -> tuple[dict[str, Any], str]:
        try:
            value = json.loads(self._paths.session_migration_ledger.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "migrations": []}, "not_created"
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "migrations": []}, "invalid"
        if (
            not isinstance(value, dict)
            or value.get("version") != 1
            or not isinstance(value.get("migrations"), list)
        ):
            return {"version": 1, "migrations": []}, "invalid"
        return value, "available"

    @classmethod
    def _verified_completed_hashes(
        cls,
        ledger: dict[str, Any],
        threads: dict[str, dict[str, str | None]],
    ) -> set[str]:
        openai_hashes = {
            cls._hash_id(thread_id)
            for thread_id, item in threads.items()
            if item.get("provider") == "openai"
        }
        return {
            str(item["old_id_hash"])
            for item in ledger.get("migrations", [])
            if isinstance(item, dict)
            and item.get("state") == "completed"
            and isinstance(item.get("old_id_hash"), str)
            and isinstance(item.get("new_id_hash"), str)
            and item["new_id_hash"] in openai_hashes
        }

    @staticmethod
    def _attempted_hashes(ledger: dict[str, Any]) -> set[str]:
        return {
            str(item["old_id_hash"])
            for item in ledger.get("migrations", [])
            if isinstance(item, dict) and isinstance(item.get("old_id_hash"), str)
        }

    @staticmethod
    def _existing_openai_forks(
        threads: dict[str, dict[str, str | None]],
        legacy_ids: set[str],
    ) -> dict[str, str]:
        result: dict[str, str] = {}
        for thread_id, item in threads.items():
            source = item.get("forked_from")
            if item.get("provider") == "openai" and source in legacy_ids:
                result.setdefault(str(source), thread_id)
        return result

    @staticmethod
    def _thread_id(value: Any) -> str | None:
        if not isinstance(value, dict):
            return None
        thread_id = value.get("id") or value.get("threadId")
        return thread_id if isinstance(thread_id, str) and thread_id else None

    @classmethod
    def _read_turn_count(cls, session: AppServerSession, thread_id: str) -> int:
        count, _ = cls._read_thread_verification(session, thread_id)
        return count

    @staticmethod
    def _read_thread_verification(
        session: AppServerSession,
        thread_id: str,
    ) -> tuple[int, str]:
        response = session.request(
            "thread/read",
            {"threadId": thread_id, "includeTurns": False},
        )
        thread = response.get("thread")
        if not isinstance(thread, dict):
            raise RuntimeError("thread/read response omitted thread")
        provider = (
            thread.get("modelProvider")
            or thread.get("model_provider")
            or thread.get("provider")
            or "unknown"
        )
        turn_count = 0
        cursor: str | None = None
        for _ in range(100):
            params: dict[str, Any] = {
                "threadId": thread_id,
                "limit": 100,
                "sortDirection": "asc",
                "itemsView": "notLoaded",
            }
            if cursor:
                params["cursor"] = cursor
            turns_page = session.request("thread/turns/list", params)
            data = turns_page.get("data")
            if not isinstance(data, list):
                raise RuntimeError("thread/turns/list response omitted data")
            turn_count += len(data)
            next_cursor = turns_page.get("nextCursor") or turns_page.get("next_cursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                return turn_count, str(provider).strip().lower()
            cursor = next_cursor
        raise RuntimeError("Codex thread/turns/list pagination exceeded 100 pages")

    def _upsert_ledger(
        self,
        ledger: dict[str, Any],
        *,
        old_id: str,
        new_id: str | None,
        state: str,
        source_turn_count: int | None,
        copy_turn_count: int | None,
        result_code: str,
    ) -> None:
        old_hash = self._hash_id(old_id)
        records = [
            item
            for item in ledger.get("migrations", [])
            if isinstance(item, dict) and item.get("old_id_hash") != old_hash
        ]
        record: dict[str, Any] = {
            "old_id_hash": old_hash,
            "state": state,
            "result_code": result_code,
            "updated_at": utc_now_iso(),
        }
        if new_id is not None:
            record["new_id_hash"] = self._hash_id(new_id)
        if source_turn_count is not None:
            record["source_turn_count"] = source_turn_count
        if copy_turn_count is not None:
            record["copy_turn_count"] = copy_turn_count
        records.append(record)
        ledger.update({"version": 1, "updated_at": utc_now_iso(), "migrations": records})
        atomic_write_json(self._paths.session_migration_ledger, ledger, mode=0o600)

    def _copy_is_persisted(self, new_id: str) -> bool:
        session: AppServerSession | None = None
        try:
            session = self._session_factory()
            threads = self._inventory(session)
            item = threads.get(new_id)
            return isinstance(item, dict) and item.get("provider") == "openai"
        finally:
            if session is not None:
                session.close()

    def _ledger_record(self, ledger: dict[str, Any], old_id: str) -> dict[str, Any] | None:
        old_hash = self._hash_id(old_id)
        return next(
            (
                item
                for item in ledger.get("migrations", [])
                if isinstance(item, dict) and item.get("old_id_hash") == old_hash
            ),
            None,
        )

    def _mark_ledger_record(
        self,
        ledger: dict[str, Any],
        record: dict[str, Any],
        *,
        state: str,
        result_code: str,
    ) -> None:
        record.update(
            {"state": state, "result_code": result_code, "updated_at": utc_now_iso()}
        )
        ledger["updated_at"] = utc_now_iso()
        atomic_write_json(self._paths.session_migration_ledger, ledger, mode=0o600)

    @staticmethod
    def _hash_id(thread_id: str) -> str:
        return hashlib.sha256(thread_id.encode("utf-8")).hexdigest()

    @staticmethod
    def _migration_error(
        result_code: str,
        *,
        retryable: bool,
        diagnostic_code: str | None = None,
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "result_code": result_code,
            "attempted_count": 0,
            "completed_count": 0,
            "failed_count": 0,
            "retryable": retryable,
            "diagnostic_code": diagnostic_code,
        }

    @staticmethod
    def _unavailable_preview() -> dict[str, Any]:
        return {
            "available": False,
            "source": "codex_app_server",
            "error_code": "codex_app_server_unavailable",
            "error_message": "Codex App Server 当前不可用，请确认 Codex 已安装后重试。",
            "total_count": 0,
            "provider_counts": {},
            "openai_count": 0,
            "legacy_litellm_count": 0,
            "other_provider_count": 0,
            "already_migrated_count": 0,
            "needs_migration_count": 0,
            "projected_openai_copies": 0,
            "needs_verification_count": 0,
            "ledger_state": "unknown",
            "migration_ready": False,
            "originals_preserved": True,
            "client_files_modified": False,
            "content_read": False,
            "preview_created_sessions": False,
            "scope": "active_and_archived",
            "diagnostic_code": None,
        }
