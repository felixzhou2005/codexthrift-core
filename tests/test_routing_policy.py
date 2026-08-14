from __future__ import annotations

import asyncio
import sys
import types

import pytest

from codexthrift import routing_policy
from codexthrift.routing_policy import (
    analyze_request,
    classify_text,
    extract_last_user_text,
    parse_control_prefix,
    strip_control_prefix_from_request,
)
from codexthrift.runtime_templates import ROUTING_CALLBACK_SOURCE


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("修改一个按钮文案", "SIMPLE"),
        ("修复这个拼写错误", "SIMPLE"),
        ("Format this file", "SIMPLE"),
        ("What is Redis?", "SIMPLE"),
        ("给这个组件增加加载状态", "MEDIUM"),
        ("修复登录接口的普通错误并运行相关测试", "MEDIUM"),
        ("Implement a scoped API endpoint", "MEDIUM"),
        ("全面分析整个项目并给出完整实施方案", "COMPLEX"),
        ("检查所有页面和全部菜单并实现未完成功能", "COMPLEX"),
        ("先检查数据库，再设计迁移方案，最后分阶段执行", "COMPLEX"),
        ("好的，按你的完整方案执行", "COMPLEX"),
        ("18.01 到 18.10 的 UI 都要重新设计，给我一个完整的方案", "COMPLEX"),
        ("全部找到演示说明并删除", "COMPLEX"),
        ("我们升级到第三阶段有什么好处，目前条件成熟吗？", "SIMPLE"),
        ("去掉最上面的一条，在各个菜单的页面都不要这个", "SIMPLE"),
        ("你要给我每个任务的详细内容，就是问题，我才能回忆起来，才能标注", "SIMPLE"),
        ("空出来的工具全部去掉，不要再占用空间", "MEDIUM"),
        ("自动化要改成定时任务，列表点击以后要进入详情并展示运行历史", "MEDIUM"),
        ("按方案执行，先修改 Figma，再修改代码", "COMPLEX"),
        ("分析 LiteLLM 的日志、模型选择和余额消耗，给优化方案", "REASONING"),
        ("参考 UI界面设计和功能设计，重新设计整个菜单并给完整方案", "REASONING"),
        ("调查生产故障和数据丢失的根本原因", "REASONING"),
        ("分析这个死锁和竞态条件", "REASONING"),
        ("执行安全审计并检查权限漏洞", "REASONING"),
    ],
)
def test_chinese_and_english_routing_matrix(text: str, expected: str) -> None:
    tier, _, _, _ = classify_text(text)
    assert tier == expected


@pytest.mark.parametrize(
    ("prefix", "expected_tier"),
    [
        ("[luna]", "SIMPLE"),
        ("[terra]", "MEDIUM"),
        ("[sol]", "REASONING"),
        ("[轻量]", "SIMPLE"),
        ("[标准]", "MEDIUM"),
        ("[最强]", "REASONING"),
    ],
)
def test_manual_prefix_is_deterministic(prefix: str, expected_tier: str) -> None:
    control, text = parse_control_prefix(f" {prefix} 修改一个文案")
    tier, cause, _, _ = classify_text(text, control=control)
    assert tier == expected_tier
    assert cause == "manual_override"


def test_auto_prefix_is_removed_but_keeps_automatic_classification() -> None:
    data = {
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "[auto] 全面分析整个项目"}],
            }
        ]
    }
    assert strip_control_prefix_from_request(data) == "auto"
    assert extract_last_user_text(data) == "全面分析整个项目"


def test_turn_id_is_hashed_and_prompt_text_is_not_in_decision() -> None:
    secret_text = "[sol] 全面分析 private-project-name"
    data = {
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": secret_text}],
                "internal_chat_message_metadata_passthrough": {"turn_id": "turn-secret-123"},
            }
        ]
    }
    decision = analyze_request(data).to_dict()
    assert decision["logical_turn_id"] != "turn-secret-123"
    assert secret_text not in repr(decision)
    assert decision["selected_alias"] == "reasoning-medium"


