from __future__ import annotations

import asyncio
import base64
import json
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

from codexthrift.content_capture import decrypt_payload
from codexthrift.config_templates import (
    AUDIT_CALLBACK_SOURCE,
    RUNTIME_INSTALL_SOURCE,
    MODEL_PROBE_SOURCE,
    OAUTH_PROBE_SOURCE,
    PROFILES,
    RUNTIME_INSTALL_SOURCE,
    build_config,
    profile_probe_models,
    render_phase21_yaml,
)
from codexthrift.runtime_templates import ROUTING_CALLBACK_SOURCE
from codexthrift import routing_policy


def test_runtime_installer_uses_symlinks_on_posix() -> None:
    assert 'symlinks=os.name != "nt"' in RUNTIME_INSTALL_SOURCE


def test_runtime_installer_accepts_multiple_pins_and_checks_proxy_imports() -> None:
    assert "version_specs = sys.argv[3:]" in RUNTIME_INSTALL_SOURCE
    assert '"pip", "install", *version_specs' in RUNTIME_INSTALL_SOURCE
    assert "from fastapi.dependencies.utils import get_flat_dependant" in RUNTIME_INSTALL_SOURCE
    assert "import litellm.proxy.proxy_server" in RUNTIME_INSTALL_SOURCE


def _model_map(config: dict) -> dict[str, dict]:
    return {item["model_name"]: item for item in config["model_list"]}


def _audit_namespace(monkeypatch) -> dict:
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
    namespace = {}
    exec(compile(AUDIT_CALLBACK_SOURCE, "audit_callback_candidate.py", "exec"), namespace)
    return namespace


def test_phase2_keeps_all_efforts_medium() -> None:
    config = build_config("phase2", "gpt56")
    models = _model_map(config)
    assert models["luna-medium"]["litellm_params"]["reasoning_effort"] == "medium"
    assert models["terra-medium"]["litellm_params"]["reasoning_effort"] == "medium"
    assert models["sol-medium"]["litellm_params"]["reasoning_effort"] == "medium"
    tiers = models["codex-auto"]["litellm_params"]["complexity_router_config"]["tiers"]
    assert tiers == {
        "SIMPLE": "luna-medium",
        "MEDIUM": "terra-medium",
        "COMPLEX": "sol-medium",
        "REASONING": "reasoning-medium",
    }
    assert "codex-task-auto" not in models
    assert models["gpt-5.6-luna"]["litellm_params"]["model"] == "chatgpt/gpt-5.6-luna"
    assert models["gpt-5.6-terra"]["litellm_params"] == {
        "model": "chatgpt/gpt-5.6-terra",
        "reasoning_effort": "medium",
    }
    assert models["gpt-5.6-sol"]["litellm_params"] == {
        "model": "chatgpt/gpt-5.6-sol",
        "reasoning_effort": "medium",
    }
    assert models["codex-auto-review"]["litellm_params"]["model"] == "chatgpt/gpt-5.6-sol"


def test_phase3_effort_and_task_escalation_aliases() -> None:
    config = build_config("phase3", "gpt56")
    models = _model_map(config)
    assert models["luna-low"]["litellm_params"]["reasoning_effort"] == "low"
    assert models["terra-medium"]["litellm_params"]["reasoning_effort"] == "medium"
    assert models["sol-high"]["litellm_params"]["reasoning_effort"] == "high"
    assert models["sol-xhigh"]["litellm_params"]["reasoning_effort"] == "xhigh"
    assert models["gpt-5.6-terra"]["litellm_params"]["reasoning_effort"] == "medium"
    assert models["gpt-5.6-sol"]["litellm_params"]["reasoning_effort"] == "high"
    task_tiers = models["codex-task-auto"]["litellm_params"]["complexity_router_config"]["tiers"]
    assert task_tiers["REASONING"] == "sol-high"


def test_phase3a_direct_model_aliases_preserve_phase_effort_policy() -> None:
    models = _model_map(build_config("phase3a", "gpt56"))

    assert models["gpt-5.6-luna"]["litellm_params"]["reasoning_effort"] == "low"
    assert models["gpt-5.6-terra"]["litellm_params"]["reasoning_effort"] == "medium"
    assert models["gpt-5.6-sol"]["litellm_params"]["reasoning_effort"] == "medium"


