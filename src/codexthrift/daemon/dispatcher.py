from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from ..application.quota_service import QuotaService
from ..contracts.protocol import CORE_FEATURES, PROTOCOL_VERSION, RpcProtocolError
from ..controller import RouterController
from ..errors import ControlError
from ..security.path_guard import require_confirmation
from .build_identity import build_identity
from .dashboard_compat import DashboardCompatService
from .event_bus import EventBus
from .supervisor import DaemonSupervisor

WRITE_METHODS = {
    "runtime.prepare",
    "runtime.install",
    "subscription.oauth.begin",
    "codex.configure",
    "proxy.start", "proxy.stop", "proxy.restart", "routing.enable", "routing.disable",
    "codex.switchNative", "codex.forceNative", "codex.switchSmart", "account.connect", "account.disconnect",
    "account.refresh", "models.probe", "routing.activatePhase", "routing.phase21.prepare",
    "routing.phase21.activate", "routing.phase3a.prepare", "routing.phase3a.activate",
    "routing.phase3a.recover", "routing.adaptive.import", "routing.adaptive.shadow",
    "routing.adaptive.canary", "routing.adaptive.disable", "reports.feedback",
    "codex.restoreBackup", "tasks.run", "tasks.stop",
    "system.restoreDesiredState",
    "system.shutdown",
}


