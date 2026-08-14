from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import yaml

ProfileName = Literal["gpt56"]
PhaseName = Literal["phase2", "phase3", "phase3a"]


@dataclass(frozen=True)
class ModelProfile:
    name: str
    simple: str
    medium: str
    complex: str
    reasoning: str
    oauth_bootstrap: str
    review: str | None = None
    phase2_simple_effort: str = "medium"
    phase2_medium_effort: str = "medium"
    phase2_complex_effort: str = "medium"
    phase2_reasoning_effort: str = "medium"
    phase2_review_effort: str = "medium"
    phase3_simple_effort: str = "low"
    phase3_medium_effort: str = "medium"
    phase3_complex_effort: str = "high"
    phase3_reasoning_effort: str = "xhigh"
    phase3_review_effort: str = "high"
    phase3a_simple_effort: str = "low"
    phase3a_medium_effort: str = "medium"
    phase3a_complex_effort: str = "medium"
    phase3a_reasoning_effort: str = "high"
    phase3a_review_effort: str = "high"


PROFILES: dict[str, ModelProfile] = {
    "gpt56": ModelProfile(
        name="gpt56",
        simple="chatgpt/gpt-5.6-luna",
        medium="chatgpt/gpt-5.6-terra",
        complex="chatgpt/gpt-5.6-sol",
        reasoning="chatgpt/gpt-5.6-sol",
        # Use the target everyday model for OAuth. If the model check fails after
        # tokens were stored, the separate phase probe remains the activation gate.
        oauth_bootstrap="chatgpt/gpt-5.6-terra",
    ),
}


_SIMPLE_KEYWORDS = [
    "fix typo",
    "rename variable",
    "format code",
    "update copy",
    "change label",
    "add comment",
    "修改拼写",
    "改文案",
    "改变量名",
    "格式化代码",
    "增加注释",
    "修改标签",
]

_COMPLEX_KEYWORDS = [
    "root cause",
    "multiple modules",
    "cross-service",
    "architecture refactor",
    "performance bottleneck",
    "websocket",
    "kubernetes",
    "database migration",
    "查找根本原因",
    "多模块",
    "跨服务",
    "架构重构",
    "性能瓶颈",
    "数据库迁移",
    "长连接",
]

_REASONING_KEYWORDS = [
    "这是复杂推理",
    "deadlock",
    "race condition",
    "security audit",
    "data loss",
    "production outage",
    "privilege escalation",
    "cryptography",
    "distributed consensus",
    "死锁",
    "竞态条件",
    "安全审计",
    "数据丢失",
    "生产故障",
    "权限漏洞",
    "分布式一致性",
]

_TECHNICAL_KEYWORDS = [
    "postgresql",
    "mysql",
    "redis",
    "kafka",
    "kubernetes",
    "docker",
    "websocket",
    "oauth",
    "authentication",
    "authorization",
    "migration",
    "concurrency",
    "transaction",
    "rollback",
    "react native",
    "swift",
    "typescript",
    "python",
]


def _model_entry(alias: str, provider_model: str, effort: str) -> dict[str, Any]:
    return {
        "model_name": alias,
        "model_info": {"mode": "responses"},
        "litellm_params": {
            "model": provider_model,
            "reasoning_effort": effort,
        },
    }


def _router_entry(tiers: dict[str, str], model_name: str = "codex-auto") -> dict[str, Any]:
    return {
        "model_name": model_name,
        "model_info": {"mode": "responses"},
        "litellm_params": {
            "model": "auto_router/complexity_router",
            "drop_params": True,
            "complexity_router_config": {
                "tiers": tiers,
                "classifier_type": "heuristic",
                # Reclassify each turn. This avoids pinning a whole coding session to
                # an expensive model merely because one turn was difficult.
                "session_affinity": False,
                "keyword_tier_rules": [
                    {"keywords": _SIMPLE_KEYWORDS, "tier": "SIMPLE"},
                    {"keywords": _COMPLEX_KEYWORDS, "tier": "COMPLEX"},
                    {"keywords": _REASONING_KEYWORDS, "tier": "REASONING"},
                ],
                "custom_technical_keywords": _TECHNICAL_KEYWORDS,
                # Codex requests naturally contain code and long system context.
                # Reduce the raw code/token influence so routine edits are not
                # automatically promoted to the frontier tier.
                "tier_boundaries": {
                    "simple_medium": 0.30,
                    "medium_complex": 0.62,
                    "complex_reasoning": 0.84,
                },
                "token_thresholds": {"simple": 40, "complex": 1200},
                "dimension_weights": {
                    "tokenCount": 0.05,
                    "codePresence": 0.12,
                    "reasoningMarkers": 0.23,
                    "technicalTerms": 0.25,
                    "simpleIndicators": 0.10,
                    "multiStepPatterns": 0.18,
                    "questionComplexity": 0.07,
                },
            },
            "complexity_router_default_model": tiers["MEDIUM"],
        },
    }