def test_phase_specific_probe_is_minimal_and_deduplicated() -> None:
    phase2 = profile_probe_models("gpt56", "phase2")
    assert len(phase2) == 4
    assert {item["effort"] for item in phase2} == {"medium", "xhigh"}
    sol = next(item for item in phase2 if item["model"].endswith("sol"))
    assert set(sol["tiers"]) == {"COMPLEX", "REASONING"}
    explicit = next(item for item in phase2 if item["effort"] == "xhigh")
    assert explicit["tiers"] == ["EXPLICIT_XHIGH"]

    phase3 = profile_probe_models("gpt56", "phase3")
    assert len(phase3) == 4
    assert {item["effort"] for item in phase3} == {"low", "medium", "high", "xhigh"}


def test_generated_helper_sources_compile() -> None:
    compile(OAUTH_PROBE_SOURCE, "oauth_probe.py", "exec")
    compile(MODEL_PROBE_SOURCE, "model_probe.py", "exec")
    compile(RUNTIME_INSTALL_SOURCE, "install_runtime.py", "exec")
    compile(ROUTING_CALLBACK_SOURCE, "routing_callback_candidate.py", "exec")
    compile(AUDIT_CALLBACK_SOURCE, "audit_callback_candidate.py", "exec")


def test_audit_callback_keeps_route_in_its_own_pre_call_cache(
    monkeypatch,
) -> None:
    namespace = _audit_namespace(monkeypatch)
    audit_logger = namespace["audit_logger"]
    assert namespace["_first_not_none"](0, None) == 0
    route = {
        "audit_schema_version": 2,
        "logical_turn_id": "hashed-turn",
        "selected_alias": "luna-medium",
        "request_kind": "user_turn",
    }
    data = {
        "model": "luna-medium",
        "litellm_call_id": "call-1",
        "metadata": {"codex_router": route},
        "input": [{"role": "user", "content": "hello"}],
    }
    asyncio.run(audit_logger.async_pre_call_hook(None, None, data, "aresponses"))

    recovered = audit_logger._cached_route(
        {"model": "luna-medium", "input": []},
        {"litellm_call_id": "call-1", "litellm_params": {}},
    )

    assert recovered == route


def test_audit_callback_reconstructs_stream_text_and_function_calls(monkeypatch) -> None:
    namespace = _audit_namespace(monkeypatch)
    audit_logger = namespace["audit_logger"]
    request_data = {
        "litellm_call_id": "call-stream",
        "proxy_server_request": {
            "body": {"model": "codex-auto", "stream": True, "input": [{"role": "user", "content": "hello"}]}
        },
    }
    asyncio.run(audit_logger.async_pre_call_hook(None, None, request_data, "aresponses"))
    events = [
        {"type": "response.output_text.delta", "response_id": "resp-1", "item_id": "msg-1", "output_index": 0, "delta": "hello "},
        {"type": "response.output_text.delta", "response_id": "resp-1", "item_id": "msg-1", "output_index": 0, "delta": "world"},
        {"type": "response.output_item.added", "response_id": "resp-1", "output_index": 1, "item": {"id": "fc-1", "type": "function_call", "call_id": "call-tool-1", "name": "lookup"}},
        {"type": "response.function_call_arguments.delta", "response_id": "resp-1", "item_id": "fc-1", "call_id": "call-tool-1", "output_index": 1, "delta": '{"q":"'},
        {"type": "response.function_call_arguments.done", "response_id": "resp-1", "item_id": "fc-1", "call_id": "call-tool-1", "output_index": 1, "arguments": '{"q":"router"}'},
        {"type": "response.completed", "response": {"id": "resp-1", "status": "completed", "output": []}},
    ]
    for event in events:
        returned = asyncio.run(audit_logger.async_post_call_streaming_deployment_hook(request_data, event, "aresponses"))
        assert returned is event

    capture = audit_logger._capture_for(request_data, response_id="resp-1")
    aggregated = namespace["_aggregate_capture"]({"id": "resp-1", "status": "completed", "output": []}, capture)

    assert aggregated["aggregation_source"] == "stream_deltas"
    assert aggregated["output_semantics_source"] == "sse_delta_reconstruction"
    assert aggregated["output_text"] == "hello world"
    assert [item["type"] for item in aggregated["normalized_output_items"]] == ["message", "function_call"]
    assert aggregated["tool_calls"][0]["arguments"] == '{"q":"router"}'
    assert aggregated["output_breakdown"] == {
        "response_item_count": 2,
        "visible_message_count": 1,
        "reasoning_item_count": 0,
        "refusal_count": 0,
        "tool_call_count": 1,
        "other_item_count": 0,
    }
    assert len(capture["events"]) == len(events)
    assert namespace["_stream_capture_summary"](
        capture["events"],
        stream_requested=True,
        output_tokens=5,
    ) == {
        "stream_capture_state": "captured_partial",
        "stream_event_count": 6,
        "stream_first_sequence": 0,
        "stream_last_sequence": 5,
        "stream_gap_count": 0,
        "stream_text_delta_count": 2,
        "stream_output_item_added_count": 1,
        "stream_output_item_done_count": 0,
        "stream_function_argument_delta_count": 1,
        "stream_response_completed_count": 1,
        "stream_has_terminal_event": 1,
        "stream_capture_complete": 0,
    }


