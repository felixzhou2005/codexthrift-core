from __future__ import annotations

import base64
import os
import time
from typing import Any

import httpx

from ..content_capture import encryption_key
from ..errors import ControlError
from ..platform.process import (
    force_kill_process,
    is_port_open,
    listening_pid,
    pid_from_file,
    process_info,
    spawn_detached,
    stop_process_tree,
    wait_for_port_closed,
)
from ..security.path_guard import require_confirmation
from ..security.redaction import redact_sensitive_text
from ..storage.atomic_io import (
    atomic_write_text,
    tail_text,
)
from .base import BaseApplicationService


class ProxyService(BaseApplicationService):
    def start_proxy(
        self,
        *,
        confirm: str,
        allow_unverified: bool = False,
    ) -> dict[str, Any]:
        require_confirmation(confirm)
        self._require_runtime()
        if not self.paths.active_config.exists():
            raise ControlError("尚未生成 active.yaml；请先 prepare_configs。")
        if not self.paths.auth_file.exists():
            raise ControlError("尚未完成 ChatGPT 订阅 OAuth。")
        if not allow_unverified:
            phase = self.active_phase()
            profile = self.active_profile()
            if not self._probe_is_ready(phase, profile):
                raise ControlError(
                    f"活动配置 {phase}/{profile} 尚未全部通过对应阶段的模型探测。"
                    "先运行模型探测；若明确接受风险，再用 allow_unverified=true。"
                )
        listener = self._listener_state()
        previous_listener_pid = listener["pid"]
        if listener["pid"] is not None:
            replacement = self._replace_existing_listener(listener)
        elif is_port_open(self.host, self.port):
            raise ControlError(
                f"{self.host}:{self.port} 已打开，但无法定位 listener PID；未启动 LiteLLM。"
            )
        else:
            replacement = None
        key = self._ensure_master_key()
        content_key = encryption_key(self.paths, create=True)
        if content_key is None:
            raise ControlError("无法读取原始调用内容的本机加密密钥。")
        env = self._runtime_env()
        env["LITELLM_MASTER_KEY"] = key
        env["CODEX_ROUTER_AUDIT_LOG"] = str(self.paths.audit_log)
        env["CODEX_ROUTER_ENCRYPTED_PAYLOAD_LOG"] = str(self.paths.encrypted_payload_log)
        env["CODEX_ROUTER_PAYLOAD_KEY"] = base64.urlsafe_b64encode(content_key).decode("ascii")
        env["CODEX_ROUTER_PROFILE"] = self.active_profile()
        env["PYTHONPATH"] = os.pathsep.join(
            [str(self.paths.config_dir), env.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
        self.paths.proxy_log.unlink(missing_ok=True)
        command = [
            str(self.paths.runtime_litellm),
            "--config",
            str(self.paths.active_config),
            "--host",
            self.host,
            "--port",
            str(self.port),
        ]
        proc = spawn_detached(
            command,
            log_path=self.paths.proxy_log,
            env=env,
            cwd=self.paths.config_dir,
        )
        atomic_write_text(self.paths.proxy_pid, f"{proc.pid}\n", mode=0o600)
        health = self._wait_for_proxy_health(
            timeout=90,
            excluded_pid=previous_listener_pid,
        )
        if not health.get("ok"):
            cleanup_error = None
            try:
                started_listener = self._listener_state()
                managed_pid = started_listener["pid"]
                if managed_pid:
                    stop_process_tree(
                        managed_pid,
                        required_fragments=["litellm"],
                        timeout=6,
                    )
            except (ControlError, OSError) as exc:
                cleanup_error = f"{type(exc).__name__}: {exc}"
            finally:
                self.paths.proxy_pid.unlink(missing_ok=True)
            return {
                "ok": False,
                "pid": proc.pid,
                "port_ready": is_port_open(self.host, self.port),
                "health": health,
                "cleanup_error": cleanup_error,
                "log_tail": redact_sensitive_text(tail_text(self.paths.proxy_log, 160), [key]),
            }
        listener = self._listener_state()
        if (
            not listener["pid"]
            or not listener["is_litellm"]
            or listener["pid"] == previous_listener_pid
        ):
            self.paths.proxy_pid.unlink(missing_ok=True)
            return {
                "ok": False,
                "pid": proc.pid,
                "port_ready": is_port_open(self.host, self.port),
                "health": {"ok": False, "error": "listener_identity_unverified"},
                "log_tail": redact_sensitive_text(tail_text(self.paths.proxy_log, 160), [key]),
            }
        managed_pid = int(listener["pid"])
        atomic_write_text(self.paths.proxy_pid, f"{managed_pid}\n", mode=0o600)
        return {
            "ok": True,
            "pid": managed_pid,
            "address": f"http://{self.host}:{self.port}",
            "phase": self.active_phase(),
            "profile": self.active_profile(),
            "health": health,
            "replacement": replacement,
        }

    def stop_proxy(self, *, confirm: str) -> dict[str, Any]:
        require_confirmation(confirm)
        listener = self._listener_state()
        if listener["pid"]:
            return {"ok": True, **self._replace_existing_listener(listener)}
        if not is_port_open(self.host, self.port):
            self.paths.proxy_pid.unlink(missing_ok=True)
            return {"ok": True, "already_stopped": True}
        raise ControlError(f"{self.host}:{self.port} 已打开，但无法定位 listener PID。")

    def restart_proxy(
        self,
        *,
        confirm: str,
        allow_unverified: bool = False,
    ) -> dict[str, Any]:
        require_confirmation(confirm)
        self.stop_proxy(confirm="APPLY")
        return self.start_proxy(confirm="APPLY", allow_unverified=allow_unverified)

    def proxy_status(self) -> dict[str, Any]:
        listener = self._listener_state()
        pid = int(listener["pid"]) if listener["pid"] else None
        info = process_info(pid)
        running = bool(listener["is_litellm"] and info.get("running"))
        health = self._proxy_health() if running else {"ok": False, "error": listener["error"]}
        key = ""
        try:
            key = self.paths.master_key_file.read_text(encoding="utf-8").strip()
        except OSError:
            pass
        return {
            "running": running,
            "pid": pid,
            "listener_pid": listener["pid"],
            # Keep the legacy diagnostic names for Desk clients; switching no
            # longer depends on config-path or model/provider comparisons.
            "listener_managed": listener["managed_by_core"],
            "listener_config": listener.get("config"),
            "listener_config_matches": listener.get("config_matches"),
            "listener_error": listener["error"],
            "listener_command": listener["command"],
            "health": health,
            "address": f"http://{self.host}:{self.port}",
            "phase": self.active_phase(),
            "profile": self.active_profile(),
            "log_tail": redact_sensitive_text(tail_text(self.paths.proxy_log, 80), [key]),
        }

    def _proxy_health(self) -> dict[str, Any]:
        if not is_port_open(self.host, self.port):
            return {"ok": False, "error": "port_closed"}
        try:
            key = self.paths.master_key_file.read_text(encoding="utf-8").strip()
            response = httpx.get(
                f"http://{self.host}:{self.port}/v1/models",
                headers={"Authorization": f"Bearer {key}"},
                timeout=3.0,
            )
            return {
                "ok": response.status_code == 200,
                "status_code": response.status_code,
            }
        except httpx.HTTPError as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def _wait_for_proxy_health(
        self,
        *,
        timeout: float,
        excluded_pid: int | None = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last_health: dict[str, Any] = {"ok": False, "error": "proxy_not_ready"}
        while time.monotonic() < deadline:
            listener = self._listener_state()
            if (
                not listener["pid"]
                or not listener["is_litellm"]
                or listener["pid"] == excluded_pid
            ):
                last_health = {"ok": False, "error": "listener_identity_unverified"}
                time.sleep(0.5)
                continue
            health = self._proxy_health()
            if health.get("ok"):
                return health
            last_health = health
            time.sleep(0.5)
        return last_health

    def _listener_state(self) -> dict[str, Any]:
        pid = listening_pid(self.host, self.port)
        if not pid:
            return {
                "pid": None,
                "running": False,
                "config": None,
                "command": [],
                "is_litellm": False,
                "managed_by_core": False,
                "error": "listener_not_found",
            }
        info = process_info(pid)
        command = list(info.get("cmdline") or [])
        command_text = " ".join(command)
        is_litellm = any("litellm" in token.casefold() for token in command)
        managed_by_core = pid_from_file(self.paths.proxy_pid) == pid
        if not info.get("running"):
            error = "listener_process_not_running"
        elif not is_litellm:
            error = "listener_is_not_litellm"
        else:
            error = None
        return {
            "pid": pid,
            "running": bool(info.get("running")),
            "config": None,
            "command": command,
            "is_litellm": is_litellm,
            "managed_by_core": managed_by_core,
            "error": error,
            "command_text": command_text,
        }

    def _replace_existing_listener(self, listener: dict[str, Any]) -> dict[str, Any]:
        pid = int(listener["pid"])
        normal_stop_error: str | None = None
        if not listener.get("managed_by_core"):
            force_kill_process(pid)
        else:
            try:
                stop_process_tree(
                    pid,
                    required_fragments=["litellm"],
                    timeout=8,
                )
            except (ControlError, OSError) as exc:
                normal_stop_error = f"{type(exc).__name__}: {exc}"

            current_pid = listening_pid(self.host, self.port)
            if current_pid or is_port_open(self.host, self.port):
                force_kill_process(int(current_pid or pid))
        if not wait_for_port_closed(self.host, self.port, timeout=20):
            raise ControlError(
                f"{self.host}:{self.port} 在停止 listener 后仍未释放。"
            )
        self.paths.proxy_pid.unlink(missing_ok=True)
        return {
            "stopped_pid": pid,
            "forced": bool(not listener.get("managed_by_core") or normal_stop_error),
            "normal_stop_error": normal_stop_error,
            "listener_error": listener.get("error"),
        }
