from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .application.runtime_service import (
    STABLE_RUNTIME_SPECS,
    ReleaseChannel,
    RuntimeService,
)
from .application.services import ApplicationServices
from .application.task_service import datetime_id
from .config_templates import PhaseName, ProfileName
from .history_store import HistoryStore
from .paths import WorkspacePaths

__all__ = ["STABLE_RUNTIME_SPECS", "RouterController", "datetime_id"]


class RouterController:
    """Compatibility facade delegating application behavior to focused services."""

    def __init__(
        self,
        paths: WorkspacePaths | None = None,
        *,
        host: str = "127.0.0.1",
        port: int = 4000,
        ensure_paths: bool = False,
    ) -> None:
        self.paths = paths or WorkspacePaths.default()
        self.host = host
        self.port = port
        if ensure_paths:
            self.paths.ensure_mutable()
        self._proxy_desired_state_writer: Callable[[bool, bool], None] | None = None
        self.services = ApplicationServices.create(self)

    def bind_proxy_desired_state_writer(
        self,
        writer: Callable[[bool, bool], None],
    ) -> None:
        self._proxy_desired_state_writer = writer

    def environment_check(self) -> dict[str, Any]:
        return self.services.runtime.environment_check()

    def _ensure_master_key(self) -> str:
        return self.services.runtime._ensure_master_key()

    def prepare_configs(
        self,
        *,
        profile: ProfileName = "gpt56",
        confirm: str,
    ) -> dict[str, Any]:
        return self.services.runtime.prepare_configs(profile=profile, confirm=confirm)

    def prepare_phase21_candidate(
        self,
        *,
        profile: ProfileName = "gpt56",
        confirm: str,
    ) -> dict[str, Any]:
        return self.services.runtime.prepare_phase21_candidate(profile=profile, confirm=confirm)

    def activate_phase21(
        self,
        *,
        profile: ProfileName | None = None,
        confirm: str,
    ) -> dict[str, Any]:
        return self.services.runtime.activate_phase21(profile=profile, confirm=confirm)

    def prepare_phase3a_candidate(
        self,
        *,
        profile: ProfileName = "gpt56",
        confirm: str,
    ) -> dict[str, Any]:
        return self.services.runtime.prepare_phase3a_candidate(profile=profile, confirm=confirm)

    def activate_phase3a(
        self,
        *,
        profile: ProfileName | None = None,
        confirm: str,
    ) -> dict[str, Any]:
        return self.services.runtime.activate_phase3a(profile=profile, confirm=confirm)

    def rollback_pending_phase3a(self, *, confirm: str) -> dict[str, Any]:
        return self.services.runtime.rollback_pending_phase3a(confirm=confirm)

    @staticmethod
    def _validate_config(path: Path) -> None:
        return RuntimeService._validate_config(path)

    def start_runtime_install(
        self,
        *,
        release: ReleaseChannel = "stable",
        confirm: str,
    ) -> dict[str, Any]:
        return self.services.runtime.start_runtime_install(release=release, confirm=confirm)

    def runtime_install_status(self) -> dict[str, Any]:
        return self.services.runtime.runtime_install_status()

    def begin_oauth(self, *, confirm: str) -> dict[str, Any]:
        return self.services.oauth.begin_oauth(confirm=confirm)

    def oauth_status(self) -> dict[str, Any]:
        return self.services.oauth.oauth_status()

    def _validate_phase_profile(
        self,
        phase: str | None,
        profile: str | None,
    ) -> tuple[PhaseName, ProfileName]:
        return self.services.probe._validate_phase_profile(phase, profile)

    def _probe_result(self, phase: PhaseName, profile: ProfileName) -> dict[str, Any] | None:
        return self.services.probe._probe_result(phase, profile)

    def _probe_is_ready(self, phase: PhaseName, profile: ProfileName) -> bool:
        return self.services.probe._probe_is_ready(phase, profile)

    def start_model_probe(
        self,
        *,
        profile: ProfileName | None = None,
        phase: PhaseName | None = None,
        confirm: str,
    ) -> dict[str, Any]:
        return self.services.probe.start_model_probe(profile=profile, phase=phase, confirm=confirm)

    def model_probe_status(
        self,
        *,
        phase: PhaseName | None = None,
        profile: ProfileName | None = None,
    ) -> dict[str, Any]:
        return self.services.probe.model_probe_status(phase=phase, profile=profile)

    def activate_phase(
        self,
        *,
        phase: PhaseName,
        profile: ProfileName | None = None,
        confirm: str,
    ) -> dict[str, Any]:
        return self.services.probe.activate_phase(phase=phase, profile=profile, confirm=confirm)

    def configure_codex(
        self,
        *,
        phase: PhaseName | None = None,
        confirm: str,
        allow_unverified: bool = False,
    ) -> dict[str, Any]:
        return self.services.codex_mode.configure_codex(
            phase=phase,
            confirm=confirm,
            allow_unverified=allow_unverified,
        )

    def codex_mode_status(self) -> dict[str, Any]:
        return self.services.codex_mode.codex_mode_status()

    def enable_smart_routing(self, *, confirm: str, allow_unverified: bool = False) -> dict[str, Any]:
        return self.services.codex_mode.enable_smart_routing(confirm=confirm, allow_unverified=allow_unverified)

    def switch_to_native_codex(self, *, confirm: str) -> dict[str, Any]:
        return self.services.codex_mode.switch_to_native_codex(confirm=confirm)

    def force_native_codex(self, *, confirm: str) -> dict[str, Any]:
        return self.services.codex_mode.force_native_codex(confirm=confirm)

    def start_proxy(
        self,
        *,
        confirm: str,
        allow_unverified: bool = False,
    ) -> dict[str, Any]:
        result = self.services.proxy.start_proxy(
            confirm=confirm,
            allow_unverified=allow_unverified,
        )
        self._record_proxy_desired_state(result, running=True, allow_unverified=allow_unverified)
        return result

    def stop_proxy(self, *, confirm: str) -> dict[str, Any]:
        result = self.services.proxy.stop_proxy(confirm=confirm)
        self._record_proxy_desired_state(result, running=False)
        return result

    def restart_proxy(
        self,
        *,
        confirm: str,
        allow_unverified: bool = False,
    ) -> dict[str, Any]:
        result = self.services.proxy.restart_proxy(
            confirm=confirm,
            allow_unverified=allow_unverified,
        )
        self._record_proxy_desired_state(result, running=True, allow_unverified=allow_unverified)
        return result

    def _record_proxy_desired_state(
        self,
        result: dict[str, Any],
        *,
        running: bool,
        allow_unverified: bool = False,
    ) -> None:
        if self._proxy_desired_state_writer is None or result.get("ok") is False:
            return
        self._proxy_desired_state_writer(running, allow_unverified if running else False)

    def proxy_status(self) -> dict[str, Any]:
        return self.services.proxy.proxy_status()

    def _proxy_health(self) -> dict[str, Any]:
        return self.services.proxy._proxy_health()

    def _wait_for_proxy_health(self, *, timeout: float) -> dict[str, Any]:
        return self.services.proxy._wait_for_proxy_health(timeout=timeout)

    def setup_guide(self) -> dict[str, Any]:
        return self.services.diagnostics.setup_guide()

    def full_status(self) -> dict[str, Any]:
        return self.services.diagnostics.full_status()

    def logs(self, *, component: str = "proxy", lines: int = 100) -> dict[str, Any]:
        return self.services.diagnostics.logs(component=component, lines=lines)

    def routing_report(self, *, days: int = 30, unit: str = "turn") -> dict[str, Any]:
        return self.services.routing.routing_report(days=days, unit=unit)

    def _adaptive_settings(self) -> dict[str, Any]:
        return self.services.routing._adaptive_settings()

    def _adaptive_store(self) -> HistoryStore:
        return self.services.routing._adaptive_store()

    def adaptive_status(self) -> dict[str, Any]:
        return self.services.routing.adaptive_status()

    def adaptive_import_tasks(self, *, confirm: str) -> dict[str, Any]:
        return self.services.routing.adaptive_import_tasks(confirm=confirm)

    def adaptive_evaluate(self, *, task: str, repo_path: str = "") -> dict[str, Any]:
        return self.services.routing.adaptive_evaluate(task=task, repo_path=repo_path)

    def enable_adaptive_shadow(self, *, confirm: str) -> dict[str, Any]:
        return self.services.routing.enable_adaptive_shadow(confirm=confirm)

    def enable_adaptive_canary(self, *, percent: int, confirm: str) -> dict[str, Any]:
        return self.services.routing.enable_adaptive_canary(percent=percent, confirm=confirm)

    def disable_adaptive(self, *, confirm: str) -> dict[str, Any]:
        return self.services.routing.disable_adaptive(confirm=confirm)

    def recent_calls(self, *, limit: int = 20) -> dict[str, Any]:
        return self.services.diagnostics.recent_calls(limit=limit)

    def recent_turns(self, *, limit: int = 20) -> dict[str, Any]:
        return self.services.diagnostics.recent_turns(limit=limit)

    def explain_turn(self, *, turn_id: str) -> dict[str, Any]:
        return self.services.diagnostics.explain_turn(turn_id=turn_id)

    def record_feedback(
        self,
        *,
        prompt_fingerprint: str | None,
        logical_turn_id: str | None,
        expected_tier: str,
        outcome: str,
        note: str = "",
    ) -> dict[str, Any]:
        return self.services.diagnostics.record_feedback(prompt_fingerprint=prompt_fingerprint, logical_turn_id=logical_turn_id, expected_tier=expected_tier, outcome=outcome, note=note)

    def list_backups(self) -> dict[str, Any]:
        return self.services.codex_mode.list_backups()

    def restore_codex_config(self, *, backup_path: str, confirm: str) -> dict[str, Any]:
        return self.services.codex_mode.restore_codex_config(backup_path=backup_path, confirm=confirm)

    def start_codex_task(
        self,
        *,
        repo_path: str,
        task: str,
        test_profile: str = "auto",
        max_escalations: int = 1,
        allow_dirty: bool = False,
        confirm: str,
    ) -> dict[str, Any]:
        return self.services.task.start_codex_task(repo_path=repo_path, task=task, test_profile=test_profile, max_escalations=max_escalations, allow_dirty=allow_dirty, confirm=confirm)

    def list_codex_tasks(self, *, limit: int = 20) -> dict[str, Any]:
        return self.services.task.list_codex_tasks(limit=limit)

    def stop_codex_task(self, *, task_id: str, confirm: str) -> dict[str, Any]:
        return self.services.task.stop_codex_task(task_id=task_id, confirm=confirm)

    def _task_run_dir(self, task_id: str) -> Path:
        return self.services.task._task_run_dir(task_id)

    def codex_task_status(self, *, task_id: str) -> dict[str, Any]:
        return self.services.task.codex_task_status(task_id=task_id)

    def active_phase(self) -> PhaseName:
        return self.services.runtime.active_phase()

    def active_profile(self) -> ProfileName:
        return self.services.runtime.active_profile()

    def active_policy(self) -> str:
        return self.services.runtime.active_policy()

    def _require_runtime(self) -> None:
        return self.services.runtime._require_runtime()

    def _runtime_env(self) -> dict[str, str]:
        return self.services.runtime._runtime_env()

    def _runtime_install_env(self) -> dict[str, str]:
        return self.services.runtime._runtime_install_env()