def build_config(
    phase: PhaseName,
    profile_name: ProfileName,
    master_key_env: str = "LITELLM_MASTER_KEY",
    *,
    phase21_policy: bool = False,
) -> dict[str, Any]:
    profile = PROFILES[profile_name]
    if phase == "phase2":
        aliases = {
            "SIMPLE": "luna-medium",
            "MEDIUM": "terra-medium",
            "COMPLEX": "sol-medium",
            "REASONING": "reasoning-medium",
        }
        review_model = profile.review or profile.complex
        entries = [
            _model_entry("luna-medium", profile.simple, profile.phase2_simple_effort),
            _model_entry("terra-medium", profile.medium, profile.phase2_medium_effort),
            _model_entry("sol-medium", profile.complex, profile.phase2_complex_effort),
            _model_entry("reasoning-medium", profile.reasoning, profile.phase2_reasoning_effort),
            # Codex IDE auxiliary calls have been observed requesting these
            # exact aliases instead of the configured primary model.
            _model_entry("gpt-5.6-luna", profile.simple, profile.phase2_simple_effort),
            _model_entry("gpt-5.6-terra", profile.medium, profile.phase2_medium_effort),
            _model_entry("gpt-5.6-sol", profile.complex, profile.phase2_complex_effort),
            _model_entry("codex-auto-review", review_model, profile.phase2_review_effort),
        ]
        if phase21_policy:
            # Phase 2.1 normally keeps all automatic routes at medium effort.
            # This isolated alias is selected only by the explicit Chinese
            # phrase handled by codex_routing_policy.
            entries.append(_model_entry("sol-xhigh", profile.reasoning, "xhigh"))
        fallbacks = [
            {"luna-medium": ["terra-medium", "sol-medium", "reasoning-medium"]},
            {"terra-medium": ["sol-medium", "reasoning-medium"]},
            {"sol-medium": ["reasoning-medium"]},
        ]
    elif phase == "phase3":
        aliases = {
            "SIMPLE": "luna-low",
            "MEDIUM": "terra-medium",
            "COMPLEX": "sol-high",
            "REASONING": "sol-xhigh",
        }
        review_model = profile.review or profile.complex
        entries = [
            _model_entry("luna-low", profile.simple, profile.phase3_simple_effort),
            _model_entry("terra-medium", profile.medium, profile.phase3_medium_effort),
            _model_entry("sol-high", profile.complex, profile.phase3_complex_effort),
            _model_entry("sol-xhigh", profile.reasoning, profile.phase3_reasoning_effort),
            _model_entry("gpt-5.6-luna", profile.simple, profile.phase3_simple_effort),
            _model_entry("gpt-5.6-terra", profile.medium, profile.phase3_medium_effort),
            _model_entry("gpt-5.6-sol", profile.complex, profile.phase3_complex_effort),
            _model_entry("codex-auto-review", review_model, profile.phase3_review_effort),
        ]
        fallbacks = [
            {"luna-low": ["terra-medium", "sol-high"]},
            {"terra-medium": ["sol-high", "sol-xhigh"]},
            {"sol-high": ["sol-xhigh"]},
        ]
    else:
        # Phase 3A keeps routine complex work at Sol/medium.  Only the
        # reasoning tier receives Sol/high; xhigh remains a rescue alias.
        aliases = {
            "SIMPLE": "luna-low",
            "MEDIUM": "terra-medium",
            "COMPLEX": "sol-medium",
            "REASONING": "sol-high",
        }
        review_model = profile.review or profile.complex
        entries = [
            _model_entry("luna-low", profile.simple, profile.phase3a_simple_effort),
            _model_entry("terra-medium", profile.medium, profile.phase3a_medium_effort),
            _model_entry("sol-medium", profile.complex, profile.phase3a_complex_effort),
            _model_entry("sol-high", profile.reasoning, profile.phase3a_reasoning_effort),
            _model_entry("sol-xhigh", profile.reasoning, "xhigh"),
            _model_entry("gpt-5.6-luna", profile.simple, profile.phase3a_simple_effort),
            _model_entry("gpt-5.6-terra", profile.medium, profile.phase3a_medium_effort),
            _model_entry("gpt-5.6-sol", profile.complex, profile.phase3a_complex_effort),
            _model_entry("codex-auto-review", review_model, profile.phase3a_review_effort),
        ]
        fallbacks = [
            {"luna-low": ["terra-medium", "sol-medium", "sol-high"]},
            {"terra-medium": ["sol-medium", "sol-high"]},
            {"sol-medium": ["sol-high", "sol-xhigh"]},
            {"sol-high": ["sol-xhigh"]},
        ]

    routers = [_router_entry(aliases, "codex-auto")]
    if phase in {"phase3", "phase3a"}:
        # The guarded task runner starts with automatic model choice capped at
        # high effort, then escalates a failed verification to sol-xhigh.
        task_aliases = dict(aliases)
        task_aliases["REASONING"] = "sol-high"
        routers.append(_router_entry(task_aliases, "codex-task-auto"))

    return {
        "model_list": [*entries, *routers],
        "router_settings": {
            "num_retries": 1,
            "timeout": 7200,
            "fallbacks": fallbacks,
        },
        "litellm_settings": {
            "drop_params": True,
            "callbacks": (
                [
                    "routing_callback_candidate.route_controller",
                    "audit_callback_candidate.audit_logger",
                ]
                if phase21_policy
                else ["audit_callback.audit_logger"]
            ),
            "set_verbose": False,
        },
        "general_settings": {
            "master_key": f"os.environ/{master_key_env}",
        },
    }


def render_yaml(phase: PhaseName, profile_name: ProfileName) -> str:
    header = (
        "# Generated by litellm-codex-control.\n"
        f"# phase={phase}; profile={profile_name}\n"
        "# Edit through the controller so backups and validation remain consistent.\n"
    )
    return header + yaml.safe_dump(
        build_config(phase, profile_name),
        allow_unicode=True,
        sort_keys=False,
        width=120,
    )


def render_phase21_yaml(profile_name: ProfileName) -> str:
    header = (
        "# Generated by litellm-codex-control.\n"
        f"# phase=phase2.1; profile={profile_name}\n"
        "# Candidate policy: activate only through the controller after validation.\n"
    )
    return header + yaml.safe_dump(
        build_config("phase2", profile_name, phase21_policy=True),
        allow_unicode=True,
        sort_keys=False,
        width=120,
    )


def render_phase3a_yaml(profile_name: ProfileName) -> str:
    header = (
        "# Generated by litellm-codex-control.\n"
        f"# phase=phase3a; profile={profile_name}\n"
        "# Candidate policy: activate only through the controller after validation.\n"
    )
    return header + yaml.safe_dump(
        build_config("phase3a", profile_name, phase21_policy=True),
        allow_unicode=True,
        sort_keys=False,
        width=120,
    )


def profile_probe_models(profile_name: ProfileName, phase: PhaseName) -> list[dict[str, Any]]:
    """Return the minimum distinct provider calls required to validate a phase.

    Phase 2 validates its normal ``medium`` routes plus the explicit Sol/xhigh
    escape hatch used by Phase 2.1. Phase 3 separately validates
    low/medium/high/xhigh before it may be activated. Repeated provider-model/
    effort pairs are de-duplicated to avoid wasting subscription allowance.
    """
    profile = PROFILES[profile_name]
    if phase == "phase2":
        raw = [
            ("SIMPLE", profile.simple, profile.phase2_simple_effort),
            ("MEDIUM", profile.medium, profile.phase2_medium_effort),
            ("COMPLEX", profile.complex, profile.phase2_complex_effort),
            ("REASONING", profile.reasoning, profile.phase2_reasoning_effort),
            ("EXPLICIT_XHIGH", profile.reasoning, "xhigh"),
        ]
    elif phase == "phase3":
        raw = [
            ("SIMPLE", profile.simple, profile.phase3_simple_effort),
            ("MEDIUM", profile.medium, profile.phase3_medium_effort),
            ("COMPLEX", profile.complex, profile.phase3_complex_effort),
            ("REASONING", profile.reasoning, profile.phase3_reasoning_effort),
        ]
    else:
        raw = [
            ("SIMPLE", profile.simple, profile.phase3a_simple_effort),
            ("MEDIUM", profile.medium, profile.phase3a_medium_effort),
            ("REASONING", profile.reasoning, profile.phase3a_reasoning_effort),
            ("EXPLICIT_XHIGH", profile.reasoning, "xhigh"),
        ]

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for tier, model, effort in raw:
        key = (model, effort)
        item = grouped.setdefault(
            key,
            {"tiers": [], "model": model, "effort": effort},
        )
        item["tiers"].append(tier)
    return list(grouped.values())


