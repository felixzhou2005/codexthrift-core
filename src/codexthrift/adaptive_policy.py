from __future__ import annotations

from typing import Any, Literal

from .history_store import HistoryStore
from .similar_task_retriever import retrieve_similar_tasks, similar_task_statistics

Action = Literal["KEEP", "DOWN_ONE", "UP_ONE"]

_DOWNGRADE = {"REASONING": "COMPLEX", "COMPLEX": "MEDIUM", "MEDIUM": "SIMPLE"}
_UPGRADE = {"SIMPLE": "MEDIUM", "MEDIUM": "COMPLEX", "COMPLEX": "REASONING"}
_PHASE3A_MODELS = {
    "SIMPLE": ("luna-low", "low"),
    "MEDIUM": ("terra-medium", "medium"),
    "COMPLEX": ("sol-medium", "medium"),
    "REASONING": ("sol-high", "high"),
}


def decide_route(
    store: HistoryStore,
    features: dict[str, Any],
    *,
    canary_enabled: bool,
    canary_percent: int,
    allow_exploration: bool,
) -> dict[str, Any]:
    baseline = str(features.get("baseline_tier") or "MEDIUM")
    rows = retrieve_similar_tasks(store, features)
    statistics = similar_task_statistics(rows)
    action: Action = "KEEP"
    reason = "baseline_no_history"
    low_risk = features.get("risk_level") == "low" and bool(features.get("has_tests"))
    first_pass_rate = statistics.get("first_pass_rate")

    if rows and first_pass_rate is not None and first_pass_rate < 0.55 and baseline in _UPGRADE:
        action = "UP_ONE"
        reason = "similar_verified_tasks_underperform"
    elif (
        rows
        and statistics["count"] >= 5
        and first_pass_rate is not None
        and first_pass_rate >= 0.85
        and low_risk
        and canary_enabled
        and allow_exploration
        and canary_percent > 0
        and baseline in _DOWNGRADE
    ):
        action = "DOWN_ONE"
        reason = "eligible_similarity_canary"

    selected_tier = baseline
    if action == "DOWN_ONE":
        selected_tier = _DOWNGRADE[baseline]
    elif action == "UP_ONE":
        selected_tier = _UPGRADE[baseline]
    alias, effort = _PHASE3A_MODELS[selected_tier]
    return {
        "baseline_tier": baseline,
        "selected_tier": selected_tier,
        "selected_alias": alias,
        "reasoning_effort": effort,
        "action": action,
        "selection_reason": reason,
        "exploration_flag": action == "DOWN_ONE",
        "low_risk_eligible": low_risk,
        "similarity": statistics,
        "similar_tasks": [
            {"task_id": row["task_id"], "similarity": row["similarity"], "final_success": bool(row["final_success"])}
            for row in rows
        ],
    }
