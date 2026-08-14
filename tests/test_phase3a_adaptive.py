from __future__ import annotations

import json
from pathlib import Path

import pytest

from codexthrift.adaptive_policy import decide_route
from codexthrift.codex_session_analyzer import import_task_run
from codexthrift.config_templates import build_config, profile_probe_models
from codexthrift.controller import RouterController
from codexthrift.history_store import HistoryStore
from codexthrift.paths import WorkspacePaths
from codexthrift.task_features import extract_task_features
from codexthrift.util import ControlError


def _controller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RouterController:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    return RouterController(
        WorkspacePaths(tmp_path / "router-home"),
        port=45454,
        ensure_paths=True,
    )


def _write_probe(controller: RouterController, phase: str) -> None:
    specs = profile_probe_models("gpt56", phase)
    controller.paths.probe_result_for(phase, "gpt56").parent.mkdir(parents=True, exist_ok=True)
    controller.paths.probe_result_for(phase, "gpt56").write_text(
        json.dumps({"phase": phase, "profile": "gpt56", "all_ok": True, "results": [{**item, "ok": True} for item in specs]}),
        encoding="utf-8",
    )


def test_phase3a_uses_only_reasoning_high_and_rescue_xhigh() -> None:
    config = build_config("phase3a", "gpt56", phase21_policy=True)
    models = {item["model_name"]: item for item in config["model_list"]}
    assert models["luna-low"]["litellm_params"]["reasoning_effort"] == "low"
    assert models["sol-medium"]["litellm_params"]["reasoning_effort"] == "medium"
    assert models["sol-high"]["litellm_params"]["reasoning_effort"] == "high"
    assert models["sol-xhigh"]["litellm_params"]["reasoning_effort"] == "xhigh"
    tiers = models["codex-auto"]["litellm_params"]["complexity_router_config"]["tiers"]
    assert tiers == {"SIMPLE": "luna-low", "MEDIUM": "terra-medium", "COMPLEX": "sol-medium", "REASONING": "sol-high"}
    probes = profile_probe_models("gpt56", "phase3a")
    assert len(probes) == 4
    assert {item["effort"] for item in probes} == {"low", "medium", "high", "xhigh"}


def test_phase3a_activation_requires_its_own_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    controller = _controller(tmp_path, monkeypatch)
    controller.prepare_configs(profile="gpt56", confirm="APPLY")
    controller.prepare_phase3a_candidate(profile="gpt56", confirm="APPLY")
    with pytest.raises(ControlError, match="phase3a/gpt56"):
        controller.activate_phase3a(profile="gpt56", confirm="APPLY")
    _write_probe(controller, "phase3a")
    _write_probe(controller, "phase2")
    monkeypatch.setattr(controller, "proxy_status", lambda: {"running": False})
    result = controller.activate_phase3a(profile="gpt56", confirm="APPLY")
    assert result["phase"] == "phase3a"
    assert controller.active_phase() == "phase3a"
    assert controller.active_policy() == "phase3a"


def _completed_run(run_dir: Path, *, success: bool = True, tests: bool = True) -> None:
    run_dir.mkdir(parents=True)
    (run_dir / "spec.json").write_text(
        json.dumps({"task_id": run_dir.name, "repo_path": "/tmp/repo", "task": "Update a React API field", "test_profile": "auto"}),
        encoding="utf-8",
    )
    (run_dir / "result.json").write_text(
        json.dumps({
            "task_id": run_dir.name,
            "started_at": "2026-08-05T00:00:00+00:00",
            "finished_at": "2026-08-05T00:01:00+00:00",
            "success": success,
            "verification_strength": "tests" if tests else "diff-check-only",
            "escalations_used": 0,
            "attempts": [{"model": "codex-task-auto", "success": success, "codex": {"returncode": 0}, "verification": []}],
        }),
        encoding="utf-8",
    )


def test_task_run_import_creates_one_verified_logical_task(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "history.sqlite3")
    run_dir = tmp_path / "task-runs" / "task-1"
    _completed_run(run_dir)
    result = import_task_run(store, run_dir)
    assert result["imported"] is True
    assert store.summary()["tasks"] == 1
    assert store.summary()["strong_feedback_tasks"] == 1
    import_task_run(store, run_dir)
    assert store.summary()["tasks"] == 1


def test_policy_never_downgrades_high_risk_work(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "history.sqlite3")
    features = extract_task_features(
        "Fix security authorization issue with tests", has_tests=True, baseline_tier="COMPLEX"
    )
    decision = decide_route(store, features, canary_enabled=True, canary_percent=10, allow_exploration=True)
    assert decision["action"] == "KEEP"
    assert decision["selected_tier"] == "COMPLEX"