AUDIT_CALLBACK_SOURCE = r'''from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import time
import zlib
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from litellm.integrations.custom_logger import CustomLogger
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


AUDIT_PATH = Path(os.environ.get("CODEX_ROUTER_AUDIT_LOG", "route-audit.jsonl"))
PAYLOAD_PATH = Path(os.environ.get("CODEX_ROUTER_ENCRYPTED_PAYLOAD_LOG", "call-payloads.jsonl.enc"))
PAYLOAD_KEY = os.environ.get("CODEX_ROUTER_PAYLOAD_KEY", "")
_PAYLOAD_AAD = b"routerllm-call-payload-v1"
_PAYLOAD_AAD_V2_IDENTITY = b"routerllm-call-payload-v2:json"
_PAYLOAD_AAD_V2_ZLIB = b"routerllm-call-payload-v2:zlib-json"


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    for method in ("model_dump", "dict"):
        fn = getattr(value, method, None)
        if callable(fn):
            try:
                return _plain(fn())
            except Exception:
                pass
    return str(value)


def _request_fingerprint(body: dict[str, Any]) -> tuple[str, int]:
    payload = body.get("input", body.get("messages", ""))
    try:
        serialized = json.dumps(_plain(payload), ensure_ascii=False, sort_keys=True)
    except Exception:
        serialized = str(payload)
    return hashlib.sha256(serialized.encode("utf-8", errors="replace")).hexdigest()[:16], len(serialized)


def _usage(response_obj: Any) -> dict[str, Any] | None:
    usage = getattr(response_obj, "usage", None)
    value = _plain(usage) if usage is not None else None
    if not isinstance(value, dict):
        return value
    input_details = value.get("input_tokens_details")
    output_details = value.get("output_tokens_details")
    if not isinstance(input_details, dict):
        input_details = {}
    if not isinstance(output_details, dict):
        output_details = {}
    value["cache_read_input_tokens"] = _first_not_none(
        value.get("cache_read_input_tokens"),
        value.get("cached_tokens"),
        input_details.get("cached_tokens"),
        0,
    )
    value["cache_write_tokens"] = _first_not_none(
        value.get("cache_write_tokens"),
        value.get("cache_creation_input_tokens"),
        input_details.get("cache_write_tokens"),
        0,
    )
    value["reasoning_tokens"] = _first_not_none(
        value.get("reasoning_tokens"),
        output_details.get("reasoning_tokens"),
        0,
    )
    return value


def _first_not_none(*values: Any) -> Any:
    return next((value for value in values if value is not None), None)


def _safe_error(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)[:1500]
    text = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/-]+", "Bearer ***REDACTED***", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "sk-***REDACTED***", text)
    return text


def _hashed_id(namespace: str, value: Any) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(f"{namespace}:{value}".encode("utf-8", errors="replace")).hexdigest()[:20]


def _runtime_route(body: dict[str, Any], kwargs: dict[str, Any]) -> dict[str, Any]:
    try:
        from routing_callback_candidate import route_controller

        params = kwargs.get("litellm_params") or {}
        call_id = kwargs.get("litellm_call_id") or params.get("litellm_call_id")
        route = route_controller.audit_route_for(body, call_id=call_id)
        return route if isinstance(route, dict) else {}
    except Exception:
        return {}


def _write(record: dict[str, Any]) -> None:
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    with AUDIT_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
    if os.name != "nt":
        AUDIT_PATH.chmod(0o600)


def _redact_payload(value: Any, key: str | None = None) -> Any:
    normalized_key = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_") if key else ""
    token_metrics = {
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "prompt_tokens",
        "completion_tokens",
        "cached_tokens",
        "cache_read_tokens",
        "cache_read_input_tokens",
        "cache_write_tokens",
        "cache_creation_input_tokens",
        "reasoning_tokens",
    }
    credentials = {
        "authorization",
        "proxy_authorization",
        "api_key",
        "openai_api_key",
        "access_token",
        "refresh_token",
        "id_token",
        "bearer_token",
        "password",
        "cookie",
        "set_cookie",
        "secret",
        "client_secret",
        "master_key",
        "proxy_master_key",
    }
    credential_suffixes = ("_api_key", "_access_token", "_refresh_token", "_id_token", "_password", "_secret")
    if normalized_key not in token_metrics and (
        normalized_key in credentials or any(normalized_key.endswith(suffix) for suffix in credential_suffixes)
    ):
        return "***REDACTED***"
    plain = _plain(value)
    if isinstance(plain, dict):
        return {str(name): _redact_payload(item, str(name)) for name, item in plain.items()}
    if isinstance(plain, list):
        return [_redact_payload(item) for item in plain]
    return plain


def _request_body(data: dict[str, Any]) -> dict[str, Any]:
    params = data.get("litellm_params") or {}
    proxy_request = params.get("proxy_server_request") or data.get("proxy_server_request") or {}
    body = proxy_request.get("body") or {}
    if isinstance(body, dict) and body:
        return body
    if any(key in data for key in ("input", "messages", "model", "stream")):
        return data
    return {}


def _response_output(response_obj: Any) -> list[Any]:
    plain = _plain(response_obj)
    if not isinstance(plain, dict):
        return []
    output = plain.get("output")
    return output if isinstance(output, list) else []


def _output_text(items: list[Any]) -> str:
    parts: list[str] = []
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif part.get("type") == "refusal" and isinstance(part.get("refusal"), str):
                parts.append(part["refusal"])
    return "\n".join(part for part in parts if part)


def _output_item_breakdown(items: list[Any]) -> dict[str, int]:
    visible_message_count = 0
    reasoning_item_count = 0
    refusal_count = 0
    tool_call_count = 0
    other_item_count = 0
    for item in items:
        if not isinstance(item, dict):
            other_item_count += 1
            continue
        item_type = item.get("type")
        if item_type == "reasoning":
            reasoning_item_count += 1
        elif item_type == "function_call":
            tool_call_count += 1
        elif item_type == "refusal":
            refusal_count += 1
        elif item_type == "message":
            content = item.get("content")
            if not isinstance(content, list):
                other_item_count += 1
                continue
            has_refusal = any(
                isinstance(part, dict) and part.get("type") == "refusal"
                for part in content
            )
            has_visible_text = any(
                isinstance(part, dict)
                and part.get("type") == "output_text"
                and isinstance(part.get("text"), str)
                and bool(part["text"])
                for part in content
            )
            if has_refusal:
                refusal_count += 1
            elif has_visible_text:
                visible_message_count += 1
            else:
                other_item_count += 1
        else:
            other_item_count += 1
    return {
        "response_item_count": len(items),
        "visible_message_count": visible_message_count,
        "reasoning_item_count": reasoning_item_count,
        "refusal_count": refusal_count,
        "tool_call_count": tool_call_count,
        "other_item_count": other_item_count,
    }


def _builtin_tool_usage(items: list[Any]) -> dict[str, int]:
    aliases = {
        "web_search_call": "web_search",
        "file_search_call": "file_search",
        "code_interpreter_call": "code_interpreter",
        "hosted_shell_call": "hosted_shell",
        "image_generation_call": "image_generation",
    }
    counts: dict[str, int] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        tool_type = aliases.get(str(item.get("type") or ""))
        if tool_type is not None:
            counts[tool_type] = counts.get(tool_type, 0) + 1
    return counts


def _output_semantics_source(aggregation_source: Any) -> str:
    aliases = {
        "output_item_done": "sse_output_item_done",
        "stream_deltas": "sse_delta_reconstruction",
    }
    source = aliases.get(aggregation_source, aggregation_source)
    if source in {
        "terminal_response",
        "sse_output_item_done",
        "sse_delta_reconstruction",
        "mixed",
    }:
        return source
    return "unknown"


def _metadata_value(key: str, *sources: Any) -> str | None:
    for source in sources:
        if not isinstance(source, dict):
            continue
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _turn_metadata(*sources: Any) -> dict[str, Any]:
    for source in sources:
        if not isinstance(source, dict):
            continue
        for metadata_key in ("x-codex-turn-metadata", "x_codex_turn_metadata", "codex_turn_metadata"):
            value = source.get(metadata_key)
            if isinstance(value, dict):
                return value
            if isinstance(value, str):
                try:
                    parsed = json.loads(value)
                except Exception:
                    continue
                if isinstance(parsed, dict):
                    return parsed
    return {}


def _call_identity(
    *,
    body: dict[str, Any],
    route: dict[str, Any],
    metadata: dict[str, Any],
    headers: dict[str, Any],
    request_kind: str,
) -> dict[str, Any]:
    body_metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    turn_metadata = _turn_metadata(metadata, body_metadata, body)
    normalized_headers = {
        str(key).lower(): value
        for key, value in headers.items()
        if isinstance(key, str)
    }
    subagent_kind = _metadata_value("subagent_kind", route, turn_metadata, metadata, body_metadata, body)
    if subagent_kind is None:
        header_value = normalized_headers.get("x-openai-subagent")
        subagent_kind = header_value.strip() if isinstance(header_value, str) and header_value.strip() else None
    if isinstance(subagent_kind, str):
        subagent_kind = subagent_kind.lower()
    thread_source = _metadata_value("thread_source", route, turn_metadata, metadata, body_metadata, body)
    if isinstance(thread_source, str):
        thread_source = thread_source.lower()
    parent_thread_id = _metadata_value("parent_thread_id", route, turn_metadata, metadata, body_metadata, body)
    parent_turn_id = _metadata_value("parent_turn_id", route, turn_metadata, metadata, body_metadata, body)
    reviewed_action_type = _metadata_value("reviewed_action_type", route, turn_metadata, metadata, body_metadata, body)
    if reviewed_action_type is None:
        reviewed_action_type = _metadata_value("action_type", route, turn_metadata, metadata, body_metadata)
    explicit_internal = None
    for source in (route, turn_metadata, metadata, body_metadata, body):
        if isinstance(source, dict) and isinstance(source.get("internal_call"), bool):
            explicit_internal = source["internal_call"]
            break
    if subagent_kind == "guardian":
        call_kind = "guardian_review"
    elif request_kind == "compaction":
        call_kind = "compaction"
    elif request_kind == "tool_continuation":
        call_kind = "tool_continuation"
    elif request_kind in {"human_handoff", "handoff"} or thread_source in {"human_handoff", "handoff"}:
        call_kind = "human_handoff"
    elif request_kind in {"background_internal", "internal"}:
        call_kind = "background_internal"
    elif subagent_kind or thread_source == "subagent":
        call_kind = "subagent"
    elif thread_source in {"primary", "main", "root", "user"}:
        call_kind = "primary_agent"
    else:
        call_kind = "unknown"
    internal_call = explicit_internal
    if internal_call is None and call_kind in {"guardian_review", "compaction", "background_internal"}:
        internal_call = True
    return {
        "call_kind": call_kind,
        "subagent_kind": subagent_kind,
        "thread_source": thread_source,
        "parent_thread_id": parent_thread_id,
        "parent_turn_id": parent_turn_id,
        "internal_call": internal_call,
        "reviewed_action_type": reviewed_action_type,
    }


def _is_guardian_request(body: dict[str, Any], headers: dict[str, Any]) -> bool:
    if body.get("model") == "codex-auto-review":
        return True
    metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    if metadata.get("subagent_kind") == "guardian":
        return True
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == "x-openai-subagent" and value == "guardian":
            return True
    return False


def _reduce_guardian_context(body: dict[str, Any]) -> dict[str, Any]:
    items = body.get("input")
    if not isinstance(items, list):
        return {"applied": False, "reason": "input_not_list"}
    original_count = len(items)
    protected_start = max(0, original_count - 12)
    seen: set[str] = set()
    reduced: list[Any] = []
    dropped_reasoning = 0
    dropped_duplicates = 0
    for index, item in enumerate(items):
        try:
            fingerprint = hashlib.sha256(
                json.dumps(_plain(item), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
        except Exception:
            fingerprint = None
        item_type = item.get("type") if isinstance(item, dict) else None
        if index < protected_start and item_type == "reasoning":
            dropped_reasoning += 1
            continue
        if index < protected_start and fingerprint is not None and fingerprint in seen:
            dropped_duplicates += 1
            continue
        if fingerprint is not None:
            seen.add(fingerprint)
        reduced.append(item)
    if len(reduced) != original_count:
        body["input"] = reduced
    action_basis = items[protected_start:]
    action_fingerprint = hashlib.sha256(
        json.dumps(_plain(action_basis), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "applied": len(reduced) != original_count,
        "original_item_count": original_count,
        "reduced_item_count": len(reduced),
        "dropped_reasoning_count": dropped_reasoning,
        "dropped_duplicate_count": dropped_duplicates,
        "protected_tail_item_count": len(action_basis),
        "action_fingerprint": action_fingerprint,
        "policy": "guardian_conservative_v1",
    }


def _display_status(
    *,
    callback_status: str,
    provider_status: str | None,
    breakdown: dict[str, int],
    output_tokens: int | None,
) -> str:
    if callback_status == "failure" or provider_status == "failed":
        return "failed"
    if provider_status == "incomplete":
        return "incomplete"
    if provider_status in {"cancelled", "canceled"}:
        return "cancelled"
    if breakdown["visible_message_count"] or breakdown["refusal_count"]:
        return "completed_with_visible_output"
    if breakdown["tool_call_count"]:
        return "completed_with_tool_call"
    if breakdown["reasoning_item_count"]:
        return "completed_internal_only"
    if (output_tokens or 0) > 0:
        return "completed_output_missing"
    return "completed_no_output"


def _stream_capture_summary(
    events: Any,
    *,
    stream_requested: Any,
    output_tokens: int | None,
) -> dict[str, int | str | None]:
    empty = {
        "stream_capture_state": "not_streamed" if stream_requested is False else "capture_missing" if stream_requested is True else "unknown",
        "stream_event_count": 0,
        "stream_first_sequence": None,
        "stream_last_sequence": None,
        "stream_gap_count": 0,
        "stream_text_delta_count": 0,
        "stream_output_item_added_count": 0,
        "stream_output_item_done_count": 0,
        "stream_function_argument_delta_count": 0,
        "stream_response_completed_count": 0,
        "stream_has_terminal_event": 0,
        "stream_capture_complete": 0,
    }
    if stream_requested is False:
        return empty
    if not isinstance(events, list) or not events:
        return empty
    sequences: list[int] = []
    added_item_keys: set[str] = set()
    done_item_keys: set[str] = set()
    text_delta_count = 0
    output_item_added_count = 0
    output_item_done_count = 0
    function_argument_delta_count = 0
    response_completed_count = 0
    has_terminal_event = False
    has_content_event = False
    corrupt = False
    for index, event in enumerate(events):
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            corrupt = True
            continue
        sequence = event.get("_routerllm_sequence", index)
        if not isinstance(sequence, int) or isinstance(sequence, bool):
            corrupt = True
            continue
        sequences.append(sequence)
        event_type = event["type"]
        if event_type in {"response.completed", "response.incomplete", "response.failed"}:
            has_terminal_event = True
            if event_type == "response.completed":
                response_completed_count += 1
            response = event.get("response")
            if isinstance(response, dict) and isinstance(response.get("output"), list) and response["output"]:
                has_content_event = True
        if event_type == "response.output_text.delta":
            text_delta_count += 1
            has_content_event = True
        if event_type == "response.function_call_arguments.delta":
            function_argument_delta_count += 1
            has_content_event = True
        elif event_type.startswith("response.function_call_arguments."):
            has_content_event = True
        if event_type == "response.output_item.added":
            output_item_added_count += 1
            item = event.get("item")
            if isinstance(item, dict):
                item_key = item.get("id") or event.get("item_id") or event.get("output_index")
                if item_key is not None:
                    added_item_keys.add(str(item_key))
        if event_type == "response.output_item.done":
            output_item_done_count += 1
            has_content_event = True
            item = event.get("item")
            item_key = (item.get("id") if isinstance(item, dict) else None) or event.get("item_id") or event.get("output_index")
            if item_key is not None:
                done_item_keys.add(str(item_key))
    sequence_gap_count = 0
    if not sequences or len(sequences) != len(events):
        corrupt = True
    else:
        expected = set(range(sequences[0], sequences[-1] + 1))
        sequence_gap_count = len(expected.difference(sequences))
        if sequence_gap_count or len(set(sequences)) != len(sequences):
            corrupt = True
    if corrupt:
        state = "captured_corrupt"
    elif not has_terminal_event or not added_item_keys.issubset(done_item_keys):
        state = "captured_partial"
    elif (output_tokens or 0) > 0 and not has_content_event:
        state = "captured_partial"
    else:
        state = "captured_complete"
    return {
        "stream_capture_state": state,
        "stream_event_count": len(events),
        "stream_first_sequence": sequences[0] if sequences else None,
        "stream_last_sequence": sequences[-1] if sequences else None,
        "stream_gap_count": sequence_gap_count,
        "stream_text_delta_count": text_delta_count,
        "stream_output_item_added_count": output_item_added_count,
        "stream_output_item_done_count": output_item_done_count,
        "stream_function_argument_delta_count": function_argument_delta_count,
        "stream_response_completed_count": response_completed_count,
        "stream_has_terminal_event": 1 if has_terminal_event else 0,
        "stream_capture_complete": 1 if state == "captured_complete" else 0,
    }


def _aggregate_capture(response_obj: Any, capture: dict[str, Any]) -> dict[str, Any]:
    terminal_response = _plain(response_obj)
    if not isinstance(terminal_response, dict):
        terminal_response = capture.get("terminal_response") or {}
    terminal_output = _response_output(terminal_response)
    done_items = sorted(
        capture.get("done_items", {}).values(),
        key=lambda entry: (entry.get("output_index", 10**9), entry.get("sequence", 10**9)),
    )
    if terminal_output:
        normalized_output = terminal_output
        aggregation_source = "terminal_response"
    elif done_items:
        normalized_output = [entry["item"] for entry in done_items]
        aggregation_source = "output_item_done"
    else:
        reconstructed: list[tuple[int, int, dict[str, Any]]] = []
        for entry in capture.get("text_deltas", {}).values():
            text = "".join(entry.get("parts") or [])
            if not text:
                continue
            item_id = entry.get("item_id") or f"reconstructed-message-{entry.get('output_index', 0)}"
            reconstructed.append((
                entry.get("output_index", 10**9),
                entry.get("sequence", 10**9),
                {
                    "id": item_id,
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": text, "annotations": []}],
                },
            ))
        for entry in capture.get("function_calls", {}).values():
            arguments = entry.get("arguments_done")
            if not isinstance(arguments, str):
                arguments = "".join(entry.get("argument_parts") or [])
            reconstructed.append((
                entry.get("output_index", 10**9),
                entry.get("sequence", 10**9),
                {
                    "id": entry.get("item_id"),
                    "type": "function_call",
                    "call_id": entry.get("call_id"),
                    "name": entry.get("name"),
                    "arguments": arguments,
                    "status": "completed",
                },
            ))
        reconstructed.sort(key=lambda entry: (entry[0], entry[1]))
        normalized_output = [entry[2] for entry in reconstructed]
        aggregation_source = "stream_deltas" if normalized_output else "none"
    text = _output_text(normalized_output)
    if not text:
        text = "".join(
            "".join(entry.get("parts") or [])
            for entry in sorted(
                capture.get("text_deltas", {}).values(),
                key=lambda entry: (entry.get("output_index", 10**9), entry.get("sequence", 10**9)),
            )
        )
    tool_calls = [item for item in normalized_output if isinstance(item, dict) and item.get("type") == "function_call"]
    return {
        "terminal_response": terminal_response,
        "terminal_output_item_count": len(terminal_output),
        "normalized_output_items": normalized_output,
        "output_text": text,
        "tool_calls": tool_calls,
        "aggregation_source": aggregation_source,
        "output_breakdown": _output_item_breakdown(normalized_output),
        "output_semantics_source": _output_semantics_source(aggregation_source),
    }


def _write_payload(
    payload_ref: str,
    body: dict[str, Any],
    response_obj: Any,
    capture: dict[str, Any],
) -> None:
    if not PAYLOAD_KEY:
        return
    try:
        key = base64.urlsafe_b64decode(PAYLOAD_KEY)
        if len(key) != 32:
            return
        aggregated = _aggregate_capture(response_obj, capture)
        payload = {
            "payload_schema_version": 4,
            "payload_ref": payload_ref,
            "request": _redact_payload(capture.get("original_request_body") or body),
            "terminal_response": _redact_payload(aggregated["terminal_response"]),
            "normalized_output_items": _redact_payload(aggregated["normalized_output_items"]),
            "visible_output_text": _redact_payload(aggregated["output_text"]),
            "tool_calls": _redact_payload(aggregated["tool_calls"]),
            "aggregation_source": aggregated["aggregation_source"],
            "output_semantics_version": 5,
            "output_semantics_source": aggregated["output_semantics_source"],
            "output_breakdown": aggregated["output_breakdown"],
            "stream_summary": capture.get("stream_summary"),
            "stream_events": _redact_payload(capture.get("events") or []),
        }
        nonce = os.urandom(12)
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        compressed = zlib.compress(serialized, level=6)
        if len(compressed) + 32 < len(serialized):
            encrypted_body = compressed
            payload_format = "aesgcm-zlib-json-v2"
            payload_aad = _PAYLOAD_AAD_V2_ZLIB
        else:
            encrypted_body = serialized
            payload_format = "aesgcm-json-v2"
            payload_aad = _PAYLOAD_AAD_V2_IDENTITY
        ciphertext = AESGCM(key).encrypt(nonce, encrypted_body, payload_aad)
        envelope = {
            "payload_ref": payload_ref,
            "format": payload_format,
            "nonce": base64.urlsafe_b64encode(nonce).decode("ascii"),
            "ciphertext": base64.urlsafe_b64encode(ciphertext).decode("ascii"),
        }
        PAYLOAD_PATH.parent.mkdir(parents=True, exist_ok=True)
        with PAYLOAD_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(envelope, ensure_ascii=True, separators=(",", ":")) + "\n")
            handle.flush()
        if os.name != "nt":
            PAYLOAD_PATH.chmod(0o600)
    except Exception:
        # Content capture must never break routing or leak raw data into the audit log.
        return


class RouteAuditLogger(CustomLogger):
    def __init__(self) -> None:
        self._routes: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._captures: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()

    def _prune_routes(self) -> None:
        cutoff = time.monotonic() - 7200
        expired = [key for key, value in self._routes.items() if value[0] < cutoff]
        for key in expired:
            self._routes.pop(key, None)
        while len(self._routes) > 3000:
            self._routes.popitem(last=False)

    def _prune_captures(self) -> None:
        cutoff = time.monotonic() - 7200
        expired = [key for key, value in self._captures.items() if value[0] < cutoff]
        for key in expired:
            self._captures.pop(key, None)
        while len(self._captures) > 9000:
            self._captures.popitem(last=False)

    def _capture_keys(
        self,
        data: dict[str, Any],
        *,
        body: dict[str, Any] | None = None,
        response_id: Any = None,
    ) -> list[str]:
        params = data.get("litellm_params") or {}
        keys: list[str] = []
        for value in (
            data.get("litellm_call_id"),
            params.get("litellm_call_id"),
            data.get("call_id"),
        ):
            if value:
                keys.append(f"call:{value}")
        if response_id:
            keys.append(f"response:{response_id}")
        request_body = body if isinstance(body, dict) else _request_body(data)
        if request_body:
            fingerprint, _ = _request_fingerprint(request_body)
            keys.append(f"body:{fingerprint}")
        return list(dict.fromkeys(keys))

    def _capture_for(
        self,
        data: dict[str, Any],
        *,
        body: dict[str, Any] | None = None,
        response_id: Any = None,
    ) -> dict[str, Any]:
        self._prune_captures()
        keys = self._capture_keys(data, body=body, response_id=response_id)
        capture = next((self._captures[key][1] for key in keys if key in self._captures), None)
        if capture is None:
            capture = {
                "payload_ref": secrets.token_urlsafe(18),
                "recorded": False,
                "events": [],
                "done_items": OrderedDict(),
                "text_deltas": OrderedDict(),
                "function_calls": OrderedDict(),
                "sequence": 0,
            }
        timestamp = time.monotonic()
        for key in keys:
            self._captures[key] = (timestamp, capture)
            self._captures.move_to_end(key)
        return capture

    @staticmethod
    def _event_response_id(event: dict[str, Any]) -> Any:
        response = event.get("response")
        if isinstance(response, dict) and response.get("id"):
            return response.get("id")
        return event.get("response_id")

    def _capture_stream_event(self, request_data: dict[str, Any], response_chunk: Any) -> None:
        event = _plain(response_chunk)
        if not isinstance(event, dict):
            return
        response_id = self._event_response_id(event)
        capture = self._capture_for(request_data, response_id=response_id)
        sequence = capture["sequence"]
        capture["sequence"] += 1
        event = {**event, "_routerllm_sequence": sequence}
        capture["events"].append(event)
        event_type = event.get("type")
        if event_type in {"response.completed", "response.incomplete", "response.failed"}:
            response = event.get("response")
            if isinstance(response, dict):
                capture["terminal_response"] = response
        if event_type == "response.output_item.done" and isinstance(event.get("item"), dict):
            item = event["item"]
            item_key = str(item.get("id") or event.get("item_id") or event.get("output_index") or sequence)
            capture["done_items"][item_key] = {
                "item": item,
                "output_index": event.get("output_index", 10**9),
                "sequence": sequence,
            }
        if event_type in {"response.output_item.added", "response.output_item.done"} and isinstance(event.get("item"), dict):
            item = event["item"]
            if item.get("type") == "function_call":
                call_key = str(item.get("call_id") or item.get("id") or event.get("output_index") or sequence)
                entry = capture["function_calls"].setdefault(call_key, {
                    "item_id": item.get("id"),
                    "call_id": item.get("call_id"),
                    "name": item.get("name"),
                    "argument_parts": [],
                    "output_index": event.get("output_index", 10**9),
                    "sequence": sequence,
                })
                entry.update({key: value for key, value in {
                    "item_id": item.get("id"),
                    "call_id": item.get("call_id"),
                    "name": item.get("name"),
                }.items() if value is not None})
                if isinstance(item.get("arguments"), str):
                    entry["arguments_done"] = item["arguments"]
        if event_type == "response.output_text.delta" and isinstance(event.get("delta"), str):
            text_key = str(event.get("item_id") or event.get("output_index") or 0)
            entry = capture["text_deltas"].setdefault(text_key, {
                "item_id": event.get("item_id"),
                "parts": [],
                "output_index": event.get("output_index", 10**9),
                "sequence": sequence,
            })
            entry["parts"].append(event["delta"])
        if event_type in {"response.function_call_arguments.delta", "response.function_call_arguments.done"}:
            call_key = str(event.get("call_id") or event.get("item_id") or event.get("output_index") or sequence)
            entry = capture["function_calls"].setdefault(call_key, {
                "item_id": event.get("item_id"),
                "call_id": event.get("call_id"),
                "name": event.get("name"),
                "argument_parts": [],
                "output_index": event.get("output_index", 10**9),
                "sequence": sequence,
            })
            if event_type.endswith(".delta") and isinstance(event.get("delta"), str):
                entry["argument_parts"].append(event["delta"])
            if event_type.endswith(".done") and isinstance(event.get("arguments"), str):
                entry["arguments_done"] = event["arguments"]

    def _remember_route(self, data: dict[str, Any]) -> None:
        metadata = data.get("metadata") or data.get("litellm_metadata") or {}
        route = metadata.get("codex_router") if isinstance(metadata, dict) else None
        if not isinstance(route, dict):
            return
        keys: list[str] = []
        call_id = data.get("litellm_call_id")
        if call_id:
            keys.append(f"call:{call_id}")
        turn_id = route.get("logical_turn_id")
        if turn_id:
            keys.append(f"turn:{turn_id}")
        fingerprint, _ = _request_fingerprint(data)
        keys.append(f"body:{fingerprint}")
        for key in keys:
            self._routes[key] = (time.monotonic(), dict(route))
            self._routes.move_to_end(key)
        self._prune_routes()

    def _cached_route(self, body: dict[str, Any], kwargs: dict[str, Any]) -> dict[str, Any]:
        self._prune_routes()
        params = kwargs.get("litellm_params") or {}
        call_id = kwargs.get("litellm_call_id") or params.get("litellm_call_id")
        keys: list[str] = []
        if call_id:
            keys.append(f"call:{call_id}")
        try:
            import codex_routing_policy as policy

            turn_id = policy.analyze_request(body).logical_turn_id
            if turn_id:
                keys.append(f"turn:{turn_id}")
        except Exception:
            pass
        fingerprint, _ = _request_fingerprint(body)
        keys.append(f"body:{fingerprint}")
        for key in keys:
            value = self._routes.get(key)
            if value is not None:
                return dict(value[1])
        return {}

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if isinstance(data, dict):
            self._remember_route(data)
            body = _request_body(data)
            capture = self._capture_for(data, body=body)
            params = data.get("litellm_params") or {}
            proxy_request = params.get("proxy_server_request") or {}
            headers = proxy_request.get("headers") if isinstance(proxy_request, dict) else {}
            if not isinstance(headers, dict):
                headers = {}
            if _is_guardian_request(body, headers):
                capture["original_request_body"] = _plain(body)
                capture["guardian_context_reduction"] = _reduce_guardian_context(body)
        return data

    async def async_post_call_streaming_deployment_hook(self, request_data, response_chunk, call_type):
        if isinstance(request_data, dict):
            try:
                self._capture_stream_event(request_data, response_chunk)
            except Exception:
                pass
        return response_chunk

    def _record(self, status: str, kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any) -> None:
        try:
            params = kwargs.get("litellm_params") or {}
            proxy_request = params.get("proxy_server_request") or {}
            body = proxy_request.get("body") or {}
            headers = proxy_request.get("headers") or {}
            if not isinstance(body, dict):
                body = {}
            if not isinstance(headers, dict):
                headers = {}
            capture = self._capture_for(kwargs, body=body, response_id=getattr(response_obj, "id", None))
            if capture.get("recorded"):
                return
            aggregated = _aggregate_capture(response_obj, capture)
            fingerprint, prompt_chars = _request_fingerprint(body)
            metadata = params.get("metadata") or params.get("litellm_metadata") or {}
            route = metadata.get("codex_router") if isinstance(metadata, dict) else None
            if not isinstance(route, dict):
                route = self._cached_route(body, kwargs)
            if not route:
                route = _runtime_route(body, kwargs)
            requested_alias = route.get("requested_alias") or body.get("model")
            if route.get("request_kind"):
                request_kind = route.get("request_kind")
            elif requested_alias == "codex-auto-review":
                request_kind = "review"
            elif requested_alias == "gpt-5.6-luna":
                request_kind = "auxiliary_luna"
            else:
                request_kind = "legacy_call"
            call_identity = _call_identity(
                body=body,
                route=route,
                metadata=metadata if isinstance(metadata, dict) else {},
                headers=headers,
                request_kind=request_kind,
            )
            model_info = params.get("model_info") or {}
            hidden = getattr(response_obj, "_hidden_params", {}) or {}
            reasoning = kwargs.get("reasoning") or params.get("reasoning") or {}
            if not isinstance(reasoning, dict):
                reasoning = {}
            effort = (
                kwargs.get("reasoning_effort")
                or params.get("reasoning_effort")
                or reasoning.get("effort")
            )
            duration_ms = None
            try:
                duration_ms = int((end_time - start_time).total_seconds() * 1000)
            except Exception:
                pass
            selected = (
                metadata.get("deployment")
                or metadata.get("model_group")
                or model_info.get("id")
                or kwargs.get("model")
            )
            provider_status = (
                aggregated["terminal_response"].get("status")
                if isinstance(aggregated["terminal_response"], dict)
                else None
            ) or ("failed" if status == "failure" else "completed")
            usage = _usage(response_obj)
            output_tokens = None
            if isinstance(usage, dict):
                output_tokens = _first_not_none(usage.get("output_tokens"), usage.get("completion_tokens"))
            output_breakdown = aggregated["output_breakdown"]
            stream_summary = _stream_capture_summary(
                capture.get("events"),
                stream_requested=body.get("stream"),
                output_tokens=output_tokens if isinstance(output_tokens, (int, float)) else None,
            )
            capture["stream_summary"] = stream_summary
            display_status = _display_status(
                callback_status=status,
                provider_status=provider_status,
                breakdown=output_breakdown,
                output_tokens=output_tokens if isinstance(output_tokens, (int, float)) else None,
            )
            record = {
                "audit_schema_version": max(int(route.get("audit_schema_version") or 1), 5),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "status": status,
                "provider_status": provider_status,
                "stream_capture_state": stream_summary["stream_capture_state"],
                "display_status": display_status,
                "output_item_count": output_breakdown["response_item_count"],
                "terminal_output_item_count": aggregated["terminal_output_item_count"],
                "tool_call_count": output_breakdown["tool_call_count"],
                "builtin_tool_usage": _builtin_tool_usage(aggregated["normalized_output_items"]),
                "output_semantics_version": 5,
                "output_semantics_source": aggregated["output_semantics_source"],
                **output_breakdown,
                **stream_summary,
                "aggregation_source": aggregated["aggregation_source"],
                "requested_model": body.get("model"),
                "requested_alias": requested_alias,
                "selected_model": selected,
                "selected_alias": route.get("selected_alias"),
                "selected_tier": route.get("tier"),
                "provider_model": kwargs.get("model"),
                "response_model": getattr(response_obj, "model", None),
                "observed_service_tier": _first_not_none(
                    getattr(response_obj, "service_tier", None),
                    hidden.get("service_tier"),
                    kwargs.get("service_tier"),
                ),
                "billing_mode": _first_not_none(
                    route.get("billing_mode"),
                    metadata.get("billing_mode") if isinstance(metadata, dict) else None,
                    hidden.get("billing_mode"),
                    "unknown",
                ),
                "billing_mode_source": _first_not_none(
                    route.get("billing_mode_source"),
                    metadata.get("billing_mode_source") if isinstance(metadata, dict) else None,
                ),
                "reasoning_effort": effort,
                "routing_mode": route.get("routing_mode") or "legacy",
                "routing_cause": (
                    route.get("routing_cause")
                    or (metadata.get("routing_cause") if isinstance(metadata, dict) else None)
                    or hidden.get("routing_cause")
                ),
                "routing_score": route.get("score"),
                "routing_signals": route.get("signals") or [],
                "request_kind": request_kind,
                **call_identity,
                "guardian_context_reduction": capture.get("guardian_context_reduction"),
                "logical_turn_id": route.get("logical_turn_id"),
                "session_hash": route.get("session_hash"),
                "inherited_from": route.get("logical_turn_id") if route.get("request_kind") == "tool_continuation" else None,
                "default_reason": route.get("default_reason"),
                "prompt_fingerprint": route.get("prompt_fingerprint") or fingerprint,
                "prompt_chars": prompt_chars,
                "prompt_chars_total": prompt_chars,
                "user_prompt_chars": route.get("user_prompt_chars"),
                "response_hash": _hashed_id("response", getattr(response_obj, "id", None)),
                "duration_ms": duration_ms,
                "usage": usage,
                "response_cost": _first_not_none(
                    hidden.get("response_cost"),
                    kwargs.get("response_cost"),
                ),
                "payload_ref": capture["payload_ref"],
            }
            if status == "failure":
                exc = kwargs.get("exception")
                record["error_type"] = type(exc).__name__ if exc is not None else None
                record["error"] = _safe_error(exc)
            _write(record)
            _write_payload(record["payload_ref"], body, response_obj, capture)
            capture["recorded"] = True
        except Exception as exc:
            try:
                _write({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "status": "audit_error",
                    "error": str(exc)[:1500],
                })
            except Exception:
                pass

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._record("success", kwargs, response_obj, start_time, end_time)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        self._record("failure", kwargs, response_obj, start_time, end_time)


audit_logger = RouteAuditLogger()
'''


