from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .paths import WorkspacePaths
from .util import utc_now_iso


_ALIAS_TO_TIER = {
    "luna-medium": "SIMPLE",
    "luna-low": "SIMPLE",
    "terra-medium": "MEDIUM",
    "sol-medium": "COMPLEX",
    "reasoning-medium": "REASONING",
    "sol-high": "COMPLEX",
    "sol-xhigh": "REASONING",
    "gpt-5.6-luna": "SIMPLE",
    "codex-auto-review": "COMPLEX",
}
_SAFE_CALL_FIELDS = (
    "audit_schema_version",
    "timestamp",
    "status",
    "request_kind",
    "logical_turn_id",
    "routing_mode",
    "routing_cause",
    "routing_score",
    "routing_signals",
    "requested_alias",
    "selected_tier",
    "selected_alias",
    "selected_model",
    "provider_model",
    "response_model",
    "observed_service_tier",
    "billing_mode",
    "billing_mode_source",
    "builtin_tool_usage",
    "reasoning_effort",
    "prompt_fingerprint",
    "prompt_chars_total",
    "user_prompt_chars",
    "duration_ms",
    "usage",
    "response_cost",
    "default_reason",
    "error_type",
)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    rows.append(value)
            except json.JSONDecodeError:
                continue
    return rows


def _safe_call(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record.get(key) for key in _SAFE_CALL_FIELDS if key in record}


