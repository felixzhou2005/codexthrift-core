from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any


PHASE2_ALIASES = {
    "SIMPLE": "luna-medium",
    "MEDIUM": "terra-medium",
    "COMPLEX": "sol-medium",
    "REASONING": "reasoning-medium",
}

_EXPLICIT_XHIGH_PHRASE = "这是复杂推理"

_CONTROL_PREFIX = re.compile(
    r"^\s*\[(auto|luna|terra|sol|自动|轻量|标准|最强)\]\s*",
    re.IGNORECASE,
)
_CONTROL_TIERS = {
    "luna": "SIMPLE",
    "轻量": "SIMPLE",
    "terra": "MEDIUM",
    "标准": "MEDIUM",
    "sol": "REASONING",
    "最强": "REASONING",
}

_HIGH_RISK_PATTERNS = (
    r"\bdeadlock\b",
    r"\brace condition\b",
    r"\bsecurity audit\b",
    r"\bdata loss\b",
    r"\bproduction (?:outage|incident|failure)\b",
    r"\bprivilege escalation\b",
    r"\bdistributed consensus\b",
    r"\birreversible\b",
    r"死锁",
    r"竞态条件",
    r"安全审计",
    r"数据丢失",
    r"生产(?:故障|事故|宕机)",
    r"权限漏洞",
    r"分布式一致性",
    r"不可逆",
)

_COMPLEX_PHRASE_PATTERNS = (
    r"\broot cause\b",
    r"\bcomplete (?:implementation|plan|analysis|audit)\b",
    r"\bcomprehensive (?:analysis|review|audit|plan)\b",
    r"\barchitecture (?:design|refactor|migration)\b",
    r"\bcross[- ]service\b",
    r"\bmultiple modules\b",
    r"\bfull[- ]stack\b",
    r"\bimplement (?:everything|all)\b",
    r"全面分析",
    r"完整(?:的)?(?:方案|分析|实施|实现|审计)",
    r"全量(?:检查|审计|迁移|实现|分析)",
    r"逐页检查",
    r"查找根本原因",
    r"跨模块",
    r"跨服务",
    r"架构(?:设计|重构|迁移)",
    r"批量迁移",
    r"设计并实现",
    r"检查所有未实现",
    r"按.+方案.+执行",
    r"逐项(?:完成|执行|实现)",
    r"全部(?:找到|删除|移除)",
    r"\d{1,2}\.\d{2}\s*(?:到|至|-)\s*\d{1,2}\.\d{2}",
    r"先.{0,80}(?:figma|设计).{0,120}(?:再.{0,80}(?:代码|修改)|代码)",
)

_DEEP_REASONING_PATTERNS = (
    r"(?:litellm|路由|模型).{0,120}(?:日志|消耗|余额|选择).{0,120}(?:分析|优化|方案)",
    r"(?:ui界面设计|功能设计).{0,180}(?:ui界面设计|功能设计)",
)