OAUTH_PROBE_SOURCE = r'''from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import litellm


def auth_path() -> Path:
    token_dir = Path(os.environ.get("CHATGPT_TOKEN_DIR", "."))
    auth_name = os.environ.get("CHATGPT_AUTH_FILE", "auth.json")
    return token_dir / auth_name


def main() -> int:
    model = sys.argv[1]
    result_path = Path(sys.argv[2])
    result_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"Starting ChatGPT subscription OAuth with {model}", flush=True)
    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "ok": False,
        "oauth_authenticated": False,
        "model_check_ok": False,
    }
    try:
        response = litellm.responses(
            model=model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Return exactly OAUTH_OK."}
                    ],
                }
            ],
        )
        result["ok"] = True
        result["oauth_authenticated"] = auth_path().exists()
        result["model_check_ok"] = True
        result["response_id"] = getattr(response, "id", None)
        print("OAUTH_COMPLETED", flush=True)
        return_code = 0
    except Exception as exc:
        authenticated = auth_path().exists()
        result["oauth_authenticated"] = authenticated
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc)
        if authenticated:
            # Authentication succeeded, but the bootstrap model check did not.
            # The phase-specific model probe is the real activation gate.
            result["ok"] = True
            print(
                f"OAUTH_COMPLETED_MODEL_CHECK_FAILED: {type(exc).__name__}: {exc}",
                flush=True,
            )
            return_code = 0
        else:
            print(f"OAUTH_FAILED: {type(exc).__name__}: {exc}", flush=True)
            return_code = 1
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
'''

