from __future__ import annotations

from typing import Any

from ..codex_config import (
    codex_config_status,
)
from ..errors import ControlError
from ..platform.process import pid_from_file, process_info
from ..reporting import (
    explain_turn,
    recent_calls,
    recent_turns,
    record_feedback,
)
from ..security.redaction import redact_sensitive_text, redact_sensitive_value
from ..storage.atomic_io import (
    read_json,
    tail_text,
    utc_now_iso,
)
from .base import BaseApplicationService


class DiagnosticsService(BaseApplicationService):
    def setup_guide(self) -> dict[str, Any]:
        phase = self.active_phase()
        profile = self.active_profile()
        runtime_ready = self.paths.runtime_python.exists() and self.paths.runtime_litellm.exists()
        config_ready = self.paths.active_config.exists()
        auth_ready = self.paths.auth_file.exists()
        probe_ready = self._probe_is_ready(phase, profile)
        proxy_running = bool(self.proxy_status().get("running"))
        codex_state = codex_config_status(self.paths, phase, self.host, self.port)
        codex_ready = bool(codex_state.get("configured"))

        if not config_ready:
            next_action = {
                "tool": "prepare_phase2",
                "reason": "尚未生成 YAML 配置与本地密钥。",
                "confirmation": "APPLY",
            }
        elif not runtime_ready:
            next_action = {
                "tool": "start_runtime_install",
                "reason": "LiteLLM 隔离运行环境尚未安装。",
                "confirmation": "APPLY",
            }
        elif not auth_ready:
            next_action = {
                "tool": "begin_subscription_oauth",
                "reason": "尚未完成个人 ChatGPT/Codex 订阅 OAuth。",
                "confirmation": "APPLY",
            }
        elif not probe_ready:
            next_action = {
                "tool": "probe_subscription_models",
                "arguments": {"phase": phase, "profile": profile},
                "reason": f"{phase}/{profile} 的目标模型与 effort 尚未全部验证可用。",
                "confirmation": "APPLY",
            }
        elif not proxy_running:
            next_action = {
                "tool": "start_litellm_proxy",
                "reason": "模型已验证，但本机 LiteLLM Proxy 尚未运行。",
                "confirmation": "APPLY",
            }
        elif not codex_ready:
            next_action = {
                "tool": "configure_codex_client",
                "arguments": {"phase": phase},
                "reason": "需要把 Codex Responses API 指向本机代理。",
                "confirmation": "APPLY",
            }
        elif phase == "phase2":
            next_action = {
                "tool": "routing_report",
                "reason": (
                    "第二阶段已运行。建议先确认 Phase 2.1 已重新加载；"
                    "准备升级时只探测 phase3a 的 Luna/low、Terra/medium、Sol/high、Sol/xhigh。"
                ),
                "confirmation": None,
            }
        elif phase == "phase3a":
            next_action = {
                "tool": "routing_report",
                "reason": "Phase 3A 已运行；持续审计 Sol/high 与验证失败后的单次 Sol/xhigh 救援效果。",
                "confirmation": None,
            }
        else:
            next_action = {
                "tool": "routing_report",
                "reason": "原版第三阶段已运行；建议回退到 Phase 2.1 后改用独立 Phase 3A。",
                "confirmation": None,
            }
        return {
            "ok": True,
            "ready": {
                "config": config_ready,
                "runtime": runtime_ready,
                "oauth": auth_ready,
                "model_probe": probe_ready,
                "proxy": proxy_running,
                "codex_config": codex_ready,
            },
            "codex_config_status": codex_state,
            "phase": phase,
            "profile": profile,
            "next_action": next_action,
            "phase3_gate": {
                "probe_ready": self._probe_is_ready("phase3", profile),
                "required_before_activation": True,
            },
            "phase3a_gate": {
                "probe_ready": self._probe_is_ready("phase3a", profile),
                "required_before_activation": True,
            },
            "manual_boundary": (
                "OAuth 浏览器授权以及第一次创建 OpenAI Secure MCP Tunnel/App 连接需要用户亲自完成；"
                "连接建立后，其余白名单动作可从 ChatGPT App 调用。"
            ),
        }

    def full_status(self) -> dict[str, Any]:
        phase = self.active_phase()
        profile = self.active_profile()
        return {
            "timestamp": utc_now_iso(),
            "environment": self.environment_check(),
            "active_phase": phase,
            "active_profile": profile,
            "active_policy": self.active_policy(),
            "oauth": {
                "auth_file_exists": self.paths.auth_file.exists(),
                "job": process_info(pid_from_file(self.paths.oauth_pid)),
                "result": redact_sensitive_value(read_json(self.paths.oauth_result)),
            },
            "model_probe": self.model_probe_status(phase=phase, profile=profile),
            "runtime_install": {
                "job": process_info(pid_from_file(self.paths.install_pid)),
                "result": redact_sensitive_value(read_json(self.paths.install_result)),
            },
            "proxy": self.proxy_status(),
            "codex_config": codex_config_status(self.paths, phase, self.host, self.port),
            "codex_mode": self.codex_mode_status(),
            "files": {
                "workspace": str(self.paths.root),
                "active_config": str(self.paths.active_config),
                "audit_log": str(self.paths.audit_log),
            },
        }

    def logs(self, *, component: str = "proxy", lines: int = 100) -> dict[str, Any]:
        job = read_json(self.paths.probe_job_meta, {})
        if not isinstance(job, dict):
            job = {}
        probe_phase, probe_profile = self._validate_phase_profile(
            job.get("phase"), job.get("profile")
        )
        mapping = {
            "proxy": self.paths.proxy_log,
            "oauth": self.paths.oauth_log,
            "probe": self.paths.probe_log_for(probe_phase, probe_profile),
            "install": self.paths.install_log,
        }
        if component not in mapping:
            raise ControlError(f"component 必须是：{', '.join(mapping)}")
        key = ""
        try:
            key = self.paths.master_key_file.read_text(encoding="utf-8").strip()
        except OSError:
            pass
        return {
            "component": component,
            "path": str(mapping[component]),
            "tail": redact_sensitive_text(tail_text(mapping[component], lines), [key]),
        }

    def recent_calls(self, *, limit: int = 20) -> dict[str, Any]:
        return recent_calls(self.paths, limit=limit)

    def recent_turns(self, *, limit: int = 20) -> dict[str, Any]:
        return recent_turns(self.paths, limit=limit)

    def explain_turn(self, *, turn_id: str) -> dict[str, Any]:
        return explain_turn(self.paths, turn_id=turn_id)

    def record_feedback(
        self,
        *,
        prompt_fingerprint: str | None,
        logical_turn_id: str | None,
        expected_tier: str,
        outcome: str,
        note: str = "",
    ) -> dict[str, Any]:
        return record_feedback(
            self.paths,
            prompt_fingerprint=prompt_fingerprint,
            logical_turn_id=logical_turn_id,
            expected_tier=expected_tier,
            outcome=outcome,
            note=note,
        )
