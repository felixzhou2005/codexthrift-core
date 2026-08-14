from __future__ import annotations

import os
import platform
import secrets
import sys
from pathlib import Path
from typing import Any, Literal

import yaml

from ..codex_config import (
    configure_codex,
)
from ..config_templates import (
    AUDIT_CALLBACK_SOURCE,
    MODEL_PROBE_SOURCE,
    OAUTH_PROBE_SOURCE,
    PROFILES,
    RUNTIME_INSTALL_SOURCE,
    PhaseName,
    ProfileName,
    profile_probe_models,
    render_phase3a_yaml,
    render_phase21_yaml,
    render_yaml,
)
from ..errors import ControlError
from ..platform.process import (
    is_port_open,
    normalized_child_env,
    pid_from_file,
    process_info,
    spawn_detached,
    which,
)
from ..routing_policy import runtime_source as routing_policy_runtime_source
from ..runtime_templates import ROUTING_CALLBACK_SOURCE
from ..security.path_guard import require_confirmation
from ..security.redaction import redact_sensitive_value
from ..storage.atomic_io import (
    atomic_write_text,
    backup_file,
    read_json,
    tail_text,
)
from .base import BaseApplicationService

ReleaseChannel = Literal["stable"]

STABLE_LITELLM_SPEC = "litellm[proxy]==1.95.0"
STABLE_FASTAPI_SPEC = "fastapi==0.136.3"
STABLE_CRYPTOGRAPHY_SPEC = "cryptography>=44,<51"
STABLE_RUNTIME_SPECS = (STABLE_LITELLM_SPEC, STABLE_FASTAPI_SPEC, STABLE_CRYPTOGRAPHY_SPEC)