MODEL_PROBE_SOURCE = r'''from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import litellm


def main() -> int:
    spec_path = Path(sys.argv[1])
    result_path = Path(sys.argv[2])
    phase = sys.argv[3]
    profile = sys.argv[4]
    specs = json.loads(spec_path.read_text(encoding="utf-8"))
    results = []
    all_ok = True
    for spec in specs:
        model = spec["model"]
        effort = spec.get("effort")
        tiers = spec.get("tiers") or ([spec.get("tier")] if spec.get("tier") else [])
        item = {
            "tiers": tiers,
            "model": model,
            "effort": effort,
            "ok": False,
        }
        print(f"PROBE_START {model} effort={effort}", flush=True)
        try:
            kwargs = {
                "model": model,
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "Return exactly MODEL_OK."}
                        ],
                    }
                ],
            }
            if effort:
                kwargs["reasoning_effort"] = effort
            response = litellm.responses(**kwargs)
            item["ok"] = True
            item["response_id"] = getattr(response, "id", None)
            print(f"PROBE_OK {model}", flush=True)
        except Exception as exc:
            all_ok = False
            item["error_type"] = type(exc).__name__
            item["error"] = str(exc)
            print(f"PROBE_FAILED {model}: {type(exc).__name__}: {exc}", flush=True)
        results.append(item)
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "phase": phase,
        "profile": profile,
        "all_ok": all_ok,
        "results": results,
    }
    result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
'''


