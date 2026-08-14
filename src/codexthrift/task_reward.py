from __future__ import annotations

from typing import Any


def is_infrastructure_failure(attempt: dict[str, Any]) -> bool:
    return bool(attempt.get("infrastructure_failure"))


def derive_reward(result: dict[str, Any]) -> dict[str, Any]:
    attempts = [item for item in result.get("attempts") or [] if isinstance(item, dict)]
    if any(is_infrastructure_failure(item) for item in attempts):
        return {"reward": None, "confidence": "excluded", "reason": "infrastructure_failure"}
    success = bool(result.get("success"))
    strength = str(result.get("verification_strength") or "diff-check-only")
    first_pass = bool(attempts and attempts[0].get("success"))
    escalation_used = max(0, len(attempts) - 1)
    if success and first_pass:
        return {
            "reward": 1.0 if strength == "tests" else 0.2,
            "confidence": "strong" if strength == "tests" else "weak",
            "reason": "first_pass",
        }
    if success:
        return {
            "reward": 0.55 if strength == "tests" else 0.1,
            "confidence": "strong" if strength == "tests" else "weak",
            "reason": "rescue_success",
            "escalation_used": escalation_used,
        }
    return {
        "reward": -1.0 if strength == "tests" else -0.2,
        "confidence": "strong" if strength == "tests" else "weak",
        "reason": "final_failure",
        "escalation_used": escalation_used,
    }