class RuntimeService(BaseApplicationService):
    def environment_check(self) -> dict[str, Any]:
        return {
            "platform": platform.platform(),
            "python": {
                "executable": sys.executable,
                "version": platform.python_version(),
                "supported": sys.version_info >= (3, 11),
            },
            "commands": {
                name: which(name)
                for name in ("git", "codex", "tunnel-client", "node", "npm")
            },
            "workspace": str(self.paths.root),
            "workspace_writable": os.access(self.paths.root, os.W_OK),
            "codex_config": str(self.paths.codex_config),
            "runtime_source": self.paths.runtime_source,
            "runtime_python": str(self.paths.runtime_python),
            "runtime_installed": self.paths.runtime_python.exists(),
            "proxy_address": f"http://{self.host}:{self.port}",
            "proxy_port_open": is_port_open(self.host, self.port),
        }

    def _ensure_master_key(self) -> str:
        if self.paths.master_key_file.exists():
            value = self.paths.master_key_file.read_text(encoding="utf-8").strip()
            if value:
                return value
        value = "sk-local-" + secrets.token_urlsafe(32)
        atomic_write_text(self.paths.master_key_file, value + "\n", mode=0o600)
        return value

    def prepare_configs(
        self,
        *,
        profile: ProfileName = "gpt56",
        confirm: str,
    ) -> dict[str, Any]:
        require_confirmation(confirm)
        if profile not in PROFILES:
            raise ControlError(f"未知模型 profile：{profile}")
        self.paths.ensure_mutable()
        self._ensure_master_key()
        atomic_write_text(self.paths.phase2_config, render_yaml("phase2", profile), mode=0o600)
        atomic_write_text(self.paths.phase3_config, render_yaml("phase3", profile), mode=0o600)
        atomic_write_text(self.paths.phase3a_config, render_phase3a_yaml(profile), mode=0o600)
        atomic_write_text(self.paths.active_config, render_yaml("phase2", profile), mode=0o600)
        atomic_write_text(self.paths.audit_callback, AUDIT_CALLBACK_SOURCE, mode=0o600)
        atomic_write_text(self.paths.oauth_script, OAUTH_PROBE_SOURCE, mode=0o700)
        atomic_write_text(self.paths.model_probe_script, MODEL_PROBE_SOURCE, mode=0o700)
        atomic_write_text(self.paths.runtime_install_script, RUNTIME_INSTALL_SOURCE, mode=0o700)
        atomic_write_text(self.paths.active_phase_file, "phase2\n", mode=0o600)
        atomic_write_text(self.paths.active_profile_file, profile + "\n", mode=0o600)
        atomic_write_text(self.paths.active_policy_file, "standard\n", mode=0o600)
        self._validate_config(self.paths.phase2_config)
        self._validate_config(self.paths.phase3_config)
        self._validate_config(self.paths.phase3a_config)
        return {
            "ok": True,
            "profile": profile,
            "active_phase": "phase2",
            "phase2_config": str(self.paths.phase2_config),
            "phase3_config": str(self.paths.phase3_config),
            "phase3a_config": str(self.paths.phase3a_config),
            "active_config": str(self.paths.active_config),
            "master_key_created": True,
            "security": {
                "proxy_bind": self.host,
                "token_dir": str(self.paths.token_dir),
                "prompt_content_logged": False,
            },
        }

    def prepare_phase21_candidate(
        self,
        *,
        profile: ProfileName = "gpt56",
        confirm: str,
    ) -> dict[str, Any]:
        require_confirmation(confirm)
        if profile not in PROFILES:
            raise ControlError(f"未知模型 profile：{profile}")
        self.paths.ensure_mutable()
        rendered = render_phase21_yaml(profile)
        policy_source = routing_policy_runtime_source()
        for source, name in (
            (policy_source, "codex_routing_policy.py"),
            (ROUTING_CALLBACK_SOURCE, "routing_callback_candidate.py"),
            (AUDIT_CALLBACK_SOURCE, "audit_callback_candidate.py"),
        ):
            try:
                compile(source, name, "exec")
            except SyntaxError as exc:
                raise ControlError(f"Phase 2.1 运行模块无法编译：{name}: {exc}") from exc
        atomic_write_text(self.paths.phase21_config, rendered, mode=0o600)
        atomic_write_text(self.paths.phase21_routing_policy, policy_source, mode=0o600)
        atomic_write_text(self.paths.phase21_routing_callback, ROUTING_CALLBACK_SOURCE, mode=0o600)
        atomic_write_text(self.paths.phase21_audit_callback, AUDIT_CALLBACK_SOURCE, mode=0o600)
        self._validate_config(self.paths.phase21_config)
        return {
            "ok": True,
            "candidate": "phase2.1",
            "profile": profile,
            "activated": False,
            "candidate_config": str(self.paths.phase21_config),
            "runtime_modules": [
                str(self.paths.phase21_routing_policy),
                str(self.paths.phase21_routing_callback),
                str(self.paths.phase21_audit_callback),
            ],
            "active_config_unchanged": True,
            "subscription_calls": 0,
        }

    def activate_phase21(
        self,
        *,
        profile: ProfileName | None = None,
        confirm: str,
    ) -> dict[str, Any]:
        require_confirmation(confirm)
        selected_profile: ProfileName = profile or self.active_profile()
        if selected_profile not in PROFILES:
            raise ControlError(f"未知模型 profile：{selected_profile}")
        required = (
            self.paths.phase21_config,
            self.paths.phase21_routing_policy,
            self.paths.phase21_routing_callback,
            self.paths.phase21_audit_callback,
        )
        missing = [str(path) for path in required if not path.exists()]
        if missing:
            raise ControlError(f"Phase 2.1 候选文件不完整：{missing}")
        if not self._probe_is_ready("phase2", selected_profile):
            raise ControlError(f"phase2/{selected_profile} 尚未通过对应模型探测。")
        self._validate_config(self.paths.phase21_config)
        backup = backup_file(self.paths.active_config, self.paths.backups_dir, "active-litellm")
        atomic_write_text(
            self.paths.active_config,
            self.paths.phase21_config.read_text(encoding="utf-8"),
            mode=0o600,
        )
        atomic_write_text(self.paths.active_phase_file, "phase2\n", mode=0o600)
        atomic_write_text(self.paths.active_profile_file, selected_profile + "\n", mode=0o600)
        atomic_write_text(self.paths.active_policy_file, "phase2.1\n", mode=0o600)
        restarted = False
        if self.proxy_status().get("running"):
            self.restart_proxy(confirm="APPLY", allow_unverified=False)
            restarted = True
        return {
            "ok": True,
            "phase": "phase2",
            "policy": "phase2.1",
            "profile": selected_profile,
            "active_config": str(self.paths.active_config),
            "config_backup": str(backup) if backup else None,
            "proxy_restarted": restarted,
        }

    def prepare_phase3a_candidate(
        self,
        *,
        profile: ProfileName = "gpt56",
        confirm: str,
    ) -> dict[str, Any]:
        """Write an isolated Phase 3A candidate without changing the proxy."""
        require_confirmation(confirm)
        if profile not in PROFILES:
            raise ControlError(f"未知模型 profile：{profile}")
        self.paths.ensure_mutable()
        policy_source = routing_policy_runtime_source()
        rendered = render_phase3a_yaml(profile)
        for source, name in (
            (policy_source, "codex_routing_policy_phase3a.py"),
            (ROUTING_CALLBACK_SOURCE, "routing_callback_phase3a.py"),
            (AUDIT_CALLBACK_SOURCE, "audit_callback_phase3a.py"),
        ):
            try:
                compile(source, name, "exec")
            except SyntaxError as exc:
                raise ControlError(f"Phase 3A 运行模块无法编译：{name}: {exc}") from exc
        atomic_write_text(self.paths.phase3a_config, rendered, mode=0o600)
        atomic_write_text(self.paths.phase3a_routing_policy, policy_source, mode=0o600)
        atomic_write_text(self.paths.phase3a_routing_callback, ROUTING_CALLBACK_SOURCE, mode=0o600)
        atomic_write_text(self.paths.phase3a_audit_callback, AUDIT_CALLBACK_SOURCE, mode=0o600)
        self._validate_config(self.paths.phase3a_config)
        return {
            "ok": True,
            "candidate": "phase3a",
            "profile": profile,
            "activated": False,
            "candidate_config": str(self.paths.phase3a_config),
            "runtime_modules": [
                str(self.paths.phase3a_routing_policy),
                str(self.paths.phase3a_routing_callback),
                str(self.paths.phase3a_audit_callback),
            ],
            "required_probe": profile_probe_models(profile, "phase3a"),
            "subscription_calls": 0,
        }

    def activate_phase3a(
        self,
        *,
        profile: ProfileName | None = None,
        confirm: str,
    ) -> dict[str, Any]:
        """Activate the separately probed Phase 3A mapping, never Phase 3."""
        require_confirmation(confirm)
        selected_profile: ProfileName = profile or self.active_profile()
        if selected_profile not in PROFILES:
            raise ControlError(f"未知模型 profile：{selected_profile}")
        required = (
            self.paths.phase3a_config,
            self.paths.phase3a_routing_policy,
            self.paths.phase3a_routing_callback,
            self.paths.phase3a_audit_callback,
        )
        missing = [str(path) for path in required if not path.exists()]
        if missing:
            raise ControlError(f"Phase 3A 候选文件不完整：{missing}")
        if not self._probe_is_ready("phase3a", selected_profile):
            raise ControlError(f"phase3a/{selected_profile} 尚未通过 Luna/low、Terra/medium、Sol/high、Sol/xhigh 探测。")
        if not self._probe_is_ready("phase2", selected_profile):
            raise ControlError(f"phase2/{selected_profile} 尚未保留 Sol/medium 的已验证探测结果。")
        self._validate_config(self.paths.phase3a_config)
        backup = backup_file(self.paths.active_config, self.paths.backups_dir, "active-litellm")
        atomic_write_text(self.paths.active_config, self.paths.phase3a_config.read_text(encoding="utf-8"), mode=0o600)
        atomic_write_text(self.paths.phase21_routing_policy, self.paths.phase3a_routing_policy.read_text(encoding="utf-8"), mode=0o600)
        atomic_write_text(self.paths.phase21_routing_callback, self.paths.phase3a_routing_callback.read_text(encoding="utf-8"), mode=0o600)
        atomic_write_text(self.paths.phase21_audit_callback, self.paths.phase3a_audit_callback.read_text(encoding="utf-8"), mode=0o600)
        atomic_write_text(self.paths.active_phase_file, "phase3a\n", mode=0o600)
        atomic_write_text(self.paths.active_profile_file, selected_profile + "\n", mode=0o600)
        atomic_write_text(self.paths.active_policy_file, "phase3a\n", mode=0o600)
        codex_result = configure_codex(self.paths, "phase3a", self.host, self.port)
        restarted = False
        if self.proxy_status().get("running"):
            self.restart_proxy(confirm="APPLY", allow_unverified=False)
            restarted = True
        return {
            "ok": True,
            "phase": "phase3a",
            "policy": "phase3a",
            "profile": selected_profile,
            "active_config": str(self.paths.active_config),
            "config_backup": str(backup) if backup else None,
            "codex": codex_result,
            "proxy_restarted": restarted,
        }

    def rollback_pending_phase3a(self, *, confirm: str) -> dict[str, Any]:
        """Restore Phase 2.1 files when a sandbox cannot restart the proxy.

        The running proxy has not loaded Phase 3A until a restart succeeds, so
        restoring the disk and Codex settings makes the visible state match the
        still-running process without sending any signal to that process.
        """
        require_confirmation(confirm)
        if not self._probe_is_ready("phase2", self.active_profile()):
            raise ControlError("phase2 当前 profile 未通过探测，拒绝恢复。")
        required = (
            self.paths.phase21_config,
            self.paths.phase21_routing_policy,
            self.paths.phase21_routing_callback,
            self.paths.phase21_audit_callback,
        )
        missing = [str(path) for path in required if not path.exists()]
        if missing:
            raise ControlError(f"无法恢复 Phase 2.1，候选文件缺失：{missing}")
        backup = backup_file(self.paths.active_config, self.paths.backups_dir, "active-litellm-before-phase3a-recovery")
        atomic_write_text(self.paths.active_config, self.paths.phase21_config.read_text(encoding="utf-8"), mode=0o600)
        atomic_write_text(self.paths.active_phase_file, "phase2\n", mode=0o600)
        atomic_write_text(self.paths.active_policy_file, "phase2.1\n", mode=0o600)
        codex_result = configure_codex(self.paths, "phase2", self.host, self.port)
        return {
            "ok": True,
            "phase": "phase2",
            "policy": "phase2.1",
            "active_config_backup": str(backup) if backup else None,
            "codex": codex_result,
            "proxy_restart_attempted": False,
            "notice": "The running proxy was left untouched because it was already using the pre-restart Phase 2.1 process.",
        }

    @staticmethod
    def _validate_config(path: Path) -> None:
        try:
            parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ControlError(f"YAML 配置无效：{path}: {exc}") from exc
        if not isinstance(parsed, dict) or not parsed.get("model_list"):
            raise ControlError(f"YAML 配置缺少 model_list：{path}")
        names = {
            item.get("model_name")
            for item in parsed.get("model_list", [])
            if isinstance(item, dict)
        }
        if "codex-auto" not in names:
            raise ControlError(f"YAML 配置缺少 codex-auto：{path}")

    def start_runtime_install(
        self,
        *,
        release: ReleaseChannel = "stable",
        confirm: str,
    ) -> dict[str, Any]:
        require_confirmation(confirm)
        if release != "stable":
            raise ControlError("当前实现只允许安装经过方案验证的 stable 版本。")
        if self.paths.bundled_runtime_root is not None:
            self._require_runtime()
            return {
                "ok": True,
                "already_installed": True,
                "source": "bundled",
                "runtime_root": str(self.paths.bundled_runtime_root),
            }
        running = process_info(pid_from_file(self.paths.install_pid))
        if running.get("running"):
            return {"ok": True, "already_running": True, **running}
        for path in (self.paths.install_log, self.paths.install_result):
            path.unlink(missing_ok=True)
        command = [
            sys.executable,
            str(self.paths.runtime_install_script),
            str(self.paths.runtime_venv),
            str(self.paths.install_result),
            *STABLE_RUNTIME_SPECS,
        ]
        proc = spawn_detached(command, log_path=self.paths.install_log, env=self._runtime_install_env())
        atomic_write_text(self.paths.install_pid, f"{proc.pid}\n", mode=0o600)
        return {
            "ok": True,
            "started": True,
            "pid": proc.pid,
            "version_specs": list(STABLE_RUNTIME_SPECS),
            "status_tool": "runtime_install_status",
        }

    def runtime_install_status(self) -> dict[str, Any]:
        pid = pid_from_file(self.paths.install_pid)
        info = process_info(pid)
        result = read_json(self.paths.install_result)
        return {
            "running": info.get("running", False),
            "pid": pid,
            "result": redact_sensitive_value(result),
            "runtime_source": self.paths.runtime_source,
            "runtime_python_exists": self.paths.runtime_python.exists(),
            "runtime_litellm_exists": self.paths.runtime_litellm.exists(),
            "log_tail": tail_text(self.paths.install_log, 80),
        }

    def active_phase(self) -> PhaseName:
        try:
            value = self.paths.active_phase_file.read_text(encoding="utf-8").strip()
        except OSError:
            value = "phase2"
        return value if value in {"phase2", "phase3", "phase3a"} else "phase2"  # type: ignore[return-value]

    def active_profile(self) -> ProfileName:
        try:
            value = self.paths.active_profile_file.read_text(encoding="utf-8").strip()
        except OSError:
            value = "gpt56"
        return value if value in PROFILES else "gpt56"  # type: ignore[return-value]

    def active_policy(self) -> str:
        try:
            value = self.paths.active_policy_file.read_text(encoding="utf-8").strip()
        except OSError:
            value = "standard"
        return value if value in {"phase2.1", "phase3a"} else "standard"

    def _require_runtime(self) -> None:
        if not self.paths.runtime_python.exists() or not self.paths.runtime_litellm.exists():
            raise ControlError("LiteLLM runtime 尚未安装完成。请先启动安装并检查结果。")

    def _runtime_env(self) -> dict[str, str]:
        env = normalized_child_env()
        # Managed runtime commands establish their own Python environment. A
        # bundled Core may require PYTHONHOME/PYTHONPATH, but leaking either to
        # a child wrapper or legacy venv can load incompatible native modules.
        env.pop("PYTHONHOME", None)
        env.pop("PYTHONPATH", None)
        # LiteLLM treats DEBUG as a strict boolean; Codex may set DEBUG=release.
        env.pop("DEBUG", None)
        env["CHATGPT_TOKEN_DIR"] = str(self.paths.token_dir)
        env["CHATGPT_AUTH_FILE"] = "auth.json"
        env["CODEX_ROUTER_STATIC_PHASE"] = self.active_phase()
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def _runtime_install_env(self) -> dict[str, str]:
        env = normalized_child_env()
        cache_root = self.paths.root / "cache"
        pip_cache = cache_root / "pip"
        xdg_cache = cache_root / "xdg"
        cargo_home = cache_root / "cargo"
        rustup_home = cache_root / "rustup"
        for directory in (cache_root, pip_cache, xdg_cache, cargo_home, rustup_home):
            directory.mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                directory.chmod(0o700)
        env["PIP_CACHE_DIR"] = str(pip_cache)
        env["XDG_CACHE_HOME"] = str(xdg_cache)
        env["CARGO_HOME"] = str(cargo_home)
        env["RUSTUP_HOME"] = str(rustup_home)
        env["PATH"] = os.pathsep.join([str(cargo_home / "bin"), env.get("PATH", "")])
        return env