RUNTIME_INSTALL_SOURCE = r'''from __future__ import annotations

import json
import os
import subprocess
import sys
import venv
from datetime import datetime, timezone
from pathlib import Path


def run(command: list[str]) -> None:
    print("+ " + " ".join(command), flush=True)
    subprocess.run(command, check=True)


def main() -> int:
    venv_path = Path(sys.argv[1])
    result_path = Path(sys.argv[2])
    version_specs = sys.argv[3:]
    if not version_specs:
        raise SystemExit("At least one runtime package specification is required")
    result_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version_specs": version_specs,
        "ok": False,
    }
    try:
        if not venv_path.exists():
            print(f"Creating runtime venv at {venv_path}", flush=True)
            venv.EnvBuilder(
                with_pip=True,
                clear=False,
                symlinks=os.name != "nt",
            ).create(venv_path)
        python = venv_path / ("Scripts/python.exe" if sys.platform.startswith("win") else "bin/python")
        run([str(python), "-m", "pip", "install", "--upgrade", "pip", "wheel"])
        run([str(python), "-m", "pip", "install", *version_specs])
        run([
            str(python),
            "-c",
            "from fastapi.dependencies.utils import get_flat_dependant; import litellm.proxy.proxy_server",
        ])
        completed = subprocess.run(
            [
                str(python),
                "-c",
                (
                    "import importlib.metadata as m, json; "
                    "print(json.dumps({name: m.version(name) for name in "
                    "('litellm', 'fastapi', 'starlette')}))"
                ),
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        payload["ok"] = True
        payload["installed_versions"] = json.loads(completed.stdout.strip())
        print(f"RUNTIME_INSTALL_OK versions={payload['installed_versions']}", flush=True)
        code = 0
    except Exception as exc:
        payload["error_type"] = type(exc).__name__
        payload["error"] = str(exc)
        print(f"RUNTIME_INSTALL_FAILED: {type(exc).__name__}: {exc}", flush=True)
        code = 1
    result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
'''