def _tier_for(record: dict[str, Any]) -> str:
    explicit = str(record.get("selected_tier") or "").upper()
    if explicit in {"SIMPLE", "MEDIUM", "COMPLEX", "REASONING"}:
        return explicit
    for key in ("selected_alias", "selected_model", "provider_model", "response_model"):
        value = str(record.get(key) or "")
        for alias, tier in _ALIAS_TO_TIER.items():
            if alias in value:
                return tier
        lowered = value.lower()
        if "luna" in lowered:
            return "SIMPLE"
        if "terra" in lowered:
            return "MEDIUM"
        if "sol" in lowered:
            effort = str(record.get("reasoning_effort") or "").lower()
            return "REASONING" if effort == "xhigh" else "COMPLEX"
    return "UNKNOWN"


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _turn_groups(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        turn_id = row.get("logical_turn_id")
        if isinstance(turn_id, str) and turn_id:
            grouped[turn_id].append(row)
    return grouped


def _turn_summary(turn_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: str(row.get("timestamp") or ""))
    success_rows = [row for row in ordered if row.get("status") == "success"]
    selected = success_rows[-1] if success_rows else ordered[-1]
    durations = [
        float(row["duration_ms"])
        for row in ordered
        if isinstance(row.get("duration_ms"), (int, float)) and row.get("duration_ms", 0) >= 0
    ]
    return {
        "logical_turn_id": turn_id,
        "timestamp": ordered[0].get("timestamp"),
        "status": "success" if success_rows else str(selected.get("status") or "unknown"),
        "routing_mode": selected.get("routing_mode"),
        "routing_cause": selected.get("routing_cause"),
        "routing_score": selected.get("routing_score"),
        "routing_signals": selected.get("routing_signals") or [],
        "selected_tier": _tier_for(selected),
        "selected_alias": selected.get("selected_alias") or selected.get("selected_model"),
        "provider_model": selected.get("provider_model") or selected.get("response_model"),
        "reasoning_effort": selected.get("reasoning_effort"),
        "internal_calls": len(ordered),
        "successful_calls": len(success_rows),
        "failed_calls": sum(1 for row in ordered if row.get("status") == "failure"),
        "continuation_calls": sum(1 for row in ordered if row.get("request_kind") == "tool_continuation"),
        "duration_ms_total": round(sum(durations), 1),
        "user_prompt_chars": selected.get("user_prompt_chars"),
        "prompt_fingerprint": selected.get("prompt_fingerprint"),
        "default_reasons": sorted(
            {str(row.get("default_reason")) for row in ordered if row.get("default_reason")}
        ),
    }


def recent_calls(paths: WorkspacePaths, *, limit: int = 20) -> dict[str, Any]:
    count = max(1, min(int(limit), 200))
    rows = _read_jsonl(paths.audit_log)[-count:]
    return {"unit": "call", "records": [_safe_call(row) for row in reversed(rows)]}


def recent_turns(paths: WorkspacePaths, *, limit: int = 20) -> dict[str, Any]:
    count = max(1, min(int(limit), 200))
    groups = _turn_groups(_read_jsonl(paths.audit_log))
    summaries = [_turn_summary(turn_id, rows) for turn_id, rows in groups.items()]
    summaries.sort(key=lambda row: str(row.get("timestamp") or ""), reverse=True)
    legacy = sum(1 for row in _read_jsonl(paths.audit_log) if not row.get("logical_turn_id"))
    return {
        "unit": "turn",
        "records": summaries[:count],
        "legacy_call_records_excluded": legacy,
    }


def explain_turn(paths: WorkspacePaths, *, turn_id: str) -> dict[str, Any]:
    normalized = turn_id.strip()
    rows = [row for row in _read_jsonl(paths.audit_log) if row.get("logical_turn_id") == normalized]
    if not rows:
        raise ValueError(f"未找到 logical_turn_id：{normalized}")
    return {
        "turn": _turn_summary(normalized, rows),
        "calls": [_safe_call(row) for row in rows],
        "privacy": "未返回提示词正文、认证信息或请求头。",
    }


def record_feedback(
    paths: WorkspacePaths,
    *,
    prompt_fingerprint: str | None,
    logical_turn_id: str | None,
    expected_tier: str,
    outcome: str,
    note: str = "",
) -> dict[str, Any]:
    expected = expected_tier.upper()
    if expected not in {"SIMPLE", "MEDIUM", "COMPLEX", "REASONING"}:
        raise ValueError("expected_tier 必须是 SIMPLE、MEDIUM、COMPLEX 或 REASONING")
    normalized_outcome = outcome.lower()
    if normalized_outcome not in {"success", "partial", "failure"}:
        raise ValueError("outcome 必须是 success、partial 或 failure")
    fingerprint = (prompt_fingerprint or "").strip() or None
    turn_id = (logical_turn_id or "").strip() or None
    if not fingerprint and not turn_id:
        raise ValueError("必须提供 prompt_fingerprint 或 logical_turn_id")
    record = {
        "timestamp": utc_now_iso(),
        "prompt_fingerprint": fingerprint,
        "logical_turn_id": turn_id,
        "expected_tier": expected,
        "outcome": normalized_outcome,
        "note": note[:1000],
    }
    paths.feedback_log.parent.mkdir(parents=True, exist_ok=True)
    with paths.feedback_log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    if os.name != "nt":
        paths.feedback_log.chmod(0o600)
    return {"ok": True, "feedback": record}


def _distribution_warning(counts: Counter[str], sample_count: int, *, label: str) -> str | None:
    if sample_count < 10 or not counts:
        return None
    tier, count = counts.most_common(1)[0]
    if count / sample_count > 0.90:
        return f"{label}中 {tier} 占比超过 90%；这是病态集中分布，请检查分类规则和任务继承。"
    return None


def routing_report(
    paths: WorkspacePaths,
    *,
    days: int = 30,
    limit: int = 5000,
    unit: str = "turn",
) -> dict[str, Any]:
    if unit not in {"turn", "call"}:
        raise ValueError("unit 必须是 turn 或 call")
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, min(days, 365)))
    audit: list[dict[str, Any]] = []
    for row in _read_jsonl(paths.audit_log)[-limit:]:
        timestamp = _parse_timestamp(row.get("timestamp"))
        if timestamp is None or timestamp >= cutoff:
            audit.append(row)

    feedback = _read_jsonl(paths.feedback_log)
    feedback_by_fp: dict[str, list[dict[str, Any]]] = defaultdict(list)
    feedback_by_turn: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in feedback:
        if item.get("prompt_fingerprint"):
            feedback_by_fp[str(item["prompt_fingerprint"])].append(item)
        if item.get("logical_turn_id"):
            feedback_by_turn[str(item["logical_turn_id"])].append(item)

    status_counts = Counter(str(row.get("status") or "unknown") for row in audit)
    call_tier_counts = Counter(_tier_for(row) for row in audit if row.get("status") == "success")
    turn_groups = _turn_groups(audit)
    turns = [_turn_summary(turn_id, rows) for turn_id, rows in turn_groups.items()]
    turn_tier_counts = Counter(row["selected_tier"] for row in turns if row.get("status") == "success")
    legacy_rows = [row for row in audit if not row.get("logical_turn_id")]

    analyzed_rows: list[dict[str, Any]] = turns if unit == "turn" else audit
    tier_counts = turn_tier_counts if unit == "turn" else call_tier_counts
    successful = sum(1 for row in analyzed_rows if row.get("status") == "success")
    failures = sum(1 for row in analyzed_rows if row.get("status") == "failure")
    total = successful + failures
    effort_counts = Counter(
        str(row.get("reasoning_effort") or "unknown")
        for row in analyzed_rows
        if row.get("status") == "success"
    )
    model_counts = Counter(
        str(row.get("selected_alias") or row.get("selected_model") or row.get("provider_model") or "unknown")
        for row in analyzed_rows
        if row.get("status") == "success"
    )

    durations = [
        float(row.get("duration_ms_total") if unit == "turn" else row.get("duration_ms"))
        for row in analyzed_rows
        if isinstance(row.get("duration_ms_total") if unit == "turn" else row.get("duration_ms"), (int, float))
    ]
    failure_types = Counter(
        str(row.get("error_type") or "unknown")
        for row in audit
        if row.get("status") == "failure"
    )

    labelled = over_routed = under_routed = failed_after_route = 0
    tier_rank = {"SIMPLE": 0, "MEDIUM": 1, "COMPLEX": 2, "REASONING": 3, "UNKNOWN": -1}
    label_subjects = turns if unit == "turn" else audit
    for row in label_subjects:
        labels = feedback_by_turn.get(str(row.get("logical_turn_id") or ""))
        if not labels:
            labels = feedback_by_fp.get(str(row.get("prompt_fingerprint") or ""))
        if not labels:
            continue
        labelled += 1
        actual = str(row.get("selected_tier") or _tier_for(row))
        expected = str(labels[-1].get("expected_tier") or "UNKNOWN")
        if tier_rank.get(actual, -1) > tier_rank.get(expected, -1):
            over_routed += 1
        elif tier_rank.get(actual, -1) < tier_rank.get(expected, -1):
            under_routed += 1
        if labels[-1].get("outcome") == "failure":
            failed_after_route += 1

    recommendations: list[str] = []
    warning = _distribution_warning(tier_counts, successful, label="用户任务" if unit == "turn" else "调用")
    if warning:
        recommendations.append(warning)
    legacy_success = [row for row in legacy_rows if row.get("status") == "success"]
    legacy_counts = Counter(_tier_for(row) for row in legacy_success)
    legacy_warning = _distribution_warning(legacy_counts, len(legacy_success), label="旧版调用记录")
    if legacy_warning:
        recommendations.append(legacy_warning)
    safe_default_calls = sum(1 for row in audit if row.get("default_reason") == "no_user_no_lineage")
    if safe_default_calls:
        recommendations.append(f"发现 {safe_default_calls} 次无法识别用户任务或调用链的安全回落；请检查 Codex turn_id 透传。")
    if total < 20:
        recommendations.append("新版用户任务样本仍少；保持 Phase 2 推理强度并继续收集真实任务。")
    if labelled >= 10:
        if over_routed / labelled > 0.20:
            recommendations.append("标注样本中过度升级超过 20%；建议收紧 broad_scope 或 complex_phrase 规则。")
        if under_routed / labelled > 0.12:
            recommendations.append("标注样本中低估复杂度超过 12%；建议补充复杂任务组合规则。")
    if status_counts.get("failure", 0) and status_counts["failure"] / max(1, len(audit)) > 0.08:
        recommendations.append("模型调用失败率超过 8%；先检查 OAuth、订阅限额和模型别名。")
    if not recommendations:
        recommendations.append("新版任务级路由分布暂未发现明显异常。")

    return {
        "window_days": days,
        "unit": unit,
        "audit_call_records": len(audit),
        "logical_turns": len(turns),
        "legacy_call_records_excluded_from_turns": len(legacy_rows),
        "successful": successful,
        "failed": failures,
        "success_rate": round(successful / total, 4) if total else None,
        "tier_counts": dict(tier_counts),
        "call_tier_counts": dict(call_tier_counts),
        "turn_tier_counts": dict(turn_tier_counts),
        "effort_counts": dict(effort_counts),
        "top_models": dict(model_counts.most_common(10)),
        "failure_types": dict(failure_types),
        "latency_ms": {
            "average": round(sum(durations) / len(durations), 1) if durations else None,
            "max": max(durations) if durations else None,
        },
        "continuation": {
            "tool_continuation_calls": sum(1 for row in audit if row.get("request_kind") == "tool_continuation"),
            "safe_default_calls": safe_default_calls,
        },
        "feedback": {
            "records": len(feedback),
            "matched": labelled,
            "over_routed": over_routed,
            "under_routed": under_routed,
            "failed_after_route": failed_after_route,
        },
        "recommendations": recommendations,
        "privacy": "审计不保存提示词正文、认证信息或请求头，只保存长度、短哈希和路由信号代码。",
    }
