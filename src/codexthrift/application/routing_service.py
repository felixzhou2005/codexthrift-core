from __future__ import annotations

from typing import Any

from ..adaptive_policy import decide_route
from ..adaptive_reporting import adaptive_report
from ..codex_session_analyzer import import_task_runs
from ..errors import ControlError
from ..history_store import HistoryStore
from ..reporting import (
    routing_report,
)
from ..routing_policy import classify_text
from ..security.path_guard import require_confirmation
from ..storage.atomic_io import atomic_write_json, read_json, utc_now_iso
from ..task_features import extract_task_features
from .base import BaseApplicationService


class RoutingService(BaseApplicationService):
    def routing_report(self, *, days: int = 30, unit: str = "turn") -> dict[str, Any]:
        return routing_report(self.paths, days=days, unit=unit)

    def _adaptive_settings(self) -> dict[str, Any]:
        value = read_json(self.paths.adaptive_settings, {})
        if not isinstance(value, dict):
            value = {}
        return {
            "mode": value.get("mode", "disabled"),
            "canary_percent": int(value.get("canary_percent", 0)),
        }

    def _adaptive_store(self) -> HistoryStore:
        return HistoryStore(self.paths.adaptive_db)

    def adaptive_status(self) -> dict[str, Any]:
        return adaptive_report(self._adaptive_store(), self._adaptive_settings())

    def adaptive_import_tasks(self, *, confirm: str) -> dict[str, Any]:
        require_confirmation(confirm)
        return import_task_runs(self._adaptive_store(), self.paths.task_runs_dir)

    def adaptive_evaluate(self, *, task: str, repo_path: str = "") -> dict[str, Any]:
        if not task.strip():
            raise ControlError("task 不能为空。")
        tier, _, _, _ = classify_text(task)
        settings = self._adaptive_settings()
        features = extract_task_features(
            task,
            repo_path=repo_path,
            has_tests=False,
            baseline_tier=tier,
        )
        decision = decide_route(
            self._adaptive_store(),
            features,
            canary_enabled=settings["mode"] == "canary",
            canary_percent=settings["canary_percent"],
            allow_exploration=settings["mode"] == "canary",
        )
        decision["mode"] = settings["mode"]
        decision["applied"] = False
        return decision

    def enable_adaptive_shadow(self, *, confirm: str) -> dict[str, Any]:
        require_confirmation(confirm)
        settings = {"mode": "shadow", "canary_percent": 0, "updated_at": utc_now_iso()}
        atomic_write_json(self.paths.adaptive_settings, settings, mode=0o600)
        return {"ok": True, **settings, "applied_to_live_routing": False}

    def enable_adaptive_canary(self, *, percent: int, confirm: str) -> dict[str, Any]:
        require_confirmation(confirm)
        if self.active_phase() != "phase3a":
            raise ControlError("Canary 只能在静态 phase3a 已激活后开启。")
        if percent not in {10, 25, 50, 100}:
            raise ControlError("Canary 仅允许 10、25、50 或 100。")
        report = adaptive_report(self._adaptive_store(), self._adaptive_settings())
        if not report["gates"]["canary_ready"]:
            raise ControlError("自动验证样本不足；至少需要 30 个任务和 20 个强反馈任务。")
        settings = {"mode": "canary", "canary_percent": percent, "updated_at": utc_now_iso()}
        atomic_write_json(self.paths.adaptive_settings, settings, mode=0o600)
        return {"ok": True, **settings, "applied_to_live_routing": False,
                "notice": "Canary policy is prepared but remains Shadow-only until the LiteLLM runtime plugin is enabled."}

    def disable_adaptive(self, *, confirm: str) -> dict[str, Any]:
        require_confirmation(confirm)
        settings = {"mode": "disabled", "canary_percent": 0, "updated_at": utc_now_iso()}
        atomic_write_json(self.paths.adaptive_settings, settings, mode=0o600)
        return {"ok": True, **settings, "applied_to_live_routing": False}