_SCOPE_WORDS = (
    "all",
    "every",
    "entire",
    "complete",
    "comprehensive",
    "全部",
    "所有",
    "整个",
    "全面",
    "完整",
    "全量",
)
_SCOPE_OBJECTS = (
    "page",
    "pages",
    "module",
    "modules",
    "service",
    "services",
    "menu",
    "menus",
    "file",
    "files",
    "feature",
    "features",
    "repository",
    "codebase",
    "页面",
    "模块",
    "服务",
    "菜单",
    "文件",
    "功能",
    "项目",
    "代码库",
)
_MULTISTEP_PATTERNS = (
    r"\bfirst\b.+\bthen\b",
    r"\bstep by step\b",
    r"\bphase\s*[0-9一二三四五六七八九十]+",
    r"先.+再",
    r"第一.+第二",
    r"一步一步",
    r"按步骤",
    r"分阶段",
)
_SIMPLE_PATTERNS = (
    r"\bfix (?:a |the )?typo\b",
    r"\brename (?:a |the )?variable\b",
    r"\bformat (?:this |the )?(?:file|code)\b",
    r"\bupdate (?:the )?copy\b",
    r"\bchange (?:the )?label\b",
    r"\badd (?:a |one )?comment\b",
    r"\bwhat is\b",
    r"\bdefine\b",
    r"修改(?:一个|这一个)?(?:拼写|文案|标签)",
    r"修改.{0,8}(?:拼写|文案|标签)",
    r"修复.{0,8}拼写",
    r"改(?:一下|一个)?(?:文案|标签|变量名)",
    r"格式化(?:这个|单个)?(?:文件|代码)",
    r"增加(?:一个)?注释",
    r"只改(?:一个|这个)",
    r"^(?:你是|是不是|是否).{0,100}(?:更新|修改|部署|完成|生效|看到|存在).*[？?]?$",
    r"^(?:请|你)?(?:给我)?(?:列出|提供).{0,100}(?:任务|问题|内容|标注).*$",
    r"^你要给我.{0,120}(?:任务|问题|内容|标注).*$",
    r"(?:第二|第三|phase\s*[23]).{0,36}(?:执行|完成|到哪里|状态|好处|条件成熟)",
    r"(?:去掉|删除).{0,48}(?:最上面|顶部).{0,100}(?:各个|所有|全部).{0,32}(?:菜单|页面)",
)

_MEDIUM_REFERENCE_UI_PATTERNS = (
    r"(?:去掉|删除).{0,160}(?:空出来|工具).{0,120}(?:不.*占用空间)",
    r"自动化.{0,180}(?:定时任务|列表).{0,260}(?:详情|运行历史)",
)

_SHORT_FOLLOW_UP_PATTERNS = (
    r"^继续[。！!？?\s]*$",
    r"^(?:好|好的)[，,。\s]*(?:按|就按).{0,24}方案(?:执行|继续)(?:[，,。\s].*)?$",
    r"^(?:按|就按).{0,24}方案(?:执行|继续)(?:[，,。\s].*)?$",
)
_TECHNICAL_TERMS = (
    "api",
    "database",
    "schema",
    "migration",
    "oauth",
    "authentication",
    "authorization",
    "concurrency",
    "transaction",
    "rollback",
    "docker",
    "kubernetes",
    "websocket",
    "react",
    "swift",
    "typescript",
    "python",
    "数据库",
    "架构",
    "迁移",
    "并发",
    "事务",
    "回滚",
    "权限",
)


@dataclass(frozen=True)
class RoutingDecision:
    tier: str
    selected_alias: str
    routing_mode: str
    routing_cause: str
    score: float
    signals: tuple[str, ...]
    override: str | None
    logical_turn_id: str | None
    session_hash: str | None
    prompt_fingerprint: str | None
    user_prompt_chars: int

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["signals"] = list(self.signals)
        return value


def _short_hash(namespace: str, value: str) -> str:
    return hashlib.sha256(f"{namespace}:{value}".encode("utf-8", errors="replace")).hexdigest()[:20]


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    for method_name in ("model_dump", "dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                return _plain(method())
            except Exception:
                pass
    return value


def _request_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    source = data.get("input")
    if isinstance(source, str):
        return [{"role": "user", "content": source}]
    if not isinstance(source, list):
        source = data.get("messages")
    if not isinstance(source, list):
        return []
    return [item for value in source if isinstance((item := _plain(value)), dict)]


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for raw in content:
        item = _plain(raw)
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "")
        text = item.get("text")
        if isinstance(text, str) and item_type in {"", "text", "input_text", "output_text"}:
            parts.append(text)
    return "\n".join(parts)


