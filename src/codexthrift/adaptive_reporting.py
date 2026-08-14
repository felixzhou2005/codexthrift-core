from __future__ import annotations

from typing import Any

from .history_store import HistoryStore


def adaptive_report(store: HistoryStore, settings: dict[str, Any]) -> dict[str, Any]:
    summary = store.summary()
    tasks = max(1, summary["tasks"])
    return {
        "mode": settings.get("mode", "disabled"),
        "canary_percent": int(settings.get("canary_percent", 0)),
        "summary": summary,
        "metrics": {
            "final_success_rate": round(summary["successes"] / tasks, 4) if summary["tasks"] else None,
            "first_pass_rate": round(summary["first_passes"] / tasks, 4) if summary["tasks"] else None,
            "escalation_rate": round(summary["escalations"] / tasks, 4) if summary["tasks"] else None,
        },
        "gates": {
            "shadow_ready": summary["tasks"] >= 30 and summary["strong_feedback_tasks"] >= 20,
            "canary_ready": summary["tasks"] >= 30 and summary["strong_feedback_tasks"] >= 20,
            "high_risk_downgrades": 0,
        },
        "notice": "This report uses logical task outcomes, not raw LiteLLM API call counts.",
    }