def test_audit_callback_marks_completed_response_with_missing_output(monkeypatch) -> None:
    namespace = _audit_namespace(monkeypatch)
    audit_logger = namespace["audit_logger"]
    records: list[dict] = []
    payload_refs: list[str] = []
    namespace["_write"] = records.append
    namespace["_write_payload"] = lambda payload_ref, body, response_obj, capture: payload_refs.append(payload_ref)

    class FakeResponse:
        id = "resp-missing"
        model = "gpt-5.6-sol"
        status = "completed"
        usage = {"input_tokens": 100, "output_tokens": 12}
        _hidden_params = {}

        def model_dump(self):
            return {"id": self.id, "model": self.model, "status": self.status, "output": [], "usage": self.usage}

    body = {"model": "codex-auto", "stream": True, "input": [{"role": "user", "content": "hello"}]}
    kwargs = {
        "litellm_call_id": "call-missing",
        "litellm_params": {"proxy_server_request": {"body": body}},
        "model": "chatgpt/gpt-5.6-sol",
    }
    asyncio.run(audit_logger.async_pre_call_hook(None, None, {"litellm_call_id": "call-missing", **body}, "aresponses"))
    start = datetime.now(timezone.utc)
    asyncio.run(audit_logger.async_log_success_event(kwargs, FakeResponse(), start, start + timedelta(seconds=1)))
    asyncio.run(audit_logger.async_log_success_event(kwargs, FakeResponse(), start, start + timedelta(seconds=1)))

    assert len(records) == 1
    assert payload_refs == [records[0]["payload_ref"]]
    assert records[0]["status"] == "success"
    assert records[0]["provider_status"] == "completed"
    assert records[0]["display_status"] == "completed_output_missing"
    assert records[0]["stream_capture_state"] == "capture_missing"
    assert records[0]["output_item_count"] == 0
    assert records[0]["terminal_output_item_count"] == 0
    assert records[0]["output_semantics_version"] == 5
    assert records[0]["output_semantics_source"] == "unknown"
    assert records[0]["response_item_count"] == 0
    assert records[0]["call_kind"] == "unknown"