def _user_texts(data: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for item in _request_items(data):
        if str(item.get("role") or "").lower() != "user":
            continue
        text = _content_text(item.get("content"))
        if text.strip():
            values.append(text)
    return values


def extract_last_user_text(data: dict[str, Any]) -> str | None:
    values = _user_texts(data)
    if values:
        return values[-1]
    source = data.get("input")
    return source if isinstance(source, str) and source.strip() else None


def extract_previous_user_text(data: dict[str, Any]) -> str | None:
    values = _user_texts(data)
    return values[-2] if len(values) >= 2 else None


def _metadata_candidates(data: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for key in ("metadata", "litellm_metadata"):
        value = _plain(data.get(key))
        if isinstance(value, dict):
            candidates.append(value)
    for item in reversed(_request_items(data)):
        value = _plain(item.get("internal_chat_message_metadata_passthrough"))
        if isinstance(value, dict):
            candidates.append(value)
        value = _plain(item.get("metadata"))
        if isinstance(value, dict):
            candidates.append(value)
    return candidates


def extract_turn_id(data: dict[str, Any]) -> str | None:
    for metadata in _metadata_candidates(data):
        for key in ("turn_id", "logical_turn_id"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def extract_session_id(data: dict[str, Any]) -> str | None:
    for metadata in _metadata_candidates(data):
        for key in ("session_id", "thread_id", "conversation_id"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def parse_control_prefix(text: str) -> tuple[str | None, str]:
    match = _CONTROL_PREFIX.match(text)
    if match is None:
        return None, text
    control = match.group(1).lower()
    return control, text[match.end() :]


def strip_control_prefix_from_request(data: dict[str, Any]) -> str | None:
    source = data.get("input")
    if isinstance(source, str):
        control, stripped = parse_control_prefix(source)
        if control is not None:
            data["input"] = stripped
        return control
    for container_key in ("input", "messages"):
        items = data.get(container_key)
        if not isinstance(items, list):
            continue
        for item in reversed(items):
            if not isinstance(item, dict) or str(item.get("role") or "").lower() != "user":
                continue
            content = item.get("content")
            if isinstance(content, str):
                control, stripped = parse_control_prefix(content)
                if control is not None:
                    item["content"] = stripped
                return control
            if not isinstance(content, list):
                return None
            for part in content:
                if not isinstance(part, dict):
                    continue
                text = part.get("text")
                if not isinstance(text, str) or not text.strip():
                    continue
                control, stripped = parse_control_prefix(text)
                if control is not None:
                    part["text"] = stripped
                return control
            return None
    return None


def _matches_any(patterns: tuple[str, ...], text: str) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL) for pattern in patterns)


def _contains_term(text: str, term: str) -> bool:
    if term.isascii() and re.fullmatch(r"[a-zA-Z0-9_-]+", term):
        return re.search(rf"\b{re.escape(term)}\b", text, flags=re.IGNORECASE) is not None
    return term.lower() in text.lower()


def classify_text(text: str, *, control: str | None = None) -> tuple[str, str, float, tuple[str, ...]]:
    normalized = text.strip()
    lowered = normalized.lower()
    if control and control not in {"auto", "自动"}:
        tier = _CONTROL_TIERS[control]
        return tier, "manual_override", 1.0, (f"manual:{control}",)

    if _matches_any(_HIGH_RISK_PATTERNS, normalized):
        return "REASONING", "high_risk_rule", 1.0, ("high_risk",)

    if _matches_any(_DEEP_REASONING_PATTERNS, normalized):
        return "REASONING", "deep_reasoning_rule", 1.0, ("deep_reasoning",)

    if _matches_any(_MEDIUM_REFERENCE_UI_PATTERNS, normalized):
        return "MEDIUM", "reference_ui_rule", 0.5, ("reference_ui",)

    signals: list[str] = []
    score = 0.34
    complex_phrase = _matches_any(_COMPLEX_PHRASE_PATTERNS, normalized)
    scope_word_count = sum(1 for word in _SCOPE_WORDS if _contains_term(lowered, word))
    scope_object_count = sum(1 for word in _SCOPE_OBJECTS if _contains_term(lowered, word))
    multistep = _matches_any(_MULTISTEP_PATTERNS, normalized)
    simple = _matches_any(_SIMPLE_PATTERNS, normalized)
    technical_count = sum(1 for term in _TECHNICAL_TERMS if _contains_term(lowered, term))

    if complex_phrase:
        signals.append("complex_phrase")
        score += 0.34
    if scope_word_count and scope_object_count:
        signals.append("broad_scope")
        score += 0.28
    if multistep:
        signals.append("multi_step")
        score += 0.16
    if technical_count:
        signals.append("technical_terms")
        score += min(technical_count, 3) * 0.05
    if len(normalized) >= 1200:
        signals.append("long_request")
        score += 0.16
    elif len(normalized) >= 500:
        signals.append("moderate_length")
        score += 0.08
    if simple and not complex_phrase and not (scope_word_count and scope_object_count):
        signals.append("simple_scope")
        score -= 0.28
    if not normalized:
        signals.append("empty_user_text")

    score = round(max(0.0, min(score, 1.0)), 3)
    if simple and score < 0.35:
        tier = "SIMPLE"
    elif score >= 0.60:
        tier = "COMPLEX"
    else:
        tier = "MEDIUM"
    return tier, "phase21_heuristic", score, tuple(signals or ["default_medium"])


def analyze_request(data: dict[str, Any]) -> RoutingDecision:
    user_text = extract_last_user_text(data) or ""
    control, stripped_text = parse_control_prefix(user_text)
    tier, cause, score, signals = classify_text(stripped_text, control=control)
    previous_user_text = extract_previous_user_text(data)
    if (
        (control is None or control in {"auto", "自动"})
        and _matches_any(_SHORT_FOLLOW_UP_PATTERNS, stripped_text)
        and previous_user_text
    ):
        inherited_tier, _, inherited_score, inherited_signals = classify_text(previous_user_text)
        tier = inherited_tier
        cause = "context_inheritance"
        score = inherited_score
        signals = ("context_inheritance", *inherited_signals)
    explicit_xhigh = _EXPLICIT_XHIGH_PHRASE in stripped_text
    if explicit_xhigh:
        tier = "REASONING"
        cause = "explicit_complex_reasoning"
        score = 1.0
        signals = ("explicit_xhigh_phrase",)
    raw_turn_id = extract_turn_id(data)
    session_id = extract_session_id(data)
    fingerprint = _short_hash("prompt", stripped_text) if stripped_text else None
    if raw_turn_id:
        logical_turn_id = _short_hash("turn", raw_turn_id)
    elif fingerprint:
        logical_turn_id = _short_hash("legacy-turn", fingerprint)
    else:
        previous = data.get("previous_response_id")
        logical_turn_id = _short_hash("response-lineage", str(previous)) if previous else None
    routing_mode = "manual" if control and control not in {"auto", "自动"} else "auto"
    return RoutingDecision(
        tier=tier,
        selected_alias="sol-xhigh" if explicit_xhigh else PHASE2_ALIASES[tier],
        routing_mode=routing_mode,
        routing_cause=cause,
        score=score,
        signals=signals,
        override=control if routing_mode == "manual" else None,
        logical_turn_id=logical_turn_id,
        session_hash=_short_hash("session", session_id) if session_id else None,
        prompt_fingerprint=fingerprint,
        user_prompt_chars=len(stripped_text),
    )


def decision_from_dict(value: dict[str, Any]) -> RoutingDecision:
    return RoutingDecision(
        tier=str(value["tier"]),
        selected_alias=str(value["selected_alias"]),
        routing_mode=str(value["routing_mode"]),
        routing_cause=str(value["routing_cause"]),
        score=float(value["score"]),
        signals=tuple(str(item) for item in value.get("signals") or []),
        override=str(value["override"]) if value.get("override") is not None else None,
        logical_turn_id=str(value["logical_turn_id"]) if value.get("logical_turn_id") else None,
        session_hash=str(value["session_hash"]) if value.get("session_hash") else None,
        prompt_fingerprint=str(value["prompt_fingerprint"]) if value.get("prompt_fingerprint") else None,
        user_prompt_chars=int(value.get("user_prompt_chars") or 0),
    )


def runtime_source() -> str:
    from pathlib import Path

    return Path(__file__).read_text(encoding="utf-8")


def safe_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
