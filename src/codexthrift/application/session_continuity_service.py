from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Callable
from typing import Any, Protocol

from ..app_server_session import CodexAppServerSession
from ..security.redaction import redact_sensitive_text


class AppServerSession(Protocol):
    def request(self, method: str, params: Any = None) -> dict[str, Any]: ...

    def close(self) -> None: ...


class CodexSessionContinuityService:
    """Read-only proof that all-provider Codex history remains discoverable.

    The persisted snapshot intentionally contains only counts and aggregate
    hashes. Thread titles, working directories, prompts, and raw identifiers
    never leave this service.
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], AppServerSession] | None = None,
    ) -> None:
        self._session_factory = session_factory or CodexAppServerSession

    def capture(self) -> dict[str, Any]:
        session: AppServerSession | None = None
        try:
            session = self._session_factory()
            active = self._list_threads(session, archived=False)
            archived = self._list_threads(session, archived=True)
            combined_ids = active["ids"] | archived["ids"]
            return {
                "available": True,
                "active": self._public_inventory(active),
                "archived": self._public_inventory(archived),
                "combined_count": len(combined_ids),
                "combined_ids_hash": self._ids_hash(combined_ids),
                "combined_id_hashes": self._individual_id_hashes(combined_ids),
                "clients": {
                    "desktop": {
                        "all_provider_history": True,
                        "cross_provider_resume": True,
                    },
                    "vscode": {
                        "all_provider_history": True,
                        "cross_provider_resume": True,
                    },
                    "cli": {
                        "all_provider_history": True,
                        "cross_provider_resume": True,
                    },
                },
                "query": {
                    "model_providers": "all",
                    "active_and_archived": True,
                },
            }
        except Exception as error:  # noqa: BLE001 - capability failure is data
            return {
                "available": False,
                "error": redact_sensitive_text(str(error)),
                "active": self._empty_inventory(),
                "archived": self._empty_inventory(),
                "combined_count": 0,
                "combined_ids_hash": self._ids_hash(set()),
                "combined_id_hashes": [],
                "clients": {
                    "desktop": {"all_provider_history": None, "cross_provider_resume": None},
                    "vscode": {"all_provider_history": None, "cross_provider_resume": None},
                    "cli": {"all_provider_history": None, "cross_provider_resume": None},
                },
                "query": {
                    "model_providers": "all",
                    "active_and_archived": True,
                },
            }
        finally:
            if session is not None:
                session.close()

    def verify(
        self,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
    ) -> dict[str, Any]:
        before = before or {}
        after = after or {}
        available = bool(before.get("available") and after.get("available"))
        if not available:
            verified = False
            unchanged = False
            reason = "session_inventory_unavailable"
        else:
            before_count = int(before.get("combined_count") or 0)
            after_count = int(after.get("combined_count") or 0)
            before_id_hashes = before.get("combined_id_hashes")
            after_id_hashes = after.get("combined_id_hashes")
            if isinstance(before_id_hashes, list) and isinstance(after_id_hashes, list):
                before_set = {str(value) for value in before_id_hashes}
                after_set = {str(value) for value in after_id_hashes}
                missing_count = len(before_set - after_set)
                added_count = len(after_set - before_set)
                unchanged = before_set == after_set
                verified = missing_count == 0
            else:
                before_hash = str(before.get("combined_ids_hash") or "")
                after_hash = str(after.get("combined_ids_hash") or "")
                unchanged = before_count == after_count and before_hash == after_hash
                verified = unchanged
                missing_count = max(0, before_count - after_count)
                added_count = max(0, after_count - before_count)
            if missing_count:
                reason = "missing_sessions_after_mode_switch"
            elif added_count:
                reason = "verified_with_new_sessions"
            else:
                reason = "verified"
        return {
            "verified": verified,
            "reason": reason,
            "session_set_unchanged": unchanged,
            "missing_count": missing_count if available else None,
            "added_count": added_count if available else None,
            "native_telemetry_required": False,
            "before": before,
            "after": after,
        }

    def _list_threads(
        self,
        session: AppServerSession,
        *,
        archived: bool,
    ) -> dict[str, Any]:
        ids: set[str] = set()
        providers: Counter[str] = Counter()
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
                if not isinstance(thread_id, str) or not thread_id:
                    continue
                if thread_id in ids:
                    continue
                ids.add(thread_id)
                provider = (
                    item.get("modelProvider")
                    or item.get("model_provider")
                    or item.get("provider")
                    or "unknown"
                )
                providers[str(provider)] += 1
            next_cursor = response.get("nextCursor") or response.get("next_cursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                break
            cursor = next_cursor
        else:
            raise RuntimeError("Codex thread/list pagination exceeded 100 pages")
        return {"ids": ids, "provider_counts": dict(providers)}

    @classmethod
    def _public_inventory(cls, inventory: dict[str, Any]) -> dict[str, Any]:
        ids = inventory.get("ids") or set()
        counts = inventory.get("provider_counts") or {}
        return {
            "count": len(ids),
            "ids_hash": cls._ids_hash(ids),
            "id_hashes": cls._individual_id_hashes(ids),
            "provider_counts": dict(sorted(counts.items())),
        }

    @classmethod
    def _empty_inventory(cls) -> dict[str, Any]:
        return {
            "count": 0,
            "ids_hash": cls._ids_hash(set()),
            "id_hashes": [],
            "provider_counts": {},
        }

    @staticmethod
    def _ids_hash(ids: set[str]) -> str:
        digest = hashlib.sha256()
        for value in sorted(ids):
            digest.update(value.encode("utf-8"))
            digest.update(b"\0")
        return digest.hexdigest()

    @staticmethod
    def _individual_id_hashes(ids: set[str]) -> list[str]:
        return sorted(
            hashlib.sha256(value.encode("utf-8")).hexdigest() for value in ids
        )