def test_audit_callback_stream_summary_distinguishes_complete_and_corrupt(monkeypatch) -> None:
    namespace = _audit_namespace(monkeypatch)
    stream_summary = namespace["_stream_capture_summary"]

    complete = stream_summary(
        [
            {
                "_routerllm_sequence": 0,
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {"id": "msg-1", "type": "message"},
            },
            {
                "_routerllm_sequence": 1,
                "type": "response.completed",
                "response": {"status": "completed", "output": []},
            },
        ],
        stream_requested=True,
        output_tokens=3,
    )
    corrupt = stream_summary(
        [
            {"_routerllm_sequence": 0, "type": "response.output_text.delta", "delta": "one"},
            {"_routerllm_sequence": 2, "type": "response.completed", "response": {"status": "completed"}},
        ],
        stream_requested=True,
        output_tokens=1,
    )

    assert complete["stream_capture_state"] == "captured_complete"
    assert complete["stream_capture_complete"] == 1
    assert complete["stream_response_completed_count"] == 1
    assert complete["stream_output_item_done_count"] == 1
    assert corrupt["stream_capture_state"] == "captured_corrupt"
    assert corrupt["stream_gap_count"] == 1
    assert corrupt["stream_capture_complete"] == 0
    assert namespace["_display_status"](
        callback_status="success",
        provider_status="completed",
        breakdown={
            "visible_message_count": 0,
            "refusal_count": 0,
            "tool_call_count": 0,
            "reasoning_item_count": 1,
        },
        output_tokens=20,
    ) == "completed_internal_only"


def test_audit_callback_identifies_guardian_review_without_capturing_content(monkeypatch) -> None:
    namespace = _audit_namespace(monkeypatch)

    identity = namespace["_call_identity"](
        body={"model": "codex-auto-review", "metadata": {"thread_source": "subagent"}},
        route={"request_kind": "review", "parent_turn_id": "parent-turn"},
        metadata={},
        headers={"X-OpenAI-Subagent": "guardian"},
        request_kind="review",
    )

    assert identity == {
        "call_kind": "guardian_review",
        "subagent_kind": "guardian",
        "thread_source": "subagent",
        "parent_thread_id": None,
        "parent_turn_id": "parent-turn",
        "internal_call": True,
        "reviewed_action_type": None,
    }


def test_audit_callback_reads_nested_turn_metadata_and_handoff_kind(monkeypatch) -> None:
    namespace = _audit_namespace(monkeypatch)

    identity = namespace["_call_identity"](
        body={
            "model": "codex-auto",
            "metadata": {
                "x-codex-turn-metadata": json.dumps({
                    "thread_source": "handoff",
                    "parent_thread_id": "thread-parent",
                    "parent_turn_id": "turn-parent",
                }),
            },
        },
        route={},
        metadata={},
        headers={},
        request_kind="handoff",
    )

    assert identity == {
        "call_kind": "human_handoff",
        "subagent_kind": None,
        "thread_source": "handoff",
        "parent_thread_id": "thread-parent",
        "parent_turn_id": "turn-parent",
        "internal_call": None,
        "reviewed_action_type": None,
    }


def test_audit_callback_writes_deduplicated_payload_v4(tmp_path: Path, monkeypatch) -> None:
    namespace = _audit_namespace(monkeypatch)
    key = b"v" * 32
    payload_path = tmp_path / "payloads.jsonl.enc"
    namespace["PAYLOAD_KEY"] = base64.urlsafe_b64encode(key).decode("ascii")
    namespace["PAYLOAD_PATH"] = payload_path

    namespace["_write_payload"](
        "payload-v4",
        {"model": "codex-auto", "input": [{"role": "user", "content": "hello"}]},
        {"status": "completed", "output": [{
            "type": "message",
            "content": [{"type": "output_text", "text": "world"}],
        }]},
        {"events": [], "done_items": {}, "text_deltas": {}, "function_calls": {}},
    )

    envelope = json.loads(payload_path.read_text(encoding="utf-8"))
    payload = json.loads(decrypt_payload(key, envelope))

    assert envelope["format"] == "aesgcm-zlib-json-v2"
    assert payload["payload_schema_version"] == 4
    assert payload["visible_output_text"] == "world"
    assert payload["terminal_response"]["status"] == "completed"
    assert "response" not in payload
    assert "output_text" not in payload


