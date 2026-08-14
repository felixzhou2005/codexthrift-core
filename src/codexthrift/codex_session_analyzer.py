from __future__ import annotations

from pathlib import Path
from typing import Any

from .history_store import HistoryStore
from .routing_policy import classify_text
from .task_features import extract_task_features
from .task_reward import derive_reward
from .util import read_json


def import_task_run(store: HistoryStore, run_dir: Path) -> dict[str, Any]:
    spec = read_json(run_dir / "spec.json", {})
    result = read_json(run_dir / "result.json", {})
    if not isinstance(spec, dict) or not isinstance(result, dict):
        return {"imported": False, "reason": "missing_spec_or_result"}
    task_id = str(result.get("task_id") or spec.get("task_id") or run_dir.name)
    if not result.get("finished_at"):
        return {"imported": False, "task_id": task_id, "reason": "task_still_running"}
    attempts = [item for item in result.get("attempts") or [] if isinstance(item, dict)]
    reward = derive_reward(result)
    baseline_tier, _, _, _ = classify_text(str(spec.get("task") or ""))
    features = extract_task_features(
        str(spec.get("task") or ""),
        repo_path=str(spec.get("repo_path") or ""),
        has_tests=result.get("verification_strength") == "tests",
        baseline_tier=baseline_tier,
    )
    first_model = str(attempts[0].get("model")) if attempts else None
    task = {
        "task_id": task_id,
        "repo_hash": features["repo_hash"],
        "task_fingerprint": features["task_fingerprint"],
        "baseline_tier": features["baseline_tier"],
        "selected_model": first_model,
        "reasoning_effort": "xhigh" if first_model == "sol-xhigh" else None,
        "selection_reason": "task-run",
        "started_at": result.get("started_at"),
        "finished_at": result.get("finished_at"),
        "features": features,
    }
    outcome = {
        "first_pass": bool(attempts and attempts[0].get("success")),
        "final_success": bool(result.get("success")),
        "escalation_used": int(result.get("escalations_used") or 0),
        "infrastructure_failure": reward["confidence"] == "excluded",
        **reward,
    }
    store.record_task(task, outcome, attempts)
    return {"imported": True, "task_id": task_id, "reward": reward}


def import_task_runs(store: HistoryStore, task_runs_dir: Path) -> dict[str, Any]:
    imported = []
    skipped = []
    for run_dir in sorted((path for path in task_runs_dir.iterdir() if path.is_dir()), key=lambda item: item.name):
        result = import_task_run(store, run_dir)
        (imported if result.get("imported") else skipped).append(result)
    return {"imported": imported, "skipped": skipped, "imported_count": len(imported)}