def test_install_does_not_accidentally_match_ascii_all_scope_word() -> None:
    tier, _, _, signals = classify_text("Install the package and run one test")
    assert tier == "MEDIUM"
    assert "broad_scope" not in signals


def _load_runtime_callback(monkeypatch: pytest.MonkeyPatch):
    litellm_module = types.ModuleType("litellm")
    integrations_module = types.ModuleType("litellm.integrations")
    logger_module = types.ModuleType("litellm.integrations.custom_logger")

    class CustomLogger:
        pass

    logger_module.CustomLogger = CustomLogger
    monkeypatch.setitem(sys.modules, "litellm", litellm_module)
    monkeypatch.setitem(sys.modules, "litellm.integrations", integrations_module)
    monkeypatch.setitem(sys.modules, "litellm.integrations.custom_logger", logger_module)
    monkeypatch.setitem(sys.modules, "codex_routing_policy", routing_policy)
    namespace: dict[str, object] = {}
    exec(compile(ROUTING_CALLBACK_SOURCE, "routing_callback_candidate.py", "exec"), namespace)
    return namespace["route_controller"]


def test_one_turn_inherits_model_and_next_turn_reclassifies(monkeypatch: pytest.MonkeyPatch) -> None:
    callback = _load_runtime_callback(monkeypatch)
    first = {
        "model": "codex-auto",
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "全面分析所有模块并给出完整方案"}],
                "internal_chat_message_metadata_passthrough": {"turn_id": "turn-complex"},
            }
        ],
    }
    routed_first = asyncio.run(callback.async_pre_call_hook(None, None, first, "aresponses"))
    assert routed_first["model"] == "sol-medium"
    assert routed_first["metadata"]["codex_router"]["request_kind"] == "user_turn"

    continuation = {
        "model": "codex-auto",
        "input": [
            {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": "tool output",
                "internal_chat_message_metadata_passthrough": {"turn_id": "turn-complex"},
            }
        ],
    }
    routed_continuation = asyncio.run(
        callback.async_pre_call_hook(None, None, continuation, "aresponses")
    )
    assert routed_continuation["model"] == "sol-medium"
    assert routed_continuation["metadata"]["codex_router"]["request_kind"] == "tool_continuation"
    assert routed_continuation["metadata"]["codex_router"]["routing_cause"] == "turn_inheritance"

    second = {
        "model": "codex-auto",
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "修改一个按钮文案"}],
                "internal_chat_message_metadata_passthrough": {"turn_id": "turn-simple"},
            }
        ],
    }
    routed_second = asyncio.run(callback.async_pre_call_hook(None, None, second, "aresponses"))
    assert routed_second["model"] == "luna-medium"
    assert routed_second["metadata"]["codex_router"]["request_kind"] == "user_turn"


def test_short_follow_up_inherits_previous_user_complexity() -> None:
    request = {
        "input": [
            {
                "role": "user",
                "content": "参考 UI界面设计和功能设计，重新设计整个菜单并给完整方案",
            },
            {
                "role": "user",
                "content": "好的，按你的方案执行",
            },
        ],
    }

    decision = analyze_request(request).to_dict()

    assert decision["tier"] == "REASONING"
    assert decision["routing_cause"] == "context_inheritance"
    assert decision["signals"] == ["context_inheritance", "deep_reasoning"]


def test_manual_prefix_is_stripped_before_provider_call(monkeypatch: pytest.MonkeyPatch) -> None:
    callback = _load_runtime_callback(monkeypatch)
    request = {
        "model": "codex-auto",
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "[sol] 修改一个文案"}],
                "internal_chat_message_metadata_passthrough": {"turn_id": "manual-sol"},
            }
        ],
    }
    routed = asyncio.run(callback.async_pre_call_hook(None, None, request, "aresponses"))
    assert routed["model"] == "reasoning-medium"
    assert extract_last_user_text(routed) == "修改一个文案"
    assert routed["metadata"]["codex_router"]["routing_mode"] == "manual"