def test_audit_callback_conservatively_reduces_guardian_history_before_send(monkeypatch) -> None:
    namespace = _audit_namespace(monkeypatch)
    audit_logger = namespace["audit_logger"]
    protected_tail = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"current-{index}"}]}
        for index in range(12)
    ]
    original_input = [
        {"type": "reasoning", "encrypted_content": "old-reasoning"},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "duplicate"}]},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "duplicate"}]},
        *protected_tail,
    ]
    request_data = {
        "litellm_call_id": "guardian-reduction",
        "proxy_server_request": {
            "headers": {"X-OpenAI-Subagent": "guardian"},
            "body": {"model": "codex-auto-review", "input": original_input},
        },
    }

    asyncio.run(audit_logger.async_pre_call_hook(None, None, request_data, "aresponses"))

    reduced_input = request_data["proxy_server_request"]["body"]["input"]
    capture = audit_logger._capture_for(request_data)
    reduction = capture["guardian_context_reduction"]
    assert reduced_input == [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "duplicate"}]}, *protected_tail]
    assert capture["original_request_body"]["input"] == original_input
    assert reduction["original_item_count"] == 15
    assert reduction["reduced_item_count"] == 13
    assert reduction["dropped_reasoning_count"] == 1
    assert reduction["dropped_duplicate_count"] == 1
    assert reduction["protected_tail_item_count"] == 12
    assert reduction["policy"] == "guardian_conservative_v1"


def test_audit_payload_redaction_preserves_token_metrics(monkeypatch) -> None:
    namespace = _audit_namespace(monkeypatch)

    redacted = namespace["_redact_payload"]({
        "usage": {
            "input_tokens": 120,
            "output_tokens": 30,
            "completion_tokens": 30,
            "cached_tokens": 20,
        },
        "authorization": "Bearer private",
        "openai_api_key": "sk-private",
        "access_token": "oauth-private",
        "tokenizer": "o200k_base",
    })

    assert redacted["usage"] == {
        "input_tokens": 120,
        "output_tokens": 30,
        "completion_tokens": 30,
        "cached_tokens": 20,
    }
    assert redacted["authorization"] == "***REDACTED***"
    assert redacted["openai_api_key"] == "***REDACTED***"
    assert redacted["access_token"] == "***REDACTED***"
    assert redacted["tokenizer"] == "o200k_base"


def test_phase21_candidate_adds_pre_route_policy_without_changing_efforts() -> None:
    config = build_config("phase2", "gpt56", phase21_policy=True)
    models = _model_map(config)
    assert models["luna-medium"]["litellm_params"]["reasoning_effort"] == "medium"
    assert models["terra-medium"]["litellm_params"]["reasoning_effort"] == "medium"
    assert models["sol-medium"]["litellm_params"]["reasoning_effort"] == "medium"
    assert models["sol-xhigh"]["litellm_params"] == {
        "model": "chatgpt/gpt-5.6-sol",
        "reasoning_effort": "xhigh",
    }
    assert config["litellm_settings"]["callbacks"] == [
        "routing_callback_candidate.route_controller",
        "audit_callback_candidate.audit_logger",
    ]
    rendered = render_phase21_yaml("gpt56")
    assert "phase=phase2.1" in rendered
    assert "os.environ/LITELLM_MASTER_KEY" in rendered


def test_gpt56_profile_is_strict_and_callback_shape_is_valid() -> None:
    config = build_config("phase3", "gpt56")
    rendered_models = {
        item["litellm_params"].get("model")
        for item in config["model_list"]
        if item["litellm_params"].get("model") != "auto_router/complexity_router"
    }
    assert rendered_models == {
        "chatgpt/gpt-5.6-luna",
        "chatgpt/gpt-5.6-terra",
        "chatgpt/gpt-5.6-sol",
    }
    assert config["litellm_settings"]["callbacks"] == ["audit_callback.audit_logger"]
    combined = repr(config)
    assert "gpt-5.3" not in combined
    assert "gpt-5.4" not in combined


def test_oauth_bootstrap_targets_gpt56_terra() -> None:
    assert PROFILES["gpt56"].oauth_bootstrap == "chatgpt/gpt-5.6-terra"
    assert "gpt-5.3" not in repr(PROFILES)
    assert "gpt-5.4" not in repr(PROFILES)
    compile(OAUTH_PROBE_SOURCE, "oauth_probe.py", "exec")


def test_chatgpt_response_probes_send_list_input() -> None:
    for source in (OAUTH_PROBE_SOURCE, MODEL_PROBE_SOURCE):
        assert '"role": "user"' in source
        assert '"type": "input_text"' in source
        assert 'input="Return exactly' not in source
