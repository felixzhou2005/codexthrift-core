from __future__ import annotations

import json
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

from ..errors import ControlError
from ..storage.atomic_io import atomic_write_json, utc_now_iso

SCHEMA_VERSION = 1


def _default_state() -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "proxy": {"desired": "stopped", "allowUnverified": False},
        "updatedAt": None,
        "lastRecovery": None,
    }


class DesiredStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    def load(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._load_unlocked())

    def status(self) -> dict[str, Any]:
        try:
            state = self.load()
        except ControlError as exc:
            return {
                "valid": False,
                "path": str(self.path),
                "error": str(exc),
                "proxy": {"desired": "stopped", "allowUnverified": False},
            }
        return {"valid": True, "path": str(self.path), **state}

    def set_proxy_desired(self, *, running: bool, allow_unverified: bool = False) -> dict[str, Any]:
        with self._lock:
            state = self._load_unlocked()
            state["proxy"] = {
                "desired": "running" if running else "stopped",
                "allowUnverified": bool(allow_unverified) if running else False,
            }
            state["updatedAt"] = utc_now_iso()
            atomic_write_json(self.path, state, mode=0o600)
            return deepcopy(state)

    def record_recovery(self, recovery: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            state = self._load_unlocked()
            state["lastRecovery"] = deepcopy(recovery)
            atomic_write_json(self.path, state, mode=0o600)
            return deepcopy(state)

    def _load_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return _default_state()
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ControlError(
                "Desired state 无法读取；为安全起见不会自动启动 LiteLLM。"
            ) from exc
        if not isinstance(value, dict) or value.get("schemaVersion") != SCHEMA_VERSION:
            raise ControlError("Desired state schema 不受支持；不会自动启动 LiteLLM。")
        proxy = value.get("proxy")
        if not isinstance(proxy, dict) or proxy.get("desired") not in {"running", "stopped"}:
            raise ControlError("Desired state 的 Proxy 目标无效；不会自动启动 LiteLLM。")
        allow_unverified = proxy.get("allowUnverified", False)
        if not isinstance(allow_unverified, bool):
            raise ControlError("Desired state 的 allowUnverified 必须是布尔值。")
        normalized = _default_state()
        normalized.update(value)
        normalized["proxy"] = {
            "desired": proxy["desired"],
            "allowUnverified": allow_unverified,
        }
        return normalized
