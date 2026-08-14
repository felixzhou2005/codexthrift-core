from __future__ import annotations

from typing import Any

from ..codex_clients import detect_codex_clients, restart_status
from ..codex_config import (
    codex_connection_status,
    codex_proxy_auth_command,
    configure_codex,
    restore_codex_backup,
    switch_to_native_codex,
)
from ..codex_ingress import verify_codex_ingress_route as verify_codex_proxy_route
from ..codex_mode import CodexClientMode, read_mode_state, record_mode_switch
from ..codex_verifier import CodexModeVerifier
from ..config_templates import (
    PhaseName,
)
from ..errors import ControlError
from ..security.path_guard import require_confirmation
from .base import BaseApplicationService


class CodexModeService(BaseApplicationService):
    def configure_codex(
        self,
        *,
        phase: PhaseName | None = None,
        confirm: str,
        allow_unverified: bool = False,
    ) -> dict[str, Any]:
        require_confirmation(confirm)
        active_phase = self.active_phase()
        selected_phase: PhaseName = phase or active_phase
        if selected_phase != active_phase:
            raise ControlError(
                f"Codex 配置必须与活动 LiteLLM 阶段一致；当前为 {active_phase}。"
                "请通过 activate_phase3/return_to_phase2 切换阶段。"
            )
        profile = self.active_profile()
        probe_verified = self._probe_is_ready(active_phase, profile)
        if not probe_verified and not allow_unverified:
            raise ControlError(f"{active_phase}/{profile} 尚未通过对应模型探测。")
        configured = configure_codex(self.paths, selected_phase, self.host, self.port)
        configured["model_probe_verified"] = probe_verified
        configured["allow_unverified"] = bool(allow_unverified)
        return configured

    def codex_mode_status(self) -> dict[str, Any]:
        connection = codex_connection_status(self.paths, self.host, self.port)
        clients = detect_codex_clients()
        restart = restart_status(self.paths, clients)
        state = read_mode_state(self.paths) or {}
        verification = state.get("verification") if isinstance(state.get("verification"), dict) else {}
        proxy = self.proxy_status()
        mode = connection["client_mode"]
        route_preflight = (
            verification.get("route_preflight")
            if isinstance(verification.get("route_preflight"), dict)
            else {}
        )
        if mode == CodexClientMode.SMART_ROUTER.value:
            new_session_mode = "routed"
        elif mode == CodexClientMode.NATIVE_OPENAI.value:
            new_session_mode = "native"
        else:
            new_session_mode = "unknown"
        if mode == CodexClientMode.SMART_ROUTER.value:
            proxy_route_state = "routed"
        elif proxy.get("running"):
            proxy_route_state = "standby"
        else:
            proxy_route_state = "stopped"
        return {
            **connection,
            "restart_required": restart["restart_required"],
            "restart": restart,
            "verification": verification,
            "native_auth": {
                "connected": verification.get("native_auth_state") == "connected",
                "state": verification.get("native_auth_state") or "unknown",
                "account_type": verification.get("account_type"),
                "plan_type": verification.get("plan_type"),
            },
            "proxy_running": bool(proxy.get("running")),
            "proxy_used_by_codex": mode == CodexClientMode.SMART_ROUTER.value,
            "proxy_route_state": proxy_route_state,
            "new_session_mode": new_session_mode,
            "stale_client_count": len(restart.get("stale_clients") or []),
            "route_verification": {
                "status": route_preflight.get("status") or (
                    "not_applicable"
                    if mode == CodexClientMode.NATIVE_OPENAI.value
                    else "unknown"
                ),
                "verified": route_preflight.get("verified") is True,
                "error_code": route_preflight.get("error_code"),
                "duration_ms": route_preflight.get("duration_ms"),
            },
            "router_policy": self.active_phase(),
            "router_policy_active": mode == CodexClientMode.SMART_ROUTER.value,
        }

    def enable_smart_routing(self, *, confirm: str, allow_unverified: bool = False) -> dict[str, Any]:
        require_confirmation(confirm, "ENABLE_ROUTER")
        observed = detect_codex_clients()
        proxy = self.start_proxy(confirm="APPLY", allow_unverified=allow_unverified)
        auth_command, auth_args = codex_proxy_auth_command(self.paths)
        route_preflight = verify_codex_proxy_route(
            host=self.host,
            port=self.port,
            command=auth_command,
            args=auth_args,
        )
        if not route_preflight["verified"]:
            return {
                "ok": False,
                "stage": "proxy_preflight",
                "mode": self.codex_mode_status(),
                "proxy": proxy,
                "route_preflight": route_preflight,
                "rollback": {"performed": False, "reason": "config_not_modified"},
            }
        configured = self.configure_codex(
            confirm="APPLY",
            allow_unverified=allow_unverified,
        )
        verification = CodexModeVerifier(self.paths).verify("openai")
        if not verification.get("verified"):
            backup_path = configured.get("backup_path")
            rollback = (
                restore_codex_backup(self.paths, str(backup_path))
                if backup_path
                else {"ok": False, "reason": "backup_unavailable"}
            )
            return {
                "ok": False,
                "stage": "config_verification",
                "mode": self.codex_mode_status(),
                "proxy": proxy,
                "configure": configured,
                "route_preflight": route_preflight,
                "verification": verification,
                "rollback": {"performed": bool(rollback.get("ok")), **rollback},
            }
        combined_verification = {
            **verification,
            "route_preflight": route_preflight,
        }
        state = record_mode_switch(
            self.paths,
            CodexClientMode.SMART_ROUTER,
            observed_clients=observed["clients"],
            verification=combined_verification,
        )
        return {
            "ok": bool(verification.get("verified")),
            "mode": self.codex_mode_status(),
            "proxy": proxy,
            "configure": configured,
            "route_preflight": route_preflight,
            "verification": verification,
            "rollback": {"performed": False},
            "generation": state["generation"],
        }

    def switch_to_native_codex(self, *, confirm: str) -> dict[str, Any]:
        require_confirmation(confirm, "SWITCH_NATIVE")
        observed = detect_codex_clients()
        standby = self.proxy_status()
        try:
            if not standby.get("running") or not standby.get("health", {}).get("ok"):
                standby = self.start_proxy(confirm="APPLY")
        except (ControlError, OSError) as exc:
            standby = {"ok": False, "error": str(exc)}
        if standby.get("ok") is False or not self.proxy_status().get("health", {}).get("ok"):
            return {
                "ok": False,
                "stage": "standby_proxy",
                "mode": self.codex_mode_status(),
                "proxy": self.proxy_status(),
                "verification": {
                    "verified": False,
                    "error": "LiteLLM Proxy 无法进入待命状态；为保护 routed 历史会话，未切换 Native。",
                },
                "rollback": {"performed": False, "reason": "config_not_modified"},
            }
        switched = switch_to_native_codex(self.paths, self.host, self.port)
        verification = CodexModeVerifier(self.paths).verify("openai")
        if not verification.get("verified"):
            backup_path = switched.get("backup_path")
            rollback = (
                restore_codex_backup(self.paths, str(backup_path))
                if backup_path
                else {"ok": False, "reason": "backup_unavailable"}
            )
            return {
                "ok": False,
                "stage": "config_verification",
                "mode": self.codex_mode_status(),
                "switch": switched,
                "verification": verification,
                "rollback": {"performed": bool(rollback.get("ok")), **rollback},
            }
        state = record_mode_switch(
            self.paths,
            CodexClientMode.NATIVE_OPENAI,
            observed_clients=observed["clients"],
            verification=verification,
        )
        return {
            "ok": bool(verification.get("verified")),
            "mode": self.codex_mode_status(),
            "switch": switched,
            "proxy": self.proxy_status(),
            "verification": verification,
            "rollback": {"performed": False},
            "generation": state["generation"],
            "notice": "OpenAI 是新会话默认 Provider；LiteLLM Proxy 保持待命以恢复 routed 历史。请重启 Codex CLI 或重新加载 VS Code。",
        }

    def force_native_codex(self, *, confirm: str) -> dict[str, Any]:
        """Write the native Codex config without making Proxy a prerequisite."""
        require_confirmation(confirm, "FORCE_SWITCH_NATIVE")
        try:
            observed = detect_codex_clients()
        except (OSError, PermissionError):
            observed = {
                "clients": [],
                "counts": {},
                "detection_status": "limited",
            }
        switched = switch_to_native_codex(self.paths, self.host, self.port)
        verification = {
            "status": "forced_without_online_verification",
            "verified": False,
            "native_auth_state": "unknown",
        }
        state = record_mode_switch(
            self.paths,
            CodexClientMode.NATIVE_OPENAI,
            observed_clients=observed.get("clients") or [],
            verification=verification,
        )
        try:
            proxy = self.proxy_status()
        except (OSError, PermissionError, TypeError, ValueError) as exc:
            proxy = {"running": False, "health": {"ok": False}, "error": str(exc)}
        warnings: list[str] = []
        if not proxy.get("running") or not proxy.get("health", {}).get("ok"):
            warnings.append("LiteLLM/4000 状态未验证或不健康；原生切换已完成，未阻止切换。")
        if observed.get("detection_status") == "limited" or observed.get("clients"):
            warnings.append("已有 Codex 客户端可能继续使用旧 Provider；请重载客户端并新建会话。")
        return {
            "ok": True,
            "forced": True,
            "stage": "config_written",
            "switch": switched,
            "generation": state["generation"],
            "mode": {
                "client_mode": CodexClientMode.NATIVE_OPENAI.value,
                "effective_provider": "openai",
                "new_session_mode": "native",
                "proxy_used_by_codex": False,
                "proxy_route_state": "standby" if proxy.get("running") else "unknown",
                "stale_client_count": len(observed.get("clients") or []),
                "restart_required": bool(observed.get("clients"))
                or observed.get("detection_status") == "limited",
                "verification": verification,
            },
            "proxy": proxy,
            "verification": verification,
            "warnings": warnings,
            "rollback": {"performed": False, "reason": "forced_fallback"},
            "notice": "Codex 配置已强制切回原生 OpenAI；LiteLLM/4000 未被停止。请重启 Codex CLI 或重新加载 VS Code 后新建会话。",
        }

    def list_backups(self) -> dict[str, Any]:
        files = sorted(self.paths.backups_dir.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
        return {
            "backups": [
                {
                    "path": str(path),
                    "name": path.name,
                    "size": path.stat().st_size,
                    "modified": path.stat().st_mtime,
                }
                for path in files[:100]
            ]
        }

    def restore_codex_config(self, *, backup_path: str, confirm: str) -> dict[str, Any]:
        require_confirmation(confirm)
        return restore_codex_backup(self.paths, backup_path)
