from __future__ import annotations

from typing import Any

from .history_store import HistoryStore
from .task_features import feature_similarity


def retrieve_similar_tasks(store: HistoryStore, features: dict[str, Any], *, limit: int = 8) -> list[dict[str, Any]]:
    candidates = store.verified_tasks(limit=500)
    ranked = []
    for candidate in candidates:
        score = feature_similarity(features, candidate["features"])
        if score >= 0.45:
            ranked.append({**candidate, "similarity": score})
    return sorted(ranked, key=lambda item: (item["similarity"], item.get("finished_at") or ""), reverse=True)[:limit]


def similar_task_statistics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"count": 0, "first_pass_rate": None, "success_rate": None, "models": {}}
    weighted_total = sum(float(row["similarity"]) for row in rows)
    first_pass = sum(float(row["similarity"]) * int(row["first_pass"]) for row in rows)
    success = sum(float(row["similarity"]) * int(row["final_success"]) for row in rows)
    models: dict[str, dict[str, float]] = {}
    for row in rows:
        model = str(row.get("selected_model") or "unknown")
        stats = models.setdefault(model, {"weight": 0.0, "success": 0.0, "first_pass": 0.0})
        stats["weight"] += float(row["similarity"])
        stats["success"] += float(row["similarity"]) * int(row["final_success"])
        stats["first_pass"] += float(row["similarity"]) * int(row["first_pass"])
    return {
        "count": len(rows),
        "first_pass_rate": round(first_pass / weighted_total, 4),
        "success_rate": round(success / weighted_total, 4),
        "models": models,
    }
