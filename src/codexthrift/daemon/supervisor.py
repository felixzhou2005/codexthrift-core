from __future__ import annotations

import threading
from copy import deepcopy
from typing import Any, Protocol

from ..errors import ControlError
from ..security.redaction import redact_sensitive_text
from ..storage.atomic_io import utc_now_iso
from .desired_state import DesiredStateStore


class ProxyController(Protocol):
    def start_proxy(
        self,
        *,
        confirm: str,
        allow_unverified: bool = False,
    ) -> dict[str, Any]: ...

    def proxy_status(self) -> dict[str, Any]: ...


class DaemonSupervisor:
    def __init__(self, controller: ProxyController, desired_state: DesiredStateStore) -> None:
        self.controller = controller
        self.desired_state = desired_state
        self._lock = threading.Lock()
        self._startup: dict[str, Any] = {"status": "pending"}

    def status(self) -> dict[str, Any]:
        with self._lock:
            startup = deepcopy(self._startup)
        return {"startup": startup, "desiredState": self.desired_state.status()}

    def restore(self) -> dict[str, Any]:
        started_at = utc_now_iso()
        self._set_startup({"status": "restoring", "startedAt": started_at})
        try:
            desired = self.desired_state.load()
            proxy = desired["proxy"]
            if proxy["desired"] == "stopped":
                recovery = {
                    "status": "skipped",
                    "startedAt": started_at,
                    "finishedAt": utc_now_iso(),
                    "reason": "proxy_desired_stopped",
                }
            else:
                result = self.controller.start_proxy(
                    confirm="APPLY",
                    allow_unverified=bool(proxy.get("allowUnverified", False)),
                )
                status = self.controller.proxy_status()
                if result.get("ok") is False:
                    raise ControlError("LiteLLM 启动返回失败状态。")
                if not status.get("running") or not status.get("health", {}).get("ok"):
                    raise ControlError("LiteLLM 未通过 PID、端口和认证健康检查。")
                recovery = {
                    "status": "restored",
                    "startedAt": started_at,
                    "finishedAt": utc_now_iso(),
                    "proxy": {
                        "running": True,
                        "pid": status.get("pid"),
                        "health": {"ok": True},
                    },
                }
            self.desired_state.record_recovery(recovery)
            self._set_startup(recovery)
            return recovery
        except (ControlError, OSError, TypeError, ValueError) as exc:
            recovery = {
                "status": "failed",
                "startedAt": started_at,
                "finishedAt": utc_now_iso(),
                "errorType": type(exc).__name__,
                "message": redact_sensitive_text(str(exc)),
            }
            try:
                self.desired_state.record_recovery(recovery)
            except ControlError:
                pass
            self._set_startup(recovery)
            return recovery

    def _set_startup(self, value: dict[str, Any]) -> None:
        with self._lock:
            self._startup = deepcopy(value)
