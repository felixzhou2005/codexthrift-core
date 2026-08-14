from __future__ import annotations

from typing import Any

from ..codex_config import (
    configure_codex,
)
from ..config_templates import (
    PROFILES,
    PhaseName,
    ProfileName,
    profile_probe_models,
    render_yaml,
)
from ..errors import ControlError
from ..platform.process import pid_from_file, process_info, spawn_detached
from ..security.path_guard import require_confirmation
from ..security.redaction import redact_sensitive_text, redact_sensitive_value
from ..storage.atomic_io import (
    atomic_write_json,
    atomic_write_text,
    backup_file,
    read_json,
    tail_text,
    utc_now_iso,
)
from .base import BaseApplicationService


class ProbeService(BaseApplicationService):
    def _validate_phase_profile(
        self,
        phase: str | None,
        profile: str | None,
    ) -> tuple[PhaseName, ProfileName]:
        selected_phase: PhaseName = phase or self.active_phase()
        selected_profile: ProfileName = profile or self.active_profile()
        if selected_phase not in {"phase2", "phase3", "phase3a"}:
            raise ControlError("phase 必须是 phase2、phase3 或 phase3a。")
        if selected_profile not in PROFILES:
            raise ControlError(f"未知模型 profile：{selected_profile}")
        return selected_phase, selected_profile

    def _probe_result(self, phase: PhaseName, profile: ProfileName) -> dict[str, Any] | None:
        value = read_json(self.paths.probe_result_for(phase, profile))
        return value if isinstance(value, dict) else None

    def _probe_is_ready(self, phase: PhaseName, profile: ProfileName) -> bool:
        result = self._probe_result(phase, profile)
        if not (
            result
            and result.get("all_ok")
            and result.get("phase") == phase
            and result.get("profile") == profile
        ):
            return False
        actual = {
            (str(item.get("model")), str(item.get("effort")))
            for item in result.get("results") or []
            if isinstance(item, dict) and item.get("ok")
        }
        expected = {
            (str(item["model"]), str(item.get("effort")))
            for item in profile_probe_models(profile, phase)
        }
        return expected <= actual

    def start_model_probe(
        self,
        *,
        profile: ProfileName | None = None,
        phase: PhaseName | None = None,
        confirm: str,
    ) -> dict[str, Any]:
        require_confirmation(confirm)
        self._require_runtime()
        if not self.paths.auth_file.exists():
            raise ControlError("尚未发现 OAuth auth.json；请先完成订阅 OAuth。")
        selected_phase, selected_profile = self._validate_phase_profile(phase, profile)
        running = process_info(pid_from_file(self.paths.probe_pid))
        if running.get("running"):
            return {
                "ok": True,
                "already_running": True,
                **self.model_probe_status(),
            }

        result_path = self.paths.probe_result_for(selected_phase, selected_profile)
        log_path = self.paths.probe_log_for(selected_phase, selected_profile)
        specs_path = self.paths.probe_spec_for(selected_phase, selected_profile)
        result_path.unlink(missing_ok=True)
        log_path.unlink(missing_ok=True)
        specs = profile_probe_models(selected_profile, selected_phase)
        atomic_write_json(specs_path, specs, mode=0o600)
        command = [
            str(self.paths.runtime_python),
            str(self.paths.model_probe_script),
            str(specs_path),
            str(result_path),
            selected_phase,
            selected_profile,
        ]
        proc = spawn_detached(
            command,
            log_path=log_path,
            env=self._runtime_env(),
            cwd=self.paths.run_dir,
        )
        atomic_write_text(self.paths.probe_pid, f"{proc.pid}\n", mode=0o600)
        atomic_write_json(
            self.paths.probe_job_meta,
            {
                "pid": proc.pid,
                "phase": selected_phase,
                "profile": selected_profile,
                "spec_path": str(specs_path),
                "result_path": str(result_path),
                "log_path": str(log_path),
                "started_at": utc_now_iso(),
            },
            mode=0o600,
        )
        return {
            "ok": True,
            "started": True,
            "pid": proc.pid,
            "phase": selected_phase,
            "profile": selected_profile,
            "models": specs,
            "notice": "模型探测会消耗少量订阅额度；相同模型与 effort 组合已去重。",
        }

    def model_probe_status(
        self,
        *,
        phase: PhaseName | None = None,
        profile: ProfileName | None = None,
    ) -> dict[str, Any]:
        job = read_json(self.paths.probe_job_meta, {})
        if not isinstance(job, dict):
            job = {}
        selected_phase, selected_profile = self._validate_phase_profile(
            phase or job.get("phase"),
            profile or job.get("profile"),
        )
        pid = pid_from_file(self.paths.probe_pid)
        info = process_info(pid)
        result_path = self.paths.probe_result_for(selected_phase, selected_profile)
        log_path = self.paths.probe_log_for(selected_phase, selected_profile)
        saved: list[dict[str, Any]] = []
        for candidate_phase in ("phase2", "phase3", "phase3a"):
            for candidate_profile in PROFILES:
                candidate = self._probe_result(candidate_phase, candidate_profile)  # type: ignore[arg-type]
                if candidate:
                    saved.append({
                        "phase": candidate_phase,
                        "profile": candidate_profile,
                        "all_ok": bool(candidate.get("all_ok")),
                        "timestamp": candidate.get("timestamp"),
                        "result_path": str(self.paths.probe_result_for(candidate_phase, candidate_profile)),
                    })
        return {
            "running": info.get("running", False),
            "pid": pid,
            "job": job,
            "requested": {"phase": selected_phase, "profile": selected_profile},
            "result": redact_sensitive_value(read_json(result_path)),
            "saved_results": saved,
            "log_tail": redact_sensitive_text(tail_text(log_path, 120)),
        }

    def activate_phase(
        self,
        *,
        phase: PhaseName,
        profile: ProfileName | None = None,
        confirm: str,
    ) -> dict[str, Any]:
        require_confirmation(confirm)
        selected_phase, selected_profile = self._validate_phase_profile(phase, profile)
        if not self._probe_is_ready(selected_phase, selected_profile):
            raise ControlError(
                f"{selected_phase}/{selected_profile} 尚未全部通过独立模型探测；"
                "先运行 probe_subscription_models 指定相同 phase/profile。"
            )
        backup = backup_file(self.paths.active_config, self.paths.backups_dir, "active-litellm")
        rendered = render_yaml(selected_phase, selected_profile)
        atomic_write_text(self.paths.active_config, rendered, mode=0o600)
        atomic_write_text(self.paths.active_phase_file, selected_phase + "\n", mode=0o600)
        atomic_write_text(self.paths.active_profile_file, selected_profile + "\n", mode=0o600)
        atomic_write_text(self.paths.active_policy_file, "standard\n", mode=0o600)
        self._validate_config(self.paths.active_config)
        codex_result = configure_codex(self.paths, selected_phase, self.host, self.port)
        restarted = False
        if self.proxy_status().get("running"):
            self.restart_proxy(confirm="APPLY", allow_unverified=False)
            restarted = True
        return {
            "ok": True,
            "phase": selected_phase,
            "profile": selected_profile,
            "active_config": str(self.paths.active_config),
            "config_backup": str(backup) if backup else None,
            "codex": codex_result,
            "proxy_restarted": restarted,
        }
