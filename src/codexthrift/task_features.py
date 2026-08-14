from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any


_RISK_TERMS = (
    "security", "permission", "authentication", "authorization", "payment",
    "production outage", "data loss", "database migration", "delete", "rollback",
    "安全", "权限", "认证", "支付", "生产故障", "数据丢失", "数据库迁移", "删除",
)
_DEBUG_TERMS = ("debug", "root cause", "traceback", "error", "bug", "调试", "根本原因", "报错")
_MULTI_MODULE_TERMS = ("multiple modules", "cross-service", "refactor", "多模块", "跨服务", "重构")
_TECHNICAL_TERMS = (
    "python", "typescript", "swift", "react", "react native", "mysql", "postgresql",
    "redis", "docker", "websocket", "oauth", "api", "database", "测试", "接口",
)


def repo_fingerprint(repo_path: str) -> str:
    return hashlib.sha256(repo_path.encode("utf-8", errors="replace")).hexdigest()[:20]


def task_fingerprint(task_text: str) -> str:
    normalized = re.sub(r"\s+", " ", task_text.strip().lower())
    return hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()[:20]


def extract_task_features(
    task_text: str,
    *,
    repo_path: str | None = None,
    has_tests: bool = False,
    baseline_tier: str = "MEDIUM",
) -> dict[str, Any]:
    text = task_text.lower()
    tokens = set(re.findall(r"[a-z0-9_+-]+|[\u4e00-\u9fff]{2,}", text))
    def count(terms: tuple[str, ...]) -> int:
        return sum(1 for term in terms if term in text)

    risk_score = count(_RISK_TERMS)
    debug_score = count(_DEBUG_TERMS)
    multi_module_score = count(_MULTI_MODULE_TERMS)
    technical_score = count(_TECHNICAL_TERMS)
    return {
        "task_fingerprint": task_fingerprint(task_text),
        "repo_hash": repo_fingerprint(repo_path or ""),
        "baseline_tier": baseline_tier,
        "prompt_length": len(task_text),
        "token_count": len(tokens),
        "code_presence": bool(re.search(r"```|\b(class|def|function|SELECT|INSERT)\b", task_text, re.I)),
        "debug_score": debug_score,
        "multi_module_score": multi_module_score,
        "technical_score": technical_score,
        "risk_score": risk_score,
        "has_tests": bool(has_tests),
        "risk_level": "high" if risk_score else "low",
        "keywords": sorted(tokens & set(_TECHNICAL_TERMS)),
    }


def feature_similarity(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    score = 0.0
    weight = 0.0
    for key, current_weight in (
        ("baseline_tier", 2.0),
        ("risk_level", 3.0),
        ("has_tests", 2.0),
        ("code_presence", 1.0),
    ):
        weight += current_weight
        if left.get(key) == right.get(key):
            score += current_weight
    left_terms = set(left.get("keywords") or [])
    right_terms = set(right.get("keywords") or [])
    weight += 3.0
    if left_terms or right_terms:
        score += 3.0 * len(left_terms & right_terms) / max(1, len(left_terms | right_terms))
    for key in ("debug_score", "multi_module_score", "technical_score"):
        weight += 1.0
        if abs(int(left.get(key, 0)) - int(right.get(key, 0))) <= 1:
            score += 1.0
    return round(score / weight, 4) if weight else 0.0