def test_explicit_complex_reasoning_phrase_routes_to_sol_xhigh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback = _load_runtime_callback(monkeypatch)
    request = {
        "model": "codex-auto",
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "这是复杂推理，请分析这个跨服务故障",
                    }
                ],
                "internal_chat_message_metadata_passthrough": {"turn_id": "explicit-xhigh"},
            }
        ],
    }
    routed = asyncio.run(callback.async_pre_call_hook(None, None, request, "aresponses"))
    assert routed["model"] == "sol-xhigh"
    assert routed["reasoning_effort"] == "xhigh"
    route = routed["metadata"]["codex_router"]
    assert route["tier"] == "REASONING"
    assert route["routing_cause"] == "explicit_complex_reasoning"
    assert route["reasoning_effort"] == "xhigh"


def test_audit_route_can_be_recovered_from_original_request(monkeypatch: pytest.MonkeyPatch) -> None:
    callback = _load_runtime_callback(monkeypatch)
    request = {
        "model": "codex-auto",
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "全面分析整个项目并给出完整方案"}],
                "internal_chat_message_metadata_passthrough": {"turn_id": "audit-turn"},
            }
        ],
    }
    routed = asyncio.run(callback.async_pre_call_hook(None, None, request, "aresponses"))

    recovered = callback.audit_route_for(request)

    assert recovered == routed["metadata"]["codex_router"]
    assert recovered["audit_schema_version"] == 2
    assert recovered["selected_alias"] == "sol-medium"
    assert recovered["request_kind"] == "user_turn"


def test_audit_route_can_be_recovered_when_litellm_drops_turn_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback = _load_runtime_callback(monkeypatch)
    request = {
        "model": "codex-auto",
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "[luna] Return exactly OK"}],
                "internal_chat_message_metadata_passthrough": {"turn_id": "dropped-turn"},
            }
        ],
    }
    routed = asyncio.run(callback.async_pre_call_hook(None, None, request, "aresponses"))
    callback_body = {
        "model": "luna-medium",
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "Return exactly OK"}],
            }
        ],
    }

    recovered = callback.audit_route_for(callback_body)

    assert recovered == routed["metadata"]["codex_router"]
    assert recovered["logical_turn_id"] is not None
    assert recovered["routing_mode"] == "manual"


def test_audit_route_can_be_recovered_by_litellm_call_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback = _load_runtime_callback(monkeypatch)
    request = {
        "model": "codex-auto",
        "litellm_call_id": "litellm-call-123",
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "修改一个按钮文案"}],
                "internal_chat_message_metadata_passthrough": {"turn_id": "call-id-turn"},
            }
        ],
    }
    routed = asyncio.run(callback.async_pre_call_hook(None, None, request, "aresponses"))

    recovered = callback.audit_route_for(
        {"model": "luna-medium", "input": []},
        call_id="litellm-call-123",
    )

    assert recovered == routed["metadata"]["codex_router"]
    assert recovered["selected_alias"] == "luna-medium"


def test_route_metadata_is_attached_to_litellm_logging_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback = _load_runtime_callback(monkeypatch)
    logging_obj = types.SimpleNamespace(model_call_details={"litellm_params": {}})
    request = {
        "model": "codex-auto",
        "litellm_logging_obj": logging_obj,
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "修改一个按钮文案"}],
                "internal_chat_message_metadata_passthrough": {"turn_id": "logger-turn"},
            }
        ],
    }

    routed = asyncio.run(callback.async_pre_call_hook(None, None, request, "aresponses"))
    expected = routed["metadata"]["codex_router"]

    assert logging_obj.model_call_details["metadata"]["codex_router"] == expected
    assert (
        logging_obj.model_call_details["litellm_params"]["metadata"]["codex_router"]
        == expected
    )


def test_no_user_and_no_lineage_uses_explicit_safe_default(monkeypatch: pytest.MonkeyPatch) -> None:
    callback = _load_runtime_callback(monkeypatch)
    request = {
        "model": "codex-auto",
        "input": [{"type": "function_call_output", "call_id": "call-2", "output": "done"}],
    }
    routed = asyncio.run(callback.async_pre_call_hook(None, None, request, "aresponses"))
    route = routed["metadata"]["codex_router"]
    assert routed["model"] == "terra-medium"
    assert route["default_reason"] == "no_user_no_lineage"