class RpcDispatcher:
    def __init__(
        self,
        controller: RouterController,
        quota: QuotaService | None = None,
        event_bus: EventBus | None = None,
        supervisor: DaemonSupervisor | None = None,
        identity: dict[str, Any] | None = None,
    ) -> None:
        self.controller = controller
        self._quota = quota
        self._dashboard: DashboardCompatService | None = None
        self._service_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self.events = event_bus or EventBus()
        self.supervisor = supervisor
        self.identity = identity or build_identity()
        self._shutdown_callback: Callable[[], None] | None = None

    def bind_shutdown(self, callback: Callable[[], None]) -> None:
        self._shutdown_callback = callback

    @property
    def quota(self) -> QuotaService:
        if self._quota is None:
            with self._service_lock:
                if self._quota is None:
                    self._quota = QuotaService()
        return self._quota

    def close(self) -> None:
        if self._dashboard is not None:
            self._dashboard.close()
        if self._quota is not None:
            self._quota.close()

    @property
    def dashboard(self) -> DashboardCompatService:
        if self._dashboard is None:
            with self._service_lock:
                if self._dashboard is None:
                    self._dashboard = DashboardCompatService(self.controller)
        return self._dashboard

    def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "core.handshake":
            return self._handshake(params, self.identity)
        if method == "system.shutdown":
            try:
                return self._shutdown(params)
            except (ControlError, TypeError, ValueError) as exc:
                raise RpcProtocolError(-32010, str(exc), {"error_type": type(exc).__name__}) from exc
        handlers: dict[str, Callable[[], Any]] = {
            "system.status": lambda: self.controller.full_status(),
            "system.doctor": lambda: self.controller.setup_guide(),
            "system.environment": lambda: self.controller.environment_check(),
            "system.startupStatus": self._startup_status,
            "system.restoreDesiredState": lambda: self._restore_desired_state(params),
            "runtime.prepare": lambda: self.controller.prepare_configs(**params),
            "runtime.install": lambda: self.controller.start_runtime_install(**params),
            "runtime.installStatus": lambda: self.controller.runtime_install_status(),
            "subscription.oauth.begin": lambda: self.controller.begin_oauth(**params),
            "subscription.oauth.status": lambda: self.controller.oauth_status(),
            "models.probeStatus": lambda: self.controller.model_probe_status(**params),
            "codex.configure": lambda: self.controller.configure_codex(**params),
            "proxy.status": lambda: self.controller.proxy_status(),
            "proxy.start": lambda: self.controller.start_proxy(**params),
            "proxy.stop": lambda: self.controller.stop_proxy(**params),
            "proxy.restart": lambda: self.controller.restart_proxy(**params),
            "routing.status": lambda: self.controller.adaptive_status(),
            "routing.listPolicies": lambda: {"policies": ["saver", "balanced", "quality", "phase3a"]},
            "routing.enable": lambda: self.controller.enable_smart_routing(
                confirm=params.get("confirm", ""),
                allow_unverified=bool(params.get("allow_unverified", False)),
            ),
            "routing.disable": lambda: self.controller.switch_to_native_codex(
                confirm=params.get("confirm", "")
            ),
            "routing.activatePhase": lambda: self.controller.activate_phase(**params),
            "routing.phase21.prepare": lambda: self.controller.prepare_phase21_candidate(**params),
            "routing.phase21.activate": lambda: self.controller.activate_phase21(**params),
            "routing.phase3a.prepare": lambda: self.controller.prepare_phase3a_candidate(**params),
            "routing.phase3a.activate": lambda: self.controller.activate_phase3a(**params),
            "routing.phase3a.recover": lambda: self.controller.rollback_pending_phase3a(**params),
            "routing.adaptive.import": lambda: self.controller.adaptive_import_tasks(**params),
            "routing.adaptive.evaluate": lambda: self.controller.adaptive_evaluate(**params),
            "routing.adaptive.shadow": lambda: self.controller.enable_adaptive_shadow(**params),
            "routing.adaptive.canary": lambda: self.controller.enable_adaptive_canary(**params),
            "routing.adaptive.disable": lambda: self.controller.disable_adaptive(**params),
            "codex.switchNative": lambda: self.controller.switch_to_native_codex(**params),
            "codex.forceNative": lambda: self.controller.force_native_codex(**params),
            "codex.switchSmart": lambda: self.controller.enable_smart_routing(**params),
            "codex.status": lambda: self.controller.codex_mode_status(),
            "account.status": lambda: self.quota.read_snapshot(profile_id=params.get("profile_id")),
            "account.usage": lambda: self.quota.read_snapshot(profile_id=params.get("profile_id")),
            "account.rateLimits": lambda: self.quota.read_snapshot(profile_id=params.get("profile_id")),
            "account.connect": lambda: self._confirmed_account_action(
                params, "CONNECT_ACCOUNT", self.quota.start_login
            ),
            "account.disconnect": lambda: self._confirmed_account_action(
                params, "DISCONNECT_ACCOUNT", self.quota.logout
            ),
            "account.refresh": lambda: self.quota.read_snapshot(profile_id=params.get("profile_id")),
            "models.probe": lambda: self.controller.start_model_probe(**params),
            "logs.tail": lambda: self.controller.logs(**params),
            "reports.summary": lambda: self.controller.routing_report(**params),
            "reports.calls": lambda: self.controller.recent_calls(**params),
            "reports.turns": lambda: self.controller.recent_turns(**params),
            "reports.explain": lambda: self.controller.explain_turn(**params),
            "reports.feedback": lambda: self.controller.record_feedback(**params),
            "codex.backups": lambda: self.controller.list_backups(),
            "codex.restoreBackup": lambda: self.controller.restore_codex_config(**params),
            "tasks.run": lambda: self.controller.start_codex_task(**params),
            "tasks.status": lambda: self.controller.codex_task_status(**params),
            "reports.tasks": lambda: self.controller.list_codex_tasks(**params),
            "tasks.stop": lambda: self.controller.stop_codex_task(**params),
            "events.subscribe": lambda: self.events.wait(
                after=int(params.get("after", 0)),
                timeout=float(params.get("timeout", 0.0)),
            ),
            "compat.dashboardRequest": lambda: self.dashboard.request(**params),
        }
        if method in {"context.compact", "system.repair"}:
            raise RpcProtocolError(
                -32004,
                "Method not available in this Core version",
                {"method": method},
            )
        handler = handlers.get(method)
        if handler is None:
            raise RpcProtocolError(-32601, "Method not found", {"method": method})
        try:
            if method in WRITE_METHODS:
                with self._write_lock:
                    result = handler()
                    self.events.publish(_event_type(method), {"method": method})
                    return result
            return handler()
        except (ControlError, TypeError, ValueError) as exc:
            raise RpcProtocolError(-32010, str(exc), {"error_type": type(exc).__name__}) from exc

    @staticmethod
    def _handshake(
        params: dict[str, Any], identity: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        minimum = str(params.get("protocolMin") or PROTOCOL_VERSION)
        maximum = str(params.get("protocolMax") or PROTOCOL_VERSION)
        if not (minimum <= PROTOCOL_VERSION <= maximum):
            raise RpcProtocolError(-32001, "Incompatible protocol version", {"supported": PROTOCOL_VERSION})
        base = {
            "coreVersion": str((identity or {}).get("coreVersion") or "0.0.0-dev"),
            "protocolVersion": PROTOCOL_VERSION,
            "features": CORE_FEATURES,
        }
        if identity is not None:
            base.update(identity)
        return base

    def _shutdown(self, params: dict[str, Any]) -> dict[str, Any]:
        require_confirmation(str(params.get("confirm") or ""), "SHUTDOWN_CORE")
        requested_instance = str(params.get("instanceId") or "")
        if requested_instance != self.identity.get("instanceId"):
            raise RpcProtocolError(
                -32012,
                "Core instance identity mismatch; refusing shutdown.",
                {"instanceId": self.identity.get("instanceId")},
            )
        if self._shutdown_callback is None:
            raise RpcProtocolError(-32004, "Core shutdown is not managed by this server.")
        threading.Thread(
            target=self._shutdown_callback,
            name="codexthrift-graceful-shutdown",
            daemon=True,
        ).start()
        return {
            "accepted": True,
            "instanceId": requested_instance,
            "coreBuildId": self.identity.get("coreBuildId"),
        }

    @staticmethod
    def _confirmed_account_action(
        params: dict[str, Any],
        expected: str,
        action: Callable[[], Any],
    ) -> Any:
        require_confirmation(str(params.get("confirm") or ""), expected)
        return action()

    def restore_startup(self) -> dict[str, Any]:
        if self.supervisor is None:
            return {"status": "not_managed"}
        with self._write_lock:
            result = self.supervisor.restore()
            self.events.publish("event.service.changed", {"method": "system.startupRestore", **result})
            return result

    def _startup_status(self) -> dict[str, Any]:
        if self.supervisor is None:
            return {"startup": {"status": "not_managed"}, "desiredState": None}
        return self.supervisor.status()

    def _restore_desired_state(self, params: dict[str, Any]) -> dict[str, Any]:
        require_confirmation(str(params.get("confirm") or ""))
        if self.supervisor is None:
            raise ControlError("Desired state supervisor is unavailable.")
        return self.supervisor.restore()

def _event_type(method: str) -> str:
    if method.startswith("proxy."):
        return "event.proxy.health"
    if method.startswith("account."):
        return "event.account.updated"
    if method.startswith(("routing.", "codex.")):
        return "event.service.changed"
    return "event.service.changed"
