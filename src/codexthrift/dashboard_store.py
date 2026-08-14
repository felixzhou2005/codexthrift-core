from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .billing import (
    ESTIMATOR_VERSION,
    PRICING_BASIS,
    PRICING_CATALOG_VERSION,
    STANDARD_API_PRICING,
    BillingMode,
    ContextClass,
    EstimateStatus,
    PricingRates,
    UsageBreakdown,
    actual_incremental_cost,
    calculate_api_equivalent_cost,
    resolve_billing_model,
    sol_equivalent_savings,
)
from .content_capture import decrypt_payload
from .payload_archive import read_payload_envelope, scan_payload_archive
from .security.redaction import redact_sensitive_value

SCHEMA_VERSION = 16

DEFAULT_OPENAI_PRICING_RULES = (
    ("gpt-5.6-sol", 5_000_000, 500_000, 30_000_000),
    ("gpt-5.6", 5_000_000, 500_000, 30_000_000),
    ("gpt-5.6-terra", 2_000_000, 200_000, 12_000_000),
    ("gpt-5.6-luna", 200_000, 20_000, 1_200_000),
)

_BILLING_WARNING_CODES = {
    "Historical audit data does not contain every v2 billing signal.": "historical_v2_signals_incomplete",
    "At least one paid tool, storage, or container charge is unavailable.": "paid_components_unavailable",
    "Token usage is incomplete.": "token_usage_incomplete",
    "Token counts cannot be negative.": "token_usage_negative",
    "Cache token counts exceed total input tokens.": "cache_tokens_exceed_input",
    "No Standard API pricing rule matched the billing model.": "pricing_rule_unmatched",
}
DEFAULT_OPENAI_PRICING_VERSION = "openai-standard-2026-08-11"

_TASK_KEY_SQL = """CASE
    WHEN logical_turn_id IS NOT NULL AND TRIM(logical_turn_id) <> ''
    THEN 'logical:' || logical_turn_id
    ELSE 'standalone:' || source_hash
END"""
_TASK_KIND_SQL = """CASE
    WHEN logical_turn_id IS NOT NULL AND TRIM(logical_turn_id) <> ''
    THEN 'logical_turn'
    ELSE 'standalone_call'
END"""
_MODEL_SQL = """COALESCE(
    NULLIF(TRIM(selected_alias), ''),
    NULLIF(TRIM(selected_model), ''),
    NULLIF(TRIM(provider_model), ''),
    'unknown'
)"""
_COMPLEXITY_SQL = """COALESCE(
    NULLIF(TRIM(selected_tier), ''),
    NULLIF(TRIM(reasoning_effort), ''),
    'unreported'
)"""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _integrity_check_enabled() -> bool:
    return os.environ.get("CODEXTHRIFT_INTEGRITY_CHECK_ENABLED", "1").lower() not in {
        "0",
        "false",
        "no",
    }


_RUNTIME_INPUT_ORIGINS = {"runtime_environment", "handoff", "compaction", "runtime"}
_INPUT_ORIGINS = {
    "human",
    "runtime_environment",
    "handoff",
    "compaction",
    "developer",
    "assistant",
    "reasoning",
    "function_call",
    "function_call_output",
    "unknown",
}


def _input_text(item: Mapping[str, Any]) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict):
            text = part.get("text") or part.get("input_text") or part.get("output_text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def _classify_input_item(item: Any, index: int) -> dict[str, Any]:
    if isinstance(item, str):
        raw_item: dict[str, Any] = {"role": "user", "content": item}
    elif isinstance(item, dict):
        raw_item = dict(item)
    else:
        raw_item = {"content": item}

    role = raw_item.get("role")
    item_type = raw_item.get("type")
    explicit_origin = raw_item.get("origin")
    text = _input_text(raw_item).lstrip()
    normalized_text = text.lower()

    if isinstance(explicit_origin, str) and explicit_origin in _INPUT_ORIGINS:
        origin = explicit_origin
        confidence = 1.0
    elif item_type == "reasoning":
        origin, confidence = "reasoning", 1.0
    elif item_type == "function_call":
        origin, confidence = "function_call", 1.0
    elif role == "tool" or item_type == "tool_result" or (
        isinstance(item_type, str) and item_type.endswith("_output")
    ):
        origin, confidence = "function_call_output", 1.0
    elif role in {"developer", "system", "instructions"}:
        origin, confidence = "developer", 1.0
    elif role == "assistant":
        origin, confidence = "assistant", 1.0
    elif role == "user":
        if normalized_text.startswith("<environment_context>"):
            origin, confidence = "runtime_environment", 1.0
        elif normalized_text.startswith("another language model started"):
            origin, confidence = "handoff", 1.0
        elif "context checkpoint compaction" in normalized_text or normalized_text.startswith("<compaction"):
            origin, confidence = "compaction", 1.0
        else:
            origin, confidence = "human", 0.95
    else:
        origin, confidence = "unknown", 0.5

    if item_type in {"reasoning", "function_call", "function_call_output", "tool_result"}:
        category = str(item_type)
    elif role in {"user", "assistant", "developer", "system", "instructions"}:
        category = "message"
    else:
        category = str(item_type or "unknown")

    return {
        **raw_item,
        "input_index": index,
        "origin": origin,
        "origin_confidence": confidence,
        "category": category,
        "turn_group_id": raw_item.get("turn_group_id"),
        "sensitive": bool(raw_item.get("sensitive", False)),
    }


def _classified_request_input(request: Any) -> list[dict[str, Any]]:
    if isinstance(request, dict):
        input_value = request.get("input")
        if input_value is None:
            input_value = request.get("messages")
    else:
        input_value = request
    values = [] if input_value is None else input_value if isinstance(input_value, list) else [input_value]
    classified = [_classify_input_item(item, index) for index, item in enumerate(values)]
    if isinstance(request, dict) and request.get("instructions") not in (None, "", []):
        classified.insert(0, _classify_input_item({
            "role": "instructions",
            "content": request["instructions"],
        }, -1))
    return classified


def _matches_input_origin(item: Mapping[str, Any], origin: str) -> bool:
    item_origin = item.get("origin")
    if origin == "all":
        return True
    if origin == "runtime":
        return item_origin in _RUNTIME_INPUT_ORIGINS
    return item_origin == origin


def _stream_event_category(event: Any) -> str:
    if not isinstance(event, dict):
        return "unknown"
    event_type = str(event.get("type") or "")
    if event_type in {"response.created", "response.in_progress", "response.completed", "response.incomplete"}:
        return "lifecycle"
    if "error" in event_type or event_type == "response.failed":
        return "error"
    if "reasoning" in event_type:
        return "reasoning"
    if "output_text" in event_type or "refusal" in event_type:
        return "text_delta"
    if "function_call" in event_type:
        return "function_call"
    if "output_item" in event_type or "content_part" in event_type:
        return "output_item"
    return "unknown"


def _request_input_sections(request: Any) -> dict[str, Any]:
    user_messages: list[Any] = []
    context_messages: list[Any] = []
    tool_results: list[Any] = []
    request_metadata: dict[str, Any] = {}

    def classify(item: Any, index: int) -> None:
        classified = _classify_input_item(item, index)
        if isinstance(item, str):
            user_messages.append({"role": "user", "content": item})
            return
        if not isinstance(item, dict):
            context_messages.append(item)
            return
        if classified["origin"] == "human":
            user_messages.append(item)
        elif classified["origin"] == "function_call_output":
            tool_results.append(item)
        else:
            context_messages.append(item)

    if isinstance(request, dict):
        input_value = request.get("input")
        if input_value is None:
            input_value = request.get("messages")
        if isinstance(input_value, list):
            for index, item in enumerate(input_value):
                classify(item, index)
        elif input_value is not None:
            classify(input_value, 0)

        instructions = request.get("instructions")
        if instructions not in (None, "", []):
            context_messages.insert(0, {"role": "instructions", "content": instructions})

        for key in (
            "model",
            "stream",
            "previous_response_id",
            "conversation",
            "tool_choice",
            "service_tier",
            "store",
            "truncation",
            "max_output_tokens",
            "parallel_tool_calls",
            "background",
            "reasoning",
            "text",
        ):
            if key in request:
                request_metadata[key] = request[key]
        tools = request.get("tools")
        request_metadata["tool_definition_count"] = len(tools) if isinstance(tools, list) else 0
    elif request is not None:
        classify(request)

    request_metadata.update({
        "user_message_count": len(user_messages),
        "context_message_count": len(context_messages),
        "tool_result_count": len(tool_results),
    })
    return {
        "user_messages": user_messages,
        "context_messages": context_messages,
        "tool_results": tool_results,
        "request_metadata": request_metadata,
    }


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _usage_value(usage: Any, *keys: str) -> int | None:
    if not isinstance(usage, dict):
        return None
    for key in keys:
        value = _as_int(usage.get(key))
        if value is not None:
            return value
    return None


def _error_category(record: dict[str, Any]) -> str | None:
    if record.get("status") != "failure":
        return None
    text = " ".join(
        str(record.get(key) or "")
        for key in ("error_type", "error", "routing_cause")
    ).lower()
    if "oauth" in text or "auth" in text or "unauthor" in text:
        return "authentication"
    if "rate" in text or "quota" in text or "limit" in text:
        return "rate_limit"
    if "timeout" in text or "network" in text or "connection" in text:
        return "network"
    if "probe" in text or "local" in text:
        return "local_runtime"
    return "provider"


def _money_to_microusd(value: Any) -> int | None:
    if isinstance(value, dict):
        value = value.get("total_cost") if value.get("total_cost") is not None else value.get("cost")
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value)) * Decimal("1000000")
        return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return None


def _encode_cursor(offset: int) -> str:
    raw = json.dumps({"offset": offset}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        offset = payload["offset"]
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("Invalid task cursor.") from error
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError("Invalid task cursor.")
    return offset


def _task_status(success_count: int, failure_count: int) -> str:
    if success_count and failure_count:
        return "partial"
    if failure_count:
        return "failure"
    if success_count:
        return "success"
    return "unknown"


_OUTPUT_SEMANTICS_VERSION = 5
_OUTPUT_SEMANTICS_SOURCE_PRIORITY = {
    "unknown": 0,
    "historical_inference": 10,
    "terminal_response": 20,
    "sse_delta_reconstruction": 30,
    "sse_output_item_done": 40,
    "mixed": 50,
}


def _output_item_breakdown(output_items: list[Any]) -> dict[str, int]:
    visible_message_count = 0
    reasoning_item_count = 0
    refusal_count = 0
    tool_call_count = 0
    other_item_count = 0

    for item in output_items:
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
        "response_item_count": len(output_items),
        "visible_message_count": visible_message_count,
        "reasoning_item_count": reasoning_item_count,
        "refusal_count": refusal_count,
        "tool_call_count": tool_call_count,
        "other_item_count": other_item_count,
    }


def _derive_output_text(output_items: list[Any]) -> str:
    parts: list[str] = []
    for item in output_items:
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
    stream_events: Any,
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
    if not isinstance(stream_events, list) or not stream_events:
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

    for index, event in enumerate(stream_events):
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
            item_key = (
                item.get("id") if isinstance(item, dict) else None
            ) or event.get("item_id") or event.get("output_index")
            if item_key is not None:
                done_item_keys.add(str(item_key))

    sequence_gap_count = 0
    if not sequences or len(sequences) != len(stream_events):
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
        "stream_event_count": len(stream_events),
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


def _normalized_output_semantics_source(payload: dict[str, Any]) -> str:
    source = payload.get("output_semantics_source") or payload.get("aggregation_source")
    aliases = {
        "output_item_done": "sse_output_item_done",
        "stream_deltas": "sse_delta_reconstruction",
    }
    if isinstance(source, str):
        source = aliases.get(source, source)
        if source in _OUTPUT_SEMANTICS_SOURCE_PRIORITY:
            return source
    if int(payload.get("payload_schema_version") or 1) < 2:
        return "historical_inference"
    return "unknown"


def _payload_output_semantics(payload: dict[str, Any], *, callback_status: str, output_tokens: int | None) -> dict[str, Any]:
    terminal_response = payload.get("terminal_response") or payload.get("response") or {}
    provider_status = terminal_response.get("status") if isinstance(terminal_response, dict) else None
    terminal_output = terminal_response.get("output") if isinstance(terminal_response, dict) else None
    terminal_output_item_count = len(terminal_output) if isinstance(terminal_output, list) else 0
    output_items = payload.get("normalized_output_items")
    if not isinstance(output_items, list):
        output_items = terminal_output if isinstance(terminal_output, list) else []
    breakdown = _output_item_breakdown(output_items)
    request = payload.get("request")
    stream_requested = request.get("stream") if isinstance(request, dict) else None
    stream_summary = _stream_capture_summary(
        payload.get("stream_events"),
        stream_requested=stream_requested,
        output_tokens=output_tokens,
    )
    display_status = _display_status(
        callback_status=callback_status,
        provider_status=provider_status,
        breakdown=breakdown,
        output_tokens=output_tokens,
    )
    return {
        "provider_status": provider_status or ("failed" if callback_status == "failure" else "completed"),
        "stream_capture_state": stream_summary["stream_capture_state"],
        "display_status": display_status,
        "output_item_count": breakdown["response_item_count"],
        "terminal_output_item_count": terminal_output_item_count,
        "tool_call_count": breakdown["tool_call_count"],
        "output_semantics_version": _OUTPUT_SEMANTICS_VERSION,
        "output_semantics_source": _normalized_output_semantics_source(payload),
        "aggregation_source": _normalized_output_semantics_source(payload),
        **breakdown,
        **stream_summary,
    }


def _apply_cost_state(item: dict[str, Any]) -> dict[str, Any]:
    actual_count = int(item.get("actual_cost_call_count") or 0)
    estimated_count = int(item.get("estimated_cost_call_count") or 0)
    unpriced_count = int(item.get("unpriced_call_count") or 0)

    if actual_count and estimated_count:
        state = "mixed"
    elif actual_count:
        state = "actual" if not unpriced_count else "partial_actual"
    elif estimated_count:
        state = "estimated" if not unpriced_count else "partial_estimate"
    else:
        state = "unpriced"
    item["cost_state"] = state
    return item


class _DatabaseAccessCoordinator:
    """Coordinate ordinary SQLite connections with exclusive file replacement."""

    def __init__(self) -> None:
        self._condition = threading.Condition(threading.RLock())
        self._active_readers = 0
        self._waiting_writers = 0
        self._exclusive_owner: int | None = None
        self._exclusive_depth = 0

    @contextmanager
    def shared(self) -> Iterator[None]:
        thread_id = threading.get_ident()
        reentrant = False
        with self._condition:
            if self._exclusive_owner == thread_id:
                reentrant = True
            else:
                while self._exclusive_owner is not None or self._waiting_writers:
                    self._condition.wait()
                self._active_readers += 1
        try:
            yield
        finally:
            if not reentrant:
                with self._condition:
                    self._active_readers -= 1
                    if self._active_readers == 0:
                        self._condition.notify_all()

    @contextmanager
    def exclusive(self, *, timeout: float) -> Iterator[None]:
        if timeout <= 0:
            raise ValueError("Database maintenance timeout must be positive.")
        thread_id = threading.get_ident()
        deadline = time.monotonic() + timeout
        with self._condition:
            if self._exclusive_owner == thread_id:
                self._exclusive_depth += 1
            else:
                self._waiting_writers += 1
                try:
                    while self._exclusive_owner is not None or self._active_readers:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("Timed out waiting for active database connections to close.")
                        self._condition.wait(remaining)
                    self._exclusive_owner = thread_id
                    self._exclusive_depth = 1
                finally:
                    self._waiting_writers -= 1
                    if self._waiting_writers == 0 and self._exclusive_owner is None:
                        self._condition.notify_all()
        try:
            yield
        finally:
            with self._condition:
                if self._exclusive_owner != thread_id:
                    raise RuntimeError("Database maintenance ownership changed unexpectedly.")
                self._exclusive_depth -= 1
                if self._exclusive_depth == 0:
                    self._exclusive_owner = None
                    self._condition.notify_all()


class DashboardStore:
    """A redacted, query-oriented read model for RouterLLM desktop telemetry."""

    def __init__(
        self,
        database_path: Path,
        *,
        payload_path: Path | None = None,
        payload_key: bytes | None = None,
    ):
        self.database_path = database_path
        self.payload_path = payload_path
        self.payload_key = payload_key
        self._database_access = _DatabaseAccessCoordinator()
        self._initialize_lock = threading.Lock()
        self._initialized = False
        self._health_lock = threading.Lock()
        self._integrity_result = "checking"
        self._integrity_checked_at = 0.0
        self._integrity_thread: threading.Thread | None = None
        self._integrity_check_enabled = _integrity_check_enabled()

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            self._initialize_once()
            self._initialized = True

    def _initialize_once(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA secure_delete=ON")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS import_cursors (
                    source_path TEXT PRIMARY KEY,
                    source_size INTEGER NOT NULL,
                    source_mtime_ns INTEGER NOT NULL,
                    last_offset INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS api_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_hash TEXT NOT NULL UNIQUE,
                    occurred_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    provider_status TEXT,
                    stream_capture_state TEXT,
                    display_status TEXT,
                    output_item_count INTEGER,
                    terminal_output_item_count INTEGER,
                    tool_call_count INTEGER,
                    output_semantics_version INTEGER NOT NULL DEFAULT 0,
                    output_semantics_source TEXT,
                    aggregation_source TEXT,
                    stream_event_count INTEGER,
                    stream_first_sequence INTEGER,
                    stream_last_sequence INTEGER,
                    stream_gap_count INTEGER,
                    stream_text_delta_count INTEGER,
                    stream_output_item_added_count INTEGER,
                    stream_output_item_done_count INTEGER,
                    stream_function_argument_delta_count INTEGER,
                    stream_response_completed_count INTEGER,
                    stream_has_terminal_event INTEGER,
                    stream_capture_complete INTEGER,
                    response_item_count INTEGER,
                    visible_message_count INTEGER,
                    reasoning_item_count INTEGER,
                    refusal_count INTEGER,
                    other_item_count INTEGER,
                    logical_turn_id TEXT,
                    prompt_fingerprint TEXT,
                    request_kind TEXT,
                    call_kind TEXT,
                    subagent_kind TEXT,
                    thread_source TEXT,
                    parent_thread_id TEXT,
                    parent_turn_id TEXT,
                    internal_call INTEGER,
                    reviewed_action_type TEXT,
                    requested_alias TEXT,
                    selected_alias TEXT,
                    selected_model TEXT,
                    provider_model TEXT,
                    response_model TEXT,
                    selected_tier TEXT,
                    reasoning_effort TEXT,
                    routing_mode TEXT,
                    routing_cause TEXT,
                    route_score REAL,
                    duration_ms INTEGER,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    cache_read_tokens INTEGER,
                    cache_write_tokens INTEGER,
                    reasoning_tokens INTEGER,
                    error_category TEXT,
                    error_type TEXT,
                    policy_version TEXT,
                    payload_ref TEXT,
                    input_cost_microusd INTEGER,
                    cached_input_cost_microusd INTEGER,
                    output_cost_microusd INTEGER,
                    total_cost_microusd INTEGER,
                    reported_cost_microusd INTEGER,
                    currency TEXT,
                    pricing_rule_id INTEGER,
                    pricing_version TEXT,
                    cost_status TEXT NOT NULL DEFAULT 'unpriced',
                    billing_model TEXT,
                    pricing_match_source TEXT,
                    billing_mode TEXT NOT NULL DEFAULT 'unknown',
                    billing_mode_source TEXT,
                    observed_service_tier TEXT,
                    pricing_basis TEXT,
                    context_class TEXT,
                    regional_processing TEXT,
                    regular_input_cost_microusd INTEGER,
                    cache_write_cost_microusd INTEGER,
                    model_token_cost_microusd INTEGER,
                    builtin_tool_cost_microusd INTEGER,
                    storage_cost_microusd INTEGER,
                    container_cost_microusd INTEGER,
                    known_api_equivalent_cost_microusd INTEGER,
                    api_equivalent_cost_microusd INTEGER,
                    provider_reported_cost_microusd INTEGER,
                    actual_incremental_cost_microusd INTEGER,
                    api_equivalent_status TEXT,
                    actual_incremental_status TEXT,
                    pricing_rule_version_id INTEGER,
                    pricing_catalog_version TEXT,
                    estimator_version TEXT,
                    applied_rates_json TEXT,
                    billing_warnings_json TEXT,
                    sol_baseline_cost_microusd INTEGER,
                    sol_equivalent_savings_microusd INTEGER,
                    safe_metadata_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS api_calls_occurred_at_idx ON api_calls(occurred_at DESC);
                CREATE INDEX IF NOT EXISTS api_calls_turn_idx ON api_calls(logical_turn_id, occurred_at);
                CREATE INDEX IF NOT EXISTS api_calls_status_idx ON api_calls(status, occurred_at DESC);

                CREATE TABLE IF NOT EXISTS pricing_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model_pattern TEXT NOT NULL UNIQUE,
                    currency TEXT NOT NULL DEFAULT 'USD',
                    input_microusd_per_million INTEGER NOT NULL,
                    cached_input_microusd_per_million INTEGER,
                    cache_write_microusd_per_million INTEGER NOT NULL DEFAULT 0,
                    long_context_threshold INTEGER,
                    long_context_input_multiplier REAL NOT NULL DEFAULT 1,
                    long_context_output_multiplier REAL NOT NULL DEFAULT 1,
                    output_microusd_per_million INTEGER NOT NULL,
                    version TEXT NOT NULL,
                    effective_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pricing_rule_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    catalog_version TEXT NOT NULL,
                    model_pattern TEXT NOT NULL,
                    service_tier TEXT NOT NULL,
                    context_class TEXT NOT NULL,
                    regional_processing TEXT NOT NULL DEFAULT 'none',
                    currency TEXT NOT NULL DEFAULT 'USD',
                    input_microusd_per_million INTEGER NOT NULL,
                    cached_input_microusd_per_million INTEGER NOT NULL,
                    cache_write_microusd_per_million INTEGER NOT NULL,
                    output_microusd_per_million INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    effective_from TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(catalog_version, model_pattern, service_tier, context_class, regional_processing, source)
                );
                CREATE INDEX IF NOT EXISTS pricing_rule_versions_lookup_idx
                ON pricing_rule_versions(model_pattern, service_tier, context_class, effective_from DESC);

                CREATE TABLE IF NOT EXISTS call_cost_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    api_call_id INTEGER NOT NULL REFERENCES api_calls(id) ON DELETE CASCADE,
                    item_type TEXT NOT NULL,
                    quantity INTEGER,
                    unit TEXT NOT NULL,
                    rate_microusd INTEGER,
                    amount_microusd INTEGER,
                    pricing_status TEXT NOT NULL,
                    source TEXT NOT NULL,
                    UNIQUE(api_call_id, item_type)
                );

                CREATE TABLE IF NOT EXISTS call_payloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payload_ref TEXT NOT NULL UNIQUE,
                    api_call_id INTEGER NOT NULL UNIQUE REFERENCES api_calls(id) ON DELETE CASCADE,
                    nonce TEXT NOT NULL,
                    ciphertext TEXT NOT NULL,
                    captured_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS payload_archive_index (
                    payload_ref TEXT PRIMARY KEY,
                    source_path TEXT NOT NULL,
                    source_inode INTEGER NOT NULL,
                    byte_offset INTEGER NOT NULL,
                    byte_length INTEGER NOT NULL,
                    indexed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS payload_archive_source_idx
                ON payload_archive_index(source_path, byte_offset);

                CREATE TABLE IF NOT EXISTS payload_archive_cursors (
                    source_path TEXT PRIMARY KEY,
                    source_inode INTEGER NOT NULL,
                    source_size INTEGER NOT NULL,
                    source_mtime_ns INTEGER NOT NULL,
                    last_offset INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS storage_retention_buckets (
                    bucket_start TEXT PRIMARY KEY,
                    bucket_end TEXT NOT NULL,
                    call_count INTEGER NOT NULL DEFAULT 0,
                    embedded_payload_count INTEGER NOT NULL DEFAULT 0,
                    embedded_payload_bytes INTEGER NOT NULL DEFAULT 0,
                    archive_payload_count INTEGER NOT NULL DEFAULT 0,
                    archive_payload_bytes INTEGER NOT NULL DEFAULT 0,
                    first_call_id INTEGER,
                    last_call_id INTEGER,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS storage_retention_progress (
                    source TEXT PRIMARY KEY,
                    last_row_id INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS storage_retention_buckets_start_idx
                ON storage_retention_buckets(bucket_start);

                CREATE TABLE IF NOT EXISTS payload_retention_tombstones (
                    payload_ref TEXT PRIMARY KEY,
                    api_call_id INTEGER,
                    occurred_at TEXT,
                    deleted_at TEXT NOT NULL,
                    delete_reason TEXT NOT NULL,
                    retention_job_id TEXT NOT NULL,
                    original_storage TEXT NOT NULL,
                    original_bytes INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS payload_retention_deleted_at_idx
                ON payload_retention_tombstones(deleted_at DESC);

                CREATE TABLE IF NOT EXISTS call_retention_tombstones (
                    source_hash TEXT PRIMARY KEY,
                    occurred_at TEXT NOT NULL,
                    deleted_at TEXT NOT NULL,
                    delete_reason TEXT NOT NULL,
                    retention_job_id TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS call_retention_deleted_at_idx
                ON call_retention_tombstones(deleted_at DESC);

                CREATE TABLE IF NOT EXISTS service_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    component TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    request_id TEXT,
                    details_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS service_events_occurred_at_idx ON service_events(occurred_at DESC);

                CREATE TABLE IF NOT EXISTS account_profiles (
                    id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    auth_mode TEXT NOT NULL,
                    plan_type TEXT,
                    keychain_ref TEXT,
                    is_active INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'unknown',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS quota_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile_id TEXT,
                    observed_at TEXT NOT NULL,
                    source_status TEXT NOT NULL,
                    error_code TEXT,
                    error_message TEXT,
                    raw_summary_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS quota_snapshots_observed_idx ON quota_snapshots(observed_at DESC);

                CREATE TABLE IF NOT EXISTS quota_windows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_id INTEGER NOT NULL REFERENCES quota_snapshots(id) ON DELETE CASCADE,
                    window_key TEXT NOT NULL,
                    label TEXT NOT NULL,
                    remaining_percent REAL,
                    reset_at TEXT,
                    used_percent REAL,
                    limit_name TEXT,
                    UNIQUE(snapshot_id, window_key)
                );

                CREATE TABLE IF NOT EXISTS reset_redemptions (
                    idempotency_key TEXT PRIMARY KEY,
                    profile_id TEXT,
                    credit_id TEXT,
                    outcome TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    error_code TEXT
                );

                CREATE TABLE IF NOT EXISTS account_usage_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    quota_snapshot_id INTEGER NOT NULL REFERENCES quota_snapshots(id) ON DELETE CASCADE,
                    lifetime_tokens INTEGER,
                    peak_daily_tokens INTEGER,
                    current_streak_days INTEGER,
                    longest_streak_days INTEGER,
                    longest_running_turn_sec INTEGER
                );

                CREATE TABLE IF NOT EXISTS account_usage_daily (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    quota_snapshot_id INTEGER NOT NULL REFERENCES quota_snapshots(id) ON DELETE CASCADE,
                    start_date TEXT NOT NULL,
                    tokens INTEGER NOT NULL,
                    UNIQUE(quota_snapshot_id, start_date)
                );

                CREATE TABLE IF NOT EXISTS quota_reset_credits (
                    quota_snapshot_id INTEGER PRIMARY KEY REFERENCES quota_snapshots(id) ON DELETE CASCADE,
                    available_count INTEGER,
                    observed_at TEXT NOT NULL,
                    source TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS quota_reset_credit_details (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    quota_snapshot_id INTEGER NOT NULL REFERENCES quota_snapshots(id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    status TEXT,
                    reset_type TEXT,
                    granted_at TEXT,
                    expires_at TEXT,
                    title TEXT,
                    description TEXT,
                    UNIQUE(quota_snapshot_id, ordinal)
                );
                """
            )
            self._ensure_columns(
                connection,
                "api_calls",
                {
                    "provider_status": "TEXT",
                    "stream_capture_state": "TEXT",
                    "display_status": "TEXT",
                    "output_item_count": "INTEGER",
                    "terminal_output_item_count": "INTEGER",
                    "tool_call_count": "INTEGER",
                    "output_semantics_version": "INTEGER NOT NULL DEFAULT 0",
                    "output_semantics_source": "TEXT",
                    "aggregation_source": "TEXT",
                    "stream_event_count": "INTEGER",
                    "stream_first_sequence": "INTEGER",
                    "stream_last_sequence": "INTEGER",
                    "stream_gap_count": "INTEGER",
                    "stream_text_delta_count": "INTEGER",
                    "stream_output_item_added_count": "INTEGER",
                    "stream_output_item_done_count": "INTEGER",
                    "stream_function_argument_delta_count": "INTEGER",
                    "stream_response_completed_count": "INTEGER",
                    "stream_has_terminal_event": "INTEGER",
                    "stream_capture_complete": "INTEGER",
                    "response_item_count": "INTEGER",
                    "visible_message_count": "INTEGER",
                    "reasoning_item_count": "INTEGER",
                    "refusal_count": "INTEGER",
                    "other_item_count": "INTEGER",
                    "call_kind": "TEXT",
                    "subagent_kind": "TEXT",
                    "thread_source": "TEXT",
                    "parent_thread_id": "TEXT",
                    "parent_turn_id": "TEXT",
                    "internal_call": "INTEGER",
                    "reviewed_action_type": "TEXT",
                    "payload_ref": "TEXT",
                    "input_cost_microusd": "INTEGER",
                    "cached_input_cost_microusd": "INTEGER",
                    "output_cost_microusd": "INTEGER",
                    "total_cost_microusd": "INTEGER",
                    "reported_cost_microusd": "INTEGER",
                    "currency": "TEXT",
                    "pricing_rule_id": "INTEGER",
                    "pricing_version": "TEXT",
                    "cost_status": "TEXT NOT NULL DEFAULT 'unpriced'",
                    "cache_write_tokens": "INTEGER",
                    "reasoning_tokens": "INTEGER",
                    "billing_model": "TEXT",
                    "pricing_match_source": "TEXT",
                    "billing_mode": "TEXT NOT NULL DEFAULT 'unknown'",
                    "billing_mode_source": "TEXT",
                    "observed_service_tier": "TEXT",
                    "pricing_basis": "TEXT",
                    "context_class": "TEXT",
                    "regional_processing": "TEXT",
                    "regular_input_cost_microusd": "INTEGER",
                    "cache_write_cost_microusd": "INTEGER",
                    "model_token_cost_microusd": "INTEGER",
                    "builtin_tool_cost_microusd": "INTEGER",
                    "storage_cost_microusd": "INTEGER",
                    "container_cost_microusd": "INTEGER",
                    "known_api_equivalent_cost_microusd": "INTEGER",
                    "api_equivalent_cost_microusd": "INTEGER",
                    "provider_reported_cost_microusd": "INTEGER",
                    "actual_incremental_cost_microusd": "INTEGER",
                    "api_equivalent_status": "TEXT",
                    "actual_incremental_status": "TEXT",
                    "pricing_rule_version_id": "INTEGER",
                    "pricing_catalog_version": "TEXT",
                    "estimator_version": "TEXT",
                    "applied_rates_json": "TEXT",
                    "billing_warnings_json": "TEXT",
                    "sol_baseline_cost_microusd": "INTEGER",
                    "sol_equivalent_savings_microusd": "INTEGER",
                },
            )
            self._ensure_columns(connection, "pricing_rules", {
                "cached_input_microusd_per_million": "INTEGER",
                "cache_write_microusd_per_million": "INTEGER NOT NULL DEFAULT 0",
                "long_context_threshold": "INTEGER",
                "long_context_input_multiplier": "REAL NOT NULL DEFAULT 1",
                "long_context_output_multiplier": "REAL NOT NULL DEFAULT 1",
            })
            connection.execute("CREATE INDEX IF NOT EXISTS api_calls_payload_ref_idx ON api_calls(payload_ref)")
            connection.execute("CREATE INDEX IF NOT EXISTS api_calls_display_status_idx ON api_calls(display_status, occurred_at DESC)")
            connection.execute("CREATE INDEX IF NOT EXISTS api_calls_kind_idx ON api_calls(call_kind, occurred_at DESC)")
            self._ensure_columns(
                connection,
                "account_profiles",
                {
                    "account_type": "TEXT",
                    "email_masked": "TEXT",
                    "email_fingerprint": "TEXT",
                    "requires_openai_auth": "INTEGER",
                    "last_verified_at": "TEXT",
                    "last_error_code": "TEXT",
                    "last_error_message": "TEXT",
                    "source": "TEXT",
                    "account_id_fingerprint": "TEXT",
                    "email_verified": "INTEGER",
                    "identity_expires_at": "TEXT",
                },
            )
            self._ensure_columns(
                connection,
                "quota_windows",
                {
                    "limit_id": "TEXT",
                    "window_kind": "TEXT",
                    "window_duration_mins": "INTEGER",
                },
            )
            self._ensure_columns(
                connection,
                "quota_reset_credits",
                {
                    "count_source": "TEXT",
                    "detail_lower_bound": "INTEGER",
                    "details_state": "TEXT",
                    "returned_detail_count": "INTEGER",
                    "missing_detail_count": "INTEGER",
                    "contract_warning": "TEXT",
                },
            )
            has_raw_credit_ids = connection.execute(
                "SELECT 1 FROM reset_redemptions WHERE credit_id IS NOT NULL LIMIT 1"
            ).fetchone() is not None
            if has_raw_credit_ids:
                connection.execute("UPDATE reset_redemptions SET credit_id=NULL WHERE credit_id IS NOT NULL")
                connection.commit()
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute("VACUUM")
                connection.execute("PRAGMA journal_mode=WAL")
            cursor = connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, _utc_now()),
            )
            connection.executemany(
                """INSERT OR IGNORE INTO pricing_rules(
                       model_pattern, currency, input_microusd_per_million,
                       cached_input_microusd_per_million, output_microusd_per_million, version, effective_at
                   ) VALUES (?, 'USD', ?, ?, ?, ?, '1970-01-01T00:00:00+00:00')""",
                [(*rule, DEFAULT_OPENAI_PRICING_VERSION) for rule in DEFAULT_OPENAI_PRICING_RULES],
            )
            self._seed_pricing_rule_versions(connection)
            self._backfill_billing_v2(connection)

    @staticmethod
    def _seed_pricing_rule_versions(connection: sqlite3.Connection) -> None:
        created_at = _utc_now()
        for (model, context_class), rates in STANDARD_API_PRICING.items():
            connection.execute(
                """INSERT OR IGNORE INTO pricing_rule_versions(
                       catalog_version, model_pattern, service_tier, context_class,
                       regional_processing, currency, input_microusd_per_million,
                       cached_input_microusd_per_million, cache_write_microusd_per_million,
                       output_microusd_per_million, source, effective_from, created_at
                   ) VALUES (?, ?, 'standard', ?, 'none', 'USD', ?, ?, ?, ?, 'builtin', ?, ?)""",
                (
                    PRICING_CATALOG_VERSION,
                    model,
                    context_class.value,
                    rates.input_microusd_per_million,
                    rates.cached_input_microusd_per_million,
                    rates.cache_write_microusd_per_million,
                    rates.output_microusd_per_million,
                    "2026-08-11T00:00:00+00:00",
                    created_at,
                ),
            )

        legacy_rows = connection.execute(
            "SELECT * FROM pricing_rules WHERE version LIKE 'local-%'"
        ).fetchall()
        for row in legacy_rows:
            input_rate = int(row["input_microusd_per_million"])
            cached_rate = int(row["cached_input_microusd_per_million"] or round(input_rate * 0.1))
            cache_write_rate = int(row["cache_write_microusd_per_million"] or round(input_rate * 1.25))
            output_rate = int(row["output_microusd_per_million"])
            threshold = int(row["long_context_threshold"] or 272_000)
            input_multiplier = Decimal(str(row["long_context_input_multiplier"] or 2))
            output_multiplier = Decimal(str(row["long_context_output_multiplier"] or 1.5))
            for context_class, multiplier_in, multiplier_out in (
                (ContextClass.SHORT, Decimal(1), Decimal(1)),
                (ContextClass.LONG, input_multiplier, output_multiplier),
            ):
                connection.execute(
                    """INSERT OR IGNORE INTO pricing_rule_versions(
                           catalog_version, model_pattern, service_tier, context_class,
                           regional_processing, currency, input_microusd_per_million,
                           cached_input_microusd_per_million, cache_write_microusd_per_million,
                           output_microusd_per_million, source, effective_from, created_at
                       ) VALUES (?, ?, 'standard', ?, 'none', ?, ?, ?, ?, ?, 'user_override', ?, ?)""",
                    (
                        str(row["version"]),
                        str(row["model_pattern"]),
                        context_class.value,
                        str(row["currency"]),
                        int(Decimal(input_rate) * multiplier_in),
                        int(Decimal(cached_rate) * multiplier_in),
                        int(Decimal(cache_write_rate) * multiplier_in),
                        int(Decimal(output_rate) * multiplier_out),
                        str(row["effective_at"]),
                        created_at,
                    ),
                )
            if threshold != 272_000:
                continue

    @staticmethod
    def _pricing_version_for(
        connection: sqlite3.Connection,
        *,
        billing_model: str | None,
        context_class: ContextClass | None,
        occurred_at: datetime,
    ) -> sqlite3.Row | None:
        if billing_model is None or context_class is None:
            return None
        return connection.execute(
            """SELECT * FROM pricing_rule_versions
               WHERE model_pattern=? AND service_tier='standard' AND context_class=?
                 AND regional_processing='none' AND effective_from <= ?
               ORDER BY CASE source WHEN 'user_override' THEN 0 ELSE 1 END,
                        effective_from DESC, id DESC LIMIT 1""",
            (billing_model, context_class.value, occurred_at.isoformat()),
        ).fetchone()

    @staticmethod
    def _billing_mode(value: Any) -> BillingMode:
        try:
            return BillingMode(str(value or BillingMode.UNKNOWN.value))
        except ValueError:
            return BillingMode.UNKNOWN

    @staticmethod
    def _builtin_tool_cost(record: dict[str, Any]) -> tuple[int | None, list[tuple[str, int, int | None]]]:
        usage = record.get("builtin_tool_usage")
        if not isinstance(usage, dict):
            return 0, []
        fixed_rates = {"web_search": 10_000, "file_search": 2_500}
        total = 0
        items: list[tuple[str, int, int | None]] = []
        incomplete = False
        for tool_type, raw_count in usage.items():
            count = _as_int(raw_count)
            if count is None or count < 0:
                incomplete = True
                continue
            rate = fixed_rates.get(str(tool_type))
            items.append((str(tool_type), count, rate))
            if rate is None:
                incomplete = True
            else:
                total += count * rate
        return (None if incomplete else total), items

    def _write_billing_details(
        self,
        connection: sqlite3.Connection,
        *,
        call_id: int,
        record: dict[str, Any],
        occurred_at: datetime,
        provider_reported_cost_microusd: int | None,
    ) -> None:
        usage = record.get("usage") if isinstance(record.get("usage"), dict) else {}
        input_tokens = _usage_value(usage, "input_tokens", "prompt_tokens")
        output_tokens = _usage_value(usage, "output_tokens", "completion_tokens")
        cached_tokens_raw = _usage_value(usage, "cache_read_input_tokens", "cached_tokens")
        cache_write_raw = _usage_value(usage, "cache_write_tokens", "cache_creation_input_tokens")
        reasoning_tokens = _usage_value(usage, "reasoning_tokens")
        audit_schema_version = _as_int(record.get("audit_schema_version")) or 0
        legacy_partial = audit_schema_version < 5 or cached_tokens_raw is None or cache_write_raw is None
        cached_tokens = 0 if cached_tokens_raw is None else cached_tokens_raw
        cache_write_tokens = 0 if cache_write_raw is None else cache_write_raw
        resolution = resolve_billing_model(
            response_model=record.get("response_model"),
            provider_model=record.get("provider_model"),
            selected_model=record.get("selected_model"),
            selected_alias=record.get("selected_alias"),
            requested_alias=record.get("requested_alias") or record.get("requested_model"),
        )
        context_class = (
            ContextClass.LONG if input_tokens is not None and input_tokens > 272_000
            else ContextClass.SHORT if input_tokens is not None else None
        )
        pricing = self._pricing_version_for(
            connection,
            billing_model=resolution.billing_model,
            context_class=context_class,
            occurred_at=occurred_at,
        )
        rates = PricingRates(
            int(pricing["input_microusd_per_million"]),
            int(pricing["cached_input_microusd_per_million"]),
            int(pricing["cache_write_microusd_per_million"]),
            int(pricing["output_microusd_per_million"]),
        ) if pricing is not None else None
        tool_cost, tool_items = self._builtin_tool_cost(record)
        estimate = calculate_api_equivalent_cost(
            usage=UsageBreakdown(input_tokens, cached_tokens, cache_write_tokens, output_tokens, reasoning_tokens),
            rates=rates,
            builtin_tool_cost_microusd=tool_cost,
            legacy_partial=legacy_partial,
        )
        status = estimate.status
        if resolution.billing_model is None and status == EstimateStatus.MODEL_UNPRICED:
            status = EstimateStatus.BILLING_MODEL_UNKNOWN
        billing_mode = self._billing_mode(record.get("billing_mode"))
        actual = actual_incremental_cost(
            billing_mode=billing_mode,
            provider_reported_cost_microusd=provider_reported_cost_microusd,
        )
        sol_baseline, sol_savings = sol_equivalent_savings(
            usage=UsageBreakdown(input_tokens, cached_tokens, cache_write_tokens, output_tokens, reasoning_tokens),
            selected_model_cost_microusd=estimate.model_token_cost_microusd,
        )
        applied_rates = {
            "input": rates.input_microusd_per_million,
            "cached_input": rates.cached_input_microusd_per_million,
            "cache_write": rates.cache_write_microusd_per_million,
            "output": rates.output_microusd_per_million,
        } if rates is not None else None
        connection.execute(
            """UPDATE api_calls SET cache_write_tokens=?, reasoning_tokens=?, billing_model=?,
                   pricing_match_source=?, billing_mode=?, billing_mode_source=?, observed_service_tier=?,
                   pricing_basis=?, context_class=?, regional_processing=?, regular_input_cost_microusd=?,
                   cached_input_cost_microusd=?, cache_write_cost_microusd=?, output_cost_microusd=?,
                   model_token_cost_microusd=?, builtin_tool_cost_microusd=?, storage_cost_microusd=?,
                   container_cost_microusd=?, known_api_equivalent_cost_microusd=?,
                   api_equivalent_cost_microusd=?, provider_reported_cost_microusd=?,
                   actual_incremental_cost_microusd=?, api_equivalent_status=?, actual_incremental_status=?,
                   pricing_rule_version_id=?, pricing_catalog_version=?, estimator_version=?, applied_rates_json=?,
                   billing_warnings_json=?, sol_baseline_cost_microusd=?, sol_equivalent_savings_microusd=?
               WHERE id=?""",
            (
                cache_write_raw,
                reasoning_tokens,
                resolution.billing_model,
                resolution.source,
                billing_mode.value,
                self._text(record.get("billing_mode_source")),
                self._text(record.get("observed_service_tier") or record.get("service_tier")),
                PRICING_BASIS,
                estimate.context_class.value if estimate.context_class else None,
                self._text(record.get("regional_processing") or "unknown"),
                estimate.regular_input_cost_microusd,
                estimate.cached_input_cost_microusd,
                estimate.cache_write_cost_microusd,
                estimate.output_cost_microusd,
                estimate.model_token_cost_microusd,
                estimate.builtin_tool_cost_microusd,
                estimate.storage_cost_microusd,
                estimate.container_cost_microusd,
                estimate.known_api_equivalent_cost_microusd,
                estimate.api_equivalent_cost_microusd,
                provider_reported_cost_microusd,
                actual.amount_microusd,
                status.value,
                actual.status.value,
                pricing["id"] if pricing is not None else None,
                pricing["catalog_version"] if pricing is not None else None,
                ESTIMATOR_VERSION,
                _json(applied_rates) if applied_rates is not None else None,
                _json(list(estimate.warnings)),
                sol_baseline,
                sol_savings,
                call_id,
            ),
        )
        quantities = {
            "regular_input": estimate.regular_input_tokens,
            "cached_input": cached_tokens_raw,
            "cache_write": cache_write_raw,
            "output": output_tokens,
        }
        rates_by_item = {
            "regular_input": rates.input_microusd_per_million if rates else None,
            "cached_input": rates.cached_input_microusd_per_million if rates else None,
            "cache_write": rates.cache_write_microusd_per_million if rates else None,
            "output": rates.output_microusd_per_million if rates else None,
        }
        amounts = {
            "regular_input": estimate.regular_input_cost_microusd,
            "cached_input": estimate.cached_input_cost_microusd,
            "cache_write": estimate.cache_write_cost_microusd,
            "output": estimate.output_cost_microusd,
        }
        connection.execute("DELETE FROM call_cost_items WHERE api_call_id=?", (call_id,))
        for item_type in quantities:
            connection.execute(
                """INSERT INTO call_cost_items(api_call_id, item_type, quantity, unit, rate_microusd,
                       amount_microusd, pricing_status, source) VALUES (?, ?, ?, 'tokens', ?, ?, ?, 'pricing_catalog')""",
                (call_id, item_type, quantities[item_type], rates_by_item[item_type], amounts[item_type], status.value),
            )
        for tool_type, count, rate in tool_items:
            connection.execute(
                """INSERT INTO call_cost_items(api_call_id, item_type, quantity, unit, rate_microusd,
                       amount_microusd, pricing_status, source) VALUES (?, ?, ?, 'calls', ?, ?, ?, 'audit')""",
                (call_id, f"builtin_tool_{tool_type}", count, rate, count * rate if rate is not None else None, status.value),
            )

    def _backfill_billing_v2(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            "SELECT * FROM api_calls WHERE estimator_version IS NULL OR estimator_version<>?",
            (ESTIMATOR_VERSION,),
        ).fetchall()
        for row in rows:
            record = dict(row)
            metadata = json.loads(record.get("safe_metadata_json") or "{}")
            record.update(metadata)
            record["usage"] = {
                "input_tokens": record.get("input_tokens"),
                "output_tokens": record.get("output_tokens"),
                "cache_read_input_tokens": record.get("cache_read_tokens"),
                "cache_write_tokens": record.get("cache_write_tokens"),
                "reasoning_tokens": record.get("reasoning_tokens"),
            }
            self._write_billing_details(
                connection,
                call_id=int(row["id"]),
                record=record,
                occurred_at=_parse_timestamp(row["occurred_at"]) or datetime.now(UTC),
                provider_reported_cost_microusd=row["reported_cost_microusd"],
            )

    def import_audit_jsonl(self, audit_path: Path) -> dict[str, int]:
        self.initialize()
        if not audit_path.exists():
            return {"imported": 0, "skipped": 0, "offset": 0}
        stat = audit_path.stat()
        with self._connect() as connection:
            cursor = connection.execute(
                "SELECT source_size, source_mtime_ns, last_offset FROM import_cursors WHERE source_path=?",
                (str(audit_path),),
            ).fetchone()
            if (
                cursor
                and int(cursor["source_size"]) == stat.st_size
                and int(cursor["source_mtime_ns"]) == stat.st_mtime_ns
                and int(cursor["last_offset"]) == stat.st_size
            ):
                return {"imported": 0, "skipped": 0, "offset": stat.st_size}
            offset = int(cursor["last_offset"]) if cursor and stat.st_size >= int(cursor["last_offset"]) else 0

        imported = 0
        skipped = 0
        with audit_path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(offset)
            with self._connect() as connection:
                for raw_line in handle:
                    try:
                        record = json.loads(raw_line)
                    except json.JSONDecodeError:
                        skipped += 1
                        continue
                    if not isinstance(record, dict):
                        skipped += 1
                        continue
                    if self._insert_audit_record(connection, record):
                        imported += 1
                    else:
                        skipped += 1
                last_offset = handle.tell()
                connection.execute(
                    """INSERT INTO import_cursors(source_path, source_size, source_mtime_ns, last_offset, updated_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(source_path) DO UPDATE SET source_size=excluded.source_size,
                         source_mtime_ns=excluded.source_mtime_ns, last_offset=excluded.last_offset,
                         updated_at=excluded.updated_at""",
                    (str(audit_path), stat.st_size, stat.st_mtime_ns, last_offset, _utc_now()),
                )
        return {"imported": imported, "skipped": skipped, "offset": last_offset}

    def import_encrypted_payloads(self, *, max_records: int | None = None) -> dict[str, int | bool]:
        """Project payload semantics and archive offsets without duplicating ciphertext in SQLite."""
        self.initialize()
        if self.payload_path is None or self.payload_key is None or not self.payload_path.exists():
            return {"imported": 0, "skipped": 0, "offset": 0, "complete": True}
        if max_records is not None and max_records < 1:
            raise ValueError("max_records must be at least 1.")
        stat = self.payload_path.stat()
        source_path = str(self.payload_path)
        with self._connect() as connection:
            cursor = connection.execute(
                "SELECT last_offset FROM import_cursors WHERE source_path=?",
                (source_path,),
            ).fetchone()
            offset = int(cursor["last_offset"]) if cursor and stat.st_size >= int(cursor["last_offset"]) else 0
        imported = 0
        skipped = 0
        processed = 0
        with self.payload_path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(offset)
            with self._connect() as connection:
                while True:
                    line_offset = handle.tell()
                    raw_line = handle.readline()
                    if not raw_line:
                        break
                    if not raw_line.endswith("\n"):
                        handle.seek(line_offset)
                        break
                    processed += 1
                    try:
                        envelope = json.loads(raw_line)
                        if not isinstance(envelope, dict):
                            raise ValueError("payload envelope must be an object")
                        raw = decrypt_payload(self.payload_key, envelope)
                        payload = json.loads(raw)
                        payload_ref = payload.get("payload_ref")
                        if not isinstance(payload_ref, str) or not payload_ref:
                            raise ValueError("payload reference missing")
                        if envelope.get("payload_ref") != payload_ref:
                            raise ValueError("payload reference mismatch")
                        tombstone = connection.execute(
                            "SELECT 1 FROM payload_retention_tombstones WHERE payload_ref=?",
                            (payload_ref,),
                        ).fetchone()
                        if tombstone is not None:
                            skipped += 1
                            continue
                        indexed_at = _utc_now()
                        connection.execute(
                            """INSERT INTO payload_archive_index(
                                   payload_ref, source_path, source_inode, byte_offset, byte_length, indexed_at
                               ) VALUES (?, ?, ?, ?, ?, ?)
                               ON CONFLICT(payload_ref) DO UPDATE SET
                                   source_path=excluded.source_path,
                                   source_inode=excluded.source_inode,
                                   byte_offset=excluded.byte_offset,
                                   byte_length=excluded.byte_length,
                                   indexed_at=excluded.indexed_at""",
                            (
                                payload_ref,
                                source_path,
                                stat.st_ino,
                                line_offset,
                                len(raw_line.encode("utf-8")),
                                indexed_at,
                            ),
                        )
                        call = connection.execute(
                            """SELECT id, status, output_tokens, output_semantics_version,
                                      output_semantics_source
                                 FROM api_calls WHERE payload_ref=?""",
                            (payload_ref,),
                        ).fetchone()
                        if call is None:
                            skipped += 1
                            continue
                        self._apply_payload_output_semantics(connection, call, payload)
                        imported += 1
                    except (ValueError, KeyError, json.JSONDecodeError):
                        skipped += 1
                    if max_records is not None and processed >= max_records:
                        break
                last_offset = handle.tell()
                connection.execute(
                    """INSERT INTO import_cursors(source_path, source_size, source_mtime_ns, last_offset, updated_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(source_path) DO UPDATE SET source_size=excluded.source_size,
                         source_mtime_ns=excluded.source_mtime_ns, last_offset=excluded.last_offset,
                         updated_at=excluded.updated_at""",
                    (source_path, stat.st_size, stat.st_mtime_ns, last_offset, _utc_now()),
                )
                connection.execute(
                    """INSERT INTO payload_archive_cursors(
                           source_path, source_inode, source_size, source_mtime_ns, last_offset, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(source_path) DO UPDATE SET
                           source_inode=excluded.source_inode,
                           source_size=excluded.source_size,
                           source_mtime_ns=excluded.source_mtime_ns,
                           last_offset=excluded.last_offset,
                           updated_at=excluded.updated_at""",
                    (source_path, stat.st_ino, stat.st_size, stat.st_mtime_ns, last_offset, _utc_now()),
                )
        return {
            "imported": imported,
            "skipped": skipped,
            "offset": last_offset,
            "complete": last_offset >= stat.st_size,
        }

    def index_payload_archive(
        self,
        *,
        max_records: int = 250,
        max_bytes: int = 128 * 1024 * 1024,
        max_seconds: float = 0.35,
    ) -> dict[str, int | bool]:
        self.initialize()
        if self.payload_path is None or not self.payload_path.exists():
            return {"indexed": 0, "skipped": 0, "offset": 0, "file_size": 0, "complete": True}
        source_path = str(self.payload_path)
        stat = self.payload_path.stat()
        reset = False
        with self._connect() as connection:
            self._reconcile_payload_archive_identity(connection, source_path, stat)
            cursor = connection.execute(
                "SELECT * FROM payload_archive_cursors WHERE source_path=?",
                (source_path,),
            ).fetchone()
            if cursor is not None:
                reset = int(cursor["source_inode"]) != stat.st_ino or stat.st_size < int(cursor["last_offset"])
                start_offset = 0 if reset else int(cursor["last_offset"])
            else:
                legacy = connection.execute(
                    "SELECT last_offset FROM import_cursors WHERE source_path=?",
                    (source_path,),
                ).fetchone()
                legacy_offset = int(legacy["last_offset"]) if legacy is not None else 0
                start_offset = legacy_offset if 0 <= legacy_offset <= stat.st_size else 0

        scan = scan_payload_archive(
            self.payload_path,
            start_offset=start_offset,
            max_records=max_records,
            max_bytes=max_bytes,
            max_seconds=max_seconds,
        )
        indexed_at = _utc_now()
        entries = scan["entries"]
        with self._connect() as connection:
            if reset:
                connection.execute("DELETE FROM payload_archive_index WHERE source_path=?", (source_path,))
            tombstones = {
                row["payload_ref"]
                for row in connection.execute(
                    "SELECT payload_ref FROM payload_retention_tombstones WHERE payload_ref IS NOT NULL"
                )
            }
            connection.executemany(
                """INSERT INTO payload_archive_index(
                       payload_ref, source_path, source_inode, byte_offset, byte_length, indexed_at
                   ) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(payload_ref) DO UPDATE SET
                       source_path=excluded.source_path,
                       source_inode=excluded.source_inode,
                       byte_offset=excluded.byte_offset,
                       byte_length=excluded.byte_length,
                       indexed_at=excluded.indexed_at""",
                [
                    (
                        entry.payload_ref,
                        source_path,
                        int(scan["file_inode"]),
                        entry.byte_offset,
                        entry.byte_length,
                        indexed_at,
                    )
                    for entry in entries
                    if entry.payload_ref not in tombstones
                ],
            )
            connection.execute(
                """INSERT INTO payload_archive_cursors(
                       source_path, source_inode, source_size, source_mtime_ns, last_offset, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source_path) DO UPDATE SET
                       source_inode=excluded.source_inode,
                       source_size=excluded.source_size,
                       source_mtime_ns=excluded.source_mtime_ns,
                       last_offset=excluded.last_offset,
                       updated_at=excluded.updated_at""",
                (
                    source_path,
                    int(scan["file_inode"]),
                    int(scan["file_size"]),
                    int(scan["file_mtime_ns"]),
                    int(scan["offset"]),
                    indexed_at,
                ),
            )
        return {
            "indexed": len(entries),
            "skipped": int(scan["skipped"]),
            "offset": int(scan["offset"]),
            "file_size": int(scan["file_size"]),
            "complete": bool(scan["complete"]),
        }

    def reconcile_payload_archive_identity(self) -> dict[str, int | bool]:
        """Rebind migrated archive metadata only when the live file identity proves equivalence."""
        self.initialize()
        archive_path = self.payload_path
        if archive_path is None or not archive_path.exists():
            return {"available": False, "rebound_index_count": 0, "rebound_cursor_count": 0}
        archive_stat = archive_path.stat()
        with self._connect() as connection:
            result = self._reconcile_payload_archive_identity(connection, str(archive_path), archive_stat)
        return {"available": True, **result}

    @staticmethod
    def _reconcile_payload_archive_identity(
        connection: sqlite3.Connection,
        source_path: str,
        archive_stat: os.stat_result,
    ) -> dict[str, int]:
        """Make the current pathname canonical without trusting pathname text alone."""
        rebound = connection.execute(
            """UPDATE payload_archive_index
                  SET source_path=?
                WHERE source_path<>? AND source_inode=?
                  AND byte_offset>=0 AND byte_length>0
                  AND byte_offset+byte_length<=?""",
            (source_path, source_path, archive_stat.st_ino, archive_stat.st_size),
        ).rowcount
        matching_cursors = connection.execute(
            """SELECT source_path, last_offset
                 FROM payload_archive_cursors
                WHERE source_inode=? AND last_offset>=0 AND last_offset<=?""",
            (archive_stat.st_ino, archive_stat.st_size),
        ).fetchall()
        rebound_cursor_count = sum(1 for row in matching_cursors if row["source_path"] != source_path)
        if matching_cursors:
            last_offset = max(int(row["last_offset"]) for row in matching_cursors)
            updated_at = _utc_now()
            connection.execute(
                """INSERT INTO payload_archive_cursors(
                       source_path, source_inode, source_size, source_mtime_ns, last_offset, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source_path) DO UPDATE SET
                       source_inode=excluded.source_inode,
                       source_size=excluded.source_size,
                       source_mtime_ns=excluded.source_mtime_ns,
                       last_offset=excluded.last_offset,
                       updated_at=excluded.updated_at""",
                (
                    source_path,
                    archive_stat.st_ino,
                    archive_stat.st_size,
                    archive_stat.st_mtime_ns,
                    last_offset,
                    updated_at,
                ),
            )
            connection.execute(
                "DELETE FROM payload_archive_cursors WHERE source_path<>? AND source_inode=?",
                (source_path, archive_stat.st_ino),
            )
        return {
            "rebound_index_count": max(0, int(rebound)),
            "rebound_cursor_count": rebound_cursor_count,
        }

    def payload_archive_status(self) -> dict[str, Any]:
        self.initialize()
        if self.payload_path is None or not self.payload_path.exists():
            return {"available": False, "indexed": 0, "offset": 0, "file_size": 0, "complete": True}
        source_path = str(self.payload_path)
        stat = self.payload_path.stat()
        with self._connect() as connection:
            cursor = connection.execute(
                "SELECT * FROM payload_archive_cursors WHERE source_path=?",
                (source_path,),
            ).fetchone()
            indexed = connection.execute(
                "SELECT COUNT(*) FROM payload_archive_index WHERE source_path=?",
                (source_path,),
            ).fetchone()[0]
        offset = int(cursor["last_offset"]) if cursor and int(cursor["source_inode"]) == stat.st_ino else 0
        return {
            "available": True,
            "indexed": int(indexed),
            "offset": offset,
            "file_size": stat.st_size,
            "complete": offset >= stat.st_size,
            "updated_at": cursor["updated_at"] if cursor else None,
        }

    def payload_archive_compression_inventory(self, *, sample_limit: int = 12) -> dict[str, Any]:
        """Return archive rewrite counts and a bounded archive-only sample."""
        if sample_limit < 1 or sample_limit > 50:
            raise ValueError("Archive compression sample limit must be between 1 and 50.")
        self.initialize()
        archive_path = self.payload_path
        if archive_path is None or not archive_path.exists():
            return {
                "available": False,
                "indexed_payload_count": 0,
                "eligible_payload_count": 0,
                "eligible_archive_bytes": 0,
                "embedded_payload_count": 0,
                "samples": [],
            }
        archive_stat = archive_path.stat()
        with self._connect() as connection:
            connection.execute("BEGIN")
            aggregate = connection.execute(
                """SELECT COUNT(*) indexed_payload_count,
                          COALESCE(SUM(i.byte_length), 0) indexed_archive_bytes,
                          COALESCE(SUM(CASE WHEN p.payload_ref IS NULL THEN 1 ELSE 0 END), 0)
                              eligible_payload_count,
                          COALESCE(SUM(CASE WHEN p.payload_ref IS NULL THEN i.byte_length ELSE 0 END), 0)
                              eligible_archive_bytes,
                          COALESCE(SUM(CASE WHEN p.payload_ref IS NOT NULL THEN 1 ELSE 0 END), 0)
                              embedded_payload_count
                     FROM payload_archive_index i
                     LEFT JOIN call_payloads p ON p.payload_ref=i.payload_ref
                    WHERE i.source_inode=? AND i.byte_offset>=0 AND i.byte_length>0
                      AND i.byte_offset+i.byte_length<=?""",
                (archive_stat.st_ino, archive_stat.st_size),
            ).fetchone()
            samples = [
                dict(row)
                for row in connection.execute(
                    """SELECT i.payload_ref, i.source_path, i.byte_offset, i.byte_length
                         FROM payload_archive_index i
                         LEFT JOIN call_payloads p ON p.payload_ref=i.payload_ref
                        WHERE i.source_inode=? AND i.byte_offset>=0 AND i.byte_length>0
                          AND i.byte_offset+i.byte_length<=? AND p.payload_ref IS NULL
                        ORDER BY i.byte_length DESC, i.payload_ref
                        LIMIT ?""",
                    (archive_stat.st_ino, archive_stat.st_size, sample_limit),
                ).fetchall()
            ]
            rewrite_set_digest = hashlib.sha256()
            for row in connection.execute(
                """SELECT i.payload_ref, i.source_path, i.source_inode,
                          i.byte_offset, i.byte_length,
                          CASE WHEN p.payload_ref IS NULL THEN 1 ELSE 0 END eligible
                     FROM payload_archive_index i
                     LEFT JOIN call_payloads p ON p.payload_ref=i.payload_ref
                    WHERE i.source_inode=? AND i.byte_offset>=0 AND i.byte_length>0
                      AND i.byte_offset+i.byte_length<=?
                    ORDER BY i.payload_ref""",
                (archive_stat.st_ino, archive_stat.st_size),
            ):
                encoded = json.dumps(
                    [
                        str(row["payload_ref"]),
                        str(row["source_path"]),
                        int(row["source_inode"]),
                        int(row["byte_offset"]),
                        int(row["byte_length"]),
                        int(row["eligible"]),
                    ],
                    ensure_ascii=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                rewrite_set_digest.update(len(encoded).to_bytes(8, "big"))
                rewrite_set_digest.update(encoded)
        return {
            "available": True,
            "indexed_payload_count": int(aggregate["indexed_payload_count"]),
            "indexed_archive_bytes": int(aggregate["indexed_archive_bytes"]),
            "eligible_payload_count": int(aggregate["eligible_payload_count"]),
            "eligible_archive_bytes": int(aggregate["eligible_archive_bytes"]),
            "embedded_payload_count": int(aggregate["embedded_payload_count"]),
            "archive_inode": archive_stat.st_ino,
            "archive_size": archive_stat.st_size,
            "archive_mtime_ns": archive_stat.st_mtime_ns,
            "rewrite_set_digest": rewrite_set_digest.hexdigest(),
            "samples": samples,
        }

    def embedded_payload_refs(self) -> set[str]:
        self.initialize()
        with self._connect() as connection:
            return {
                str(row[0])
                for row in connection.execute("SELECT payload_ref FROM call_payloads").fetchall()
            }

    def payload_archive_index_entries(self) -> list[dict[str, Any]]:
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_ref, byte_offset, byte_length FROM payload_archive_index ORDER BY byte_offset"
            ).fetchall()
        return [dict(row) for row in rows]

    def replace_payload_archive_index(
        self,
        archive_entries: list[dict[str, Any]],
        archive_stat: os.stat_result,
    ) -> None:
        self.initialize()
        with self._connect() as connection:
            self._replace_archive_index(connection, archive_entries, archive_stat, _utc_now())

    def payload_projection_audit(self) -> dict[str, Any]:
        """Prove which embedded payload copies are safely represented by the live archive."""
        self.initialize()
        archive = self.payload_archive_status()
        archive_path = self.payload_path
        archive_stat = archive_path.stat() if archive_path is not None and archive_path.exists() else None
        source_path = str(archive_path) if archive_path is not None else ""
        source_inode = archive_stat.st_ino if archive_stat is not None else -1
        source_size = archive_stat.st_size if archive_stat is not None else 0
        with self._connect() as connection:
            row = connection.execute(
                """SELECT COUNT(*) embedded_payload_count,
                          COALESCE(SUM(length(p.nonce) + length(p.ciphertext)), 0) embedded_payload_bytes,
                          COALESCE(SUM(CASE WHEN i.source_path=? AND i.source_inode=?
                                                AND i.byte_offset>=0 AND i.byte_length>0
                                                AND i.byte_offset+i.byte_length<=?
                                            THEN 1 ELSE 0 END), 0) archive_covered_payload_count,
                          COALESCE(SUM(CASE WHEN i.source_path=? AND i.source_inode=?
                                                AND i.byte_offset>=0 AND i.byte_length>0
                                                AND i.byte_offset+i.byte_length<=?
                                            THEN length(p.nonce) + length(p.ciphertext) ELSE 0 END), 0)
                              archive_covered_payload_bytes,
                          COALESCE(MAX(CASE WHEN i.source_path=? AND i.source_inode=?
                                                AND i.byte_offset>=0 AND i.byte_length>0
                                                AND i.byte_offset+i.byte_length<=?
                                            THEN i.byte_offset+i.byte_length ELSE 0 END), 0) max_indexed_end
                     FROM call_payloads p
                     LEFT JOIN payload_archive_index i ON i.payload_ref=p.payload_ref""",
                (
                    source_path, source_inode, source_size,
                    source_path, source_inode, source_size,
                    source_path, source_inode, source_size,
                ),
            ).fetchone()
            try:
                embedded_table_bytes = int(connection.execute(
                    "SELECT COALESCE(SUM(pgsize), 0) FROM dbstat WHERE name='call_payloads'"
                ).fetchone()[0])
            except sqlite3.Error:
                embedded_table_bytes = int(row["embedded_payload_bytes"])
        embedded_count = int(row["embedded_payload_count"])
        covered_count = int(row["archive_covered_payload_count"])
        missing_count = max(0, embedded_count - covered_count)
        archive_complete = bool(archive.get("complete"))
        archive_available = bool(archive.get("available"))
        key_available = self.payload_key is not None
        if embedded_count == 0:
            state = "not_needed"
        elif not key_available:
            state = "key_unavailable"
        elif not archive_available:
            state = "archive_unavailable"
        elif not archive_complete:
            state = "indexing_required"
        elif missing_count == 0:
            state = "ready"
        else:
            state = "ready_with_retained_items"
        embedded_payload_bytes = int(row["embedded_payload_bytes"])
        covered_payload_bytes = int(row["archive_covered_payload_bytes"])
        removable_payload_bytes = 0
        if state in {"ready", "ready_with_retained_items"} and embedded_payload_bytes > 0:
            covered_ratio = min(1.0, covered_payload_bytes / embedded_payload_bytes)
            removable_payload_bytes = int(embedded_table_bytes * covered_ratio)
        return {
            "state": state,
            "embedded_payload_count": embedded_count,
            "embedded_payload_bytes": embedded_payload_bytes,
            "embedded_table_bytes": embedded_table_bytes,
            "archive_covered_payload_count": covered_count,
            "archive_covered_payload_bytes": covered_payload_bytes,
            "missing_archive_payload_count": missing_count,
            "removable_payload_bytes": removable_payload_bytes,
            "archive_available": archive_available,
            "archive_complete": archive_complete,
            "archive_path": source_path or None,
            "archive_inode": archive_stat.st_ino if archive_stat is not None else None,
            "archive_size": source_size,
            "archive_mtime_ns": archive_stat.st_mtime_ns if archive_stat is not None else None,
            "max_indexed_end": int(row["max_indexed_end"]),
            "decryption_key_available": key_available,
        }

    def payload_archive_canary_status(self, *, limit: int = 3) -> dict[str, int | bool]:
        """Decrypt a bounded set of archive-backed details without returning their content."""
        if limit < 1 or limit > 10:
            raise ValueError("Archive canary limit must be between 1 and 10.")
        self.initialize()
        with self._connect() as connection:
            call_ids = [
                int(row["id"])
                for row in connection.execute(
                    """SELECT a.id
                         FROM api_calls a
                         JOIN payload_archive_index i ON i.payload_ref=a.payload_ref
                         LEFT JOIN call_payloads p ON p.payload_ref=a.payload_ref
                         LEFT JOIN payload_retention_tombstones t ON t.payload_ref=a.payload_ref
                        WHERE t.payload_ref IS NULL AND p.payload_ref IS NULL
                        ORDER BY a.id
                        LIMIT ?""",
                    (limit,),
                ).fetchall()
            ]
        available = 0
        for call_id in call_ids:
            payload = self._load_detail_payload(call_id)
            if payload is not None and payload.get("state") == "available" and payload.get("source") == "encrypted_log":
                available += 1
        return {
            "tested": len(call_ids),
            "available": available,
            "passed": bool(call_ids) and available == len(call_ids),
        }

    @staticmethod
    def _storage_bucket_range(value: str) -> tuple[str, str] | None:
        parsed = _parse_timestamp(value)
        if parsed is None:
            return None
        start = parsed.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        return start.isoformat(), (start + timedelta(hours=1)).isoformat()

    def storage_bucket_status(self) -> dict[str, Any]:
        self.initialize()
        with self._connect() as connection:
            progress = {
                row["source"]: int(row["last_row_id"])
                for row in connection.execute("SELECT source, last_row_id FROM storage_retention_progress")
            }
            maxima = {
                "calls": int(connection.execute("SELECT COALESCE(MAX(id), 0) FROM api_calls").fetchone()[0]),
                "embedded": int(connection.execute("SELECT COALESCE(MAX(id), 0) FROM call_payloads").fetchone()[0]),
                "archive": int(connection.execute("SELECT COALESCE(MAX(rowid), 0) FROM payload_archive_index").fetchone()[0]),
            }
            row = connection.execute(
                """SELECT COUNT(*) bucket_count, MIN(bucket_start) earliest_bucket,
                          MAX(bucket_end) latest_bucket,
                          COALESCE(SUM(call_count), 0) call_count,
                          COALESCE(SUM(embedded_payload_bytes), 0) embedded_payload_bytes,
                          COALESCE(SUM(archive_payload_bytes), 0) archive_payload_bytes
                     FROM storage_retention_buckets"""
            ).fetchone()
        complete = all(progress.get(source, 0) >= maximum for source, maximum in maxima.items())
        return {
            "complete": complete,
            "bucket_count": int(row["bucket_count"]),
            "earliest_bucket": row["earliest_bucket"],
            "latest_bucket": row["latest_bucket"],
            "call_count": int(row["call_count"]),
            "embedded_payload_bytes": int(row["embedded_payload_bytes"]),
            "archive_payload_bytes": int(row["archive_payload_bytes"]),
            "progress": progress,
            "maxima": maxima,
        }

    def refresh_storage_buckets(
        self,
        *,
        max_records: int = 250,
        max_seconds: float = 0.2,
    ) -> dict[str, Any]:
        self.initialize()
        if max_records < 1 or max_seconds <= 0:
            raise ValueError("Storage bucket limits must be positive.")
        started = time.monotonic()
        processed = 0
        with self._connect() as connection:
            progress = {
                row["source"]: int(row["last_row_id"])
                for row in connection.execute("SELECT source, last_row_id FROM storage_retention_progress")
            }
            sources = (
                ("calls", "SELECT id row_id, id api_call_id, occurred_at, 0 payload_bytes FROM api_calls WHERE id>? ORDER BY id LIMIT ?"),
                ("embedded", """SELECT p.id row_id, p.api_call_id, a.occurred_at,
                                      length(p.nonce) + length(p.ciphertext) payload_bytes
                                 FROM call_payloads p JOIN api_calls a ON a.id=p.api_call_id
                                WHERE p.id>? ORDER BY p.id LIMIT ?"""),
                ("archive", """SELECT i.rowid row_id, a.id api_call_id,
                                     COALESCE(a.occurred_at, i.indexed_at) occurred_at,
                                     i.byte_length payload_bytes
                                FROM payload_archive_index i
                                LEFT JOIN api_calls a ON a.payload_ref=i.payload_ref
                               WHERE i.rowid>? ORDER BY i.rowid LIMIT ?"""),
            )
            for source, query in sources:
                if processed >= max_records or time.monotonic() - started >= max_seconds:
                    break
                limit = max_records - processed
                rows = connection.execute(query, (progress.get(source, 0), limit)).fetchall()
                for row in rows:
                    bucket = self._storage_bucket_range(str(row["occurred_at"] or ""))
                    if bucket is not None:
                        call_id = int(row["api_call_id"]) if row["api_call_id"] is not None else None
                        values = {
                            "call_count": 1 if source == "calls" else 0,
                            "embedded_count": 1 if source == "embedded" else 0,
                            "embedded_bytes": int(row["payload_bytes"] or 0) if source == "embedded" else 0,
                            "archive_count": 1 if source == "archive" else 0,
                            "archive_bytes": int(row["payload_bytes"] or 0) if source == "archive" else 0,
                        }
                        connection.execute(
                            """INSERT INTO storage_retention_buckets(
                                   bucket_start, bucket_end, call_count,
                                   embedded_payload_count, embedded_payload_bytes,
                                   archive_payload_count, archive_payload_bytes,
                                   first_call_id, last_call_id, updated_at
                               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                               ON CONFLICT(bucket_start) DO UPDATE SET
                                   call_count=call_count+excluded.call_count,
                                   embedded_payload_count=embedded_payload_count+excluded.embedded_payload_count,
                                   embedded_payload_bytes=embedded_payload_bytes+excluded.embedded_payload_bytes,
                                   archive_payload_count=archive_payload_count+excluded.archive_payload_count,
                                   archive_payload_bytes=archive_payload_bytes+excluded.archive_payload_bytes,
                                   first_call_id=CASE
                                     WHEN excluded.first_call_id IS NULL THEN first_call_id
                                     WHEN first_call_id IS NULL THEN excluded.first_call_id
                                     ELSE MIN(first_call_id, excluded.first_call_id) END,
                                   last_call_id=CASE
                                     WHEN excluded.last_call_id IS NULL THEN last_call_id
                                     WHEN last_call_id IS NULL THEN excluded.last_call_id
                                     ELSE MAX(last_call_id, excluded.last_call_id) END,
                                   updated_at=excluded.updated_at""",
                            (
                                bucket[0], bucket[1], values["call_count"], values["embedded_count"],
                                values["embedded_bytes"], values["archive_count"], values["archive_bytes"],
                                call_id, call_id, _utc_now(),
                            ),
                        )
                    progress[source] = int(row["row_id"])
                    processed += 1
                if rows:
                    connection.execute(
                        """INSERT INTO storage_retention_progress(source, last_row_id, updated_at)
                           VALUES (?, ?, ?)
                           ON CONFLICT(source) DO UPDATE SET last_row_id=excluded.last_row_id,
                             updated_at=excluded.updated_at""",
                        (source, progress[source], _utc_now()),
                    )
        status = self.storage_bucket_status()
        return {"processed": processed, "complete": status["complete"], "status": status}

    def storage_overview(self) -> dict[str, Any]:
        self.initialize()
        database_bytes = self.database_path.stat().st_size if self.database_path.exists() else 0
        wal_path = Path(f"{self.database_path}-wal")
        shm_path = Path(f"{self.database_path}-shm")
        with self._connect() as connection:
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
            freelist_count = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
            auto_vacuum = int(connection.execute("PRAGMA auto_vacuum").fetchone()[0])
            call_row = connection.execute(
                "SELECT COUNT(*) call_count, MIN(occurred_at) earliest, MAX(occurred_at) latest FROM api_calls"
            ).fetchone()
            payload_count = int(connection.execute("SELECT COUNT(*) FROM call_payloads").fetchone()[0])
        return {
            "path": str(self.database_path),
            "bytes": database_bytes,
            "wal_bytes": wal_path.stat().st_size if wal_path.exists() else 0,
            "shm_bytes": shm_path.stat().st_size if shm_path.exists() else 0,
            "page_size": page_size,
            "page_count": page_count,
            "freelist_count": freelist_count,
            "reclaimable_bytes": page_size * freelist_count,
            "auto_vacuum": auto_vacuum,
            "call_count": int(call_row["call_count"]),
            "earliest_call_at": call_row["earliest"],
            "latest_call_at": call_row["latest"],
            "embedded_payload_count": payload_count,
        }

    def storage_retention_buckets(self) -> list[dict[str, Any]]:
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM storage_retention_buckets ORDER BY bucket_start ASC"
            ).fetchall()
        return [dict(row) for row in rows]

    def retention_payload_refs(self, cutoff_at: str) -> list[dict[str, Any]]:
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT MIN(a.id) api_call_id, a.payload_ref, MIN(a.occurred_at) occurred_at,
                          COUNT(*) affected_call_count,
                          CASE WHEN MAX(p.api_call_id) IS NOT NULL THEN 'database' ELSE 'encrypted_log' END original_storage,
                          COALESCE(MAX(length(p.nonce) + length(p.ciphertext)), MAX(i.byte_length), 0) original_bytes
                     FROM api_calls a
                     LEFT JOIN call_payloads p ON p.payload_ref=a.payload_ref
                     LEFT JOIN payload_archive_index i ON i.payload_ref=a.payload_ref
                    WHERE a.occurred_at < ? AND a.payload_ref IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM payload_retention_tombstones t WHERE t.payload_ref=a.payload_ref
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM api_calls retained
                           WHERE retained.payload_ref=a.payload_ref AND retained.occurred_at>=?
                      )
                    GROUP BY a.payload_ref
                    ORDER BY MIN(a.id)""",
                (cutoff_at, cutoff_at),
            ).fetchall()
        return [dict(row) for row in rows]

    def retention_payload_preflight(self, cutoff_at: str) -> dict[str, Any]:
        payloads = self.retention_payload_refs(cutoff_at)
        with self._connect() as connection:
            protected_shared_payload_count = int(connection.execute(
                """SELECT COUNT(DISTINCT old.payload_ref)
                     FROM api_calls old
                    WHERE old.occurred_at < ? AND old.payload_ref IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM payload_retention_tombstones t
                           WHERE t.payload_ref=old.payload_ref
                      )
                      AND EXISTS (
                          SELECT 1 FROM api_calls retained
                           WHERE retained.payload_ref=old.payload_ref AND retained.occurred_at>=?
                      )""",
                (cutoff_at, cutoff_at),
            ).fetchone()[0])
        affected_call_count = sum(int(item.get("affected_call_count") or 0) for item in payloads)
        return {
            "payloads": payloads,
            "candidate_call_count": affected_call_count,
            "unique_payload_count": len(payloads),
            "duplicate_reference_count": max(0, affected_call_count - len(payloads)),
            "protected_shared_payload_count": protected_shared_payload_count,
        }

    def apply_payload_retention(
        self,
        *,
        cutoff_at: str,
        job_id: str,
        archive_entries: list[dict[str, Any]],
        archive_stat: os.stat_result | None,
    ) -> dict[str, Any]:
        self.initialize()
        payloads = self.retention_payload_refs(cutoff_at)
        deleted_at = _utc_now()
        refs = [str(item["payload_ref"]) for item in payloads]
        with self._connect() as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executemany(
                """INSERT OR IGNORE INTO payload_retention_tombstones(
                       payload_ref, api_call_id, occurred_at, deleted_at, delete_reason,
                       retention_job_id, original_storage, original_bytes
                   ) VALUES (?, ?, ?, ?, 'capacity_limit', ?, ?, ?)""",
                [
                    (
                        item["payload_ref"], item["api_call_id"], item["occurred_at"], deleted_at,
                        job_id, item["original_storage"], int(item["original_bytes"] or 0),
                    )
                    for item in payloads
                ],
            )
            if refs:
                connection.executemany("DELETE FROM call_payloads WHERE payload_ref=?", [(ref,) for ref in refs])
                connection.executemany("DELETE FROM payload_archive_index WHERE payload_ref=?", [(ref,) for ref in refs])
            if archive_stat is not None:
                self._replace_archive_index(connection, archive_entries, archive_stat, deleted_at)
            connection.execute(
                """UPDATE storage_retention_buckets
                      SET embedded_payload_count=0, embedded_payload_bytes=0,
                          archive_payload_count=0, archive_payload_bytes=0, updated_at=?
                    WHERE bucket_end <= ?""",
                (deleted_at, cutoff_at),
            )
            remaining_payloads = 0
            remaining_archive_refs = 0
            tombstones = 0
            if refs:
                remaining_payloads = int(connection.execute(
                    """SELECT COUNT(*) FROM call_payloads p
                         JOIN payload_retention_tombstones t ON t.payload_ref=p.payload_ref
                        WHERE t.retention_job_id=?""",
                    (job_id,),
                ).fetchone()[0])
                remaining_archive_refs = int(connection.execute(
                    """SELECT COUNT(*) FROM payload_archive_index i
                         JOIN payload_retention_tombstones t ON t.payload_ref=i.payload_ref
                        WHERE t.retention_job_id=?""",
                    (job_id,),
                ).fetchone()[0])
                tombstones = int(connection.execute(
                    "SELECT COUNT(*) FROM payload_retention_tombstones WHERE retention_job_id=?",
                    (job_id,),
                ).fetchone()[0])
            quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
            if remaining_payloads or remaining_archive_refs:
                raise RuntimeError("Payload retention left deleted payload references active.")
            if tombstones != len(refs):
                raise RuntimeError("Payload retention tombstone count mismatch.")
            if quick_check != "ok" or foreign_key_errors:
                raise RuntimeError("Payload retention database validation failed.")
        return {
            "payload_count": len(payloads),
            "affected_call_count": sum(int(item.get("affected_call_count") or 0) for item in payloads),
            "payload_bytes": sum(int(item["original_bytes"] or 0) for item in payloads),
            "quick_check": quick_check,
            "foreign_key_errors": len(foreign_key_errors),
        }

    def full_call_retention_refs(self, cutoff_at: str) -> list[dict[str, Any]]:
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT id api_call_id, source_hash, payload_ref, occurred_at
                     FROM api_calls WHERE occurred_at < ? ORDER BY id""",
                (cutoff_at,),
            ).fetchall()
        return [dict(row) for row in rows]

    def apply_full_call_retention(
        self,
        *,
        cutoff_at: str,
        job_id: str,
        archive_entries: list[dict[str, Any]],
        archive_stat: os.stat_result | None,
    ) -> dict[str, int]:
        self.initialize()
        calls = self.full_call_retention_refs(cutoff_at)
        deleted_at = _utc_now()
        call_ids = [int(item["api_call_id"]) for item in calls]
        payload_refs = [str(item["payload_ref"]) for item in calls if item.get("payload_ref")]
        with self._connect() as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executemany(
                """INSERT OR IGNORE INTO call_retention_tombstones(
                       source_hash, occurred_at, deleted_at, delete_reason, retention_job_id
                   ) VALUES (?, ?, ?, 'capacity_limit', ?)""",
                [(item["source_hash"], item["occurred_at"], deleted_at, job_id) for item in calls],
            )
            if call_ids:
                connection.executemany("DELETE FROM api_calls WHERE id=?", [(call_id,) for call_id in call_ids])
            if payload_refs:
                connection.executemany("DELETE FROM payload_archive_index WHERE payload_ref=?", [(ref,) for ref in payload_refs])
                connection.executemany("DELETE FROM payload_retention_tombstones WHERE payload_ref=?", [(ref,) for ref in payload_refs])
            self._replace_archive_index(connection, archive_entries, archive_stat, deleted_at)
            connection.execute("DELETE FROM storage_retention_buckets WHERE bucket_end <= ?", (cutoff_at,))
        return {"call_count": len(calls), "payload_ref_count": len(payload_refs)}

    def _replace_archive_index(
        self,
        connection: sqlite3.Connection,
        archive_entries: list[dict[str, Any]],
        archive_stat: os.stat_result | None,
        indexed_at: str,
    ) -> None:
        if archive_stat is None or self.payload_path is None:
            return
        source_path = str(self.payload_path)
        connection.execute("DELETE FROM payload_archive_index")
        connection.execute("DELETE FROM payload_archive_cursors")
        connection.executemany(
            """INSERT INTO payload_archive_index(
                   payload_ref, source_path, source_inode, byte_offset, byte_length, indexed_at
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            [
                (
                    entry["payload_ref"], source_path, archive_stat.st_ino,
                    entry["byte_offset"], entry["byte_length"], indexed_at,
                )
                for entry in archive_entries
            ],
        )
        connection.execute(
            """INSERT INTO payload_archive_cursors(
                   source_path, source_inode, source_size, source_mtime_ns, last_offset, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(source_path) DO UPDATE SET source_inode=excluded.source_inode,
                   source_size=excluded.source_size, source_mtime_ns=excluded.source_mtime_ns,
                   last_offset=excluded.last_offset, updated_at=excluded.updated_at""",
            (source_path, archive_stat.st_ino, archive_stat.st_size, archive_stat.st_mtime_ns, archive_stat.st_size, indexed_at),
        )
        archive_max = int(connection.execute("SELECT COALESCE(MAX(rowid), 0) FROM payload_archive_index").fetchone()[0])
        connection.execute(
            """INSERT INTO storage_retention_progress(source, last_row_id, updated_at)
               VALUES ('archive', ?, ?)
               ON CONFLICT(source) DO UPDATE SET last_row_id=excluded.last_row_id, updated_at=excluded.updated_at""",
            (archive_max, indexed_at),
        )

    def migrate_output_semantics(
        self,
        *,
        max_records: int = 25,
        max_bytes: int = 32 * 1024 * 1024,
        max_seconds: float = 0.35,
    ) -> dict[str, int | bool]:
        """Upgrade historical call semantics in bounded, resumable batches."""
        self.initialize()
        if max_records < 1 or max_bytes < 1 or max_seconds <= 0:
            raise ValueError("Migration limits must be positive.")
        started = time.monotonic()
        migrated = 0
        inferred = 0
        skipped = 0
        processed_bytes = 0
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT c.id, c.status, c.output_tokens, c.output_semantics_version,
                          c.output_semantics_source, c.aggregation_source, c.payload_ref,
                          p.nonce, p.ciphertext,
                          i.source_path archive_source_path, i.source_inode archive_source_inode,
                          i.byte_offset archive_byte_offset, i.byte_length archive_byte_length
                   FROM api_calls c
                   LEFT JOIN call_payloads p ON p.api_call_id=c.id
                   LEFT JOIN payload_archive_index i ON i.payload_ref=c.payload_ref
                   WHERE COALESCE(c.output_semantics_version, 0) < ?
                   ORDER BY CASE WHEN p.api_call_id IS NOT NULL OR i.payload_ref IS NOT NULL THEN 1 ELSE 0 END, c.id
                   LIMIT ?""",
                (_OUTPUT_SEMANTICS_VERSION, max_records),
            ).fetchall()
            for row in rows:
                if migrated + inferred + skipped > 0 and (
                    processed_bytes >= max_bytes or time.monotonic() - started >= max_seconds
                ):
                    break
                envelope: dict[str, Any] | None = None
                envelope_bytes = 0
                if row["ciphertext"] is not None:
                    envelope = {"nonce": row["nonce"], "ciphertext": row["ciphertext"]}
                    envelope_bytes = len(str(row["ciphertext"]).encode("ascii", errors="ignore"))
                elif row["archive_source_path"] is not None and row["payload_ref"]:
                    try:
                        source_path = Path(str(row["archive_source_path"]))
                        source_stat = source_path.stat()
                        if source_stat.st_ino != int(row["archive_source_inode"]):
                            raise ValueError("Payload archive was rotated.")
                        envelope = read_payload_envelope(
                            source_path,
                            byte_offset=int(row["archive_byte_offset"]),
                            byte_length=int(row["archive_byte_length"]),
                            expected_payload_ref=str(row["payload_ref"]),
                        )
                        envelope_bytes = int(row["archive_byte_length"])
                    except (OSError, TypeError, ValueError, json.JSONDecodeError):
                        envelope = None
                if envelope is None:
                    source = _normalized_output_semantics_source({
                        "output_semantics_source": row["output_semantics_source"],
                        "aggregation_source": row["aggregation_source"],
                        "payload_schema_version": 1,
                    })
                    connection.execute(
                        """UPDATE api_calls SET output_semantics_version=?,
                               output_semantics_source=?, aggregation_source=? WHERE id=?""",
                        (_OUTPUT_SEMANTICS_VERSION, source, source, row["id"]),
                    )
                    inferred += 1
                    continue
                if self.payload_key is None:
                    break
                processed_bytes += envelope_bytes
                try:
                    payload = json.loads(decrypt_payload(self.payload_key, envelope))
                except Exception:
                    source = _normalized_output_semantics_source({
                        "output_semantics_source": row["output_semantics_source"],
                        "aggregation_source": row["aggregation_source"],
                        "payload_schema_version": 1,
                    })
                    connection.execute(
                        """UPDATE api_calls SET output_semantics_version=?,
                               output_semantics_source=?, aggregation_source=? WHERE id=?""",
                        (_OUTPUT_SEMANTICS_VERSION, source, source, row["id"]),
                    )
                    skipped += 1
                    continue
                if isinstance(payload, dict):
                    self._apply_payload_output_semantics(connection, row, payload)
                    migrated += 1
                else:
                    skipped += 1
            remaining = int(connection.execute(
                "SELECT COUNT(*) FROM api_calls WHERE COALESCE(output_semantics_version, 0) < ?",
                (_OUTPUT_SEMANTICS_VERSION,),
            ).fetchone()[0])
        return {
            "migrated": migrated,
            "inferred": inferred,
            "skipped": skipped,
            "processed_bytes": processed_bytes,
            "remaining": remaining,
            "complete": remaining == 0,
        }

    def output_semantics_migration_status(self) -> dict[str, int | bool]:
        self.initialize()
        with self._connect() as connection:
            remaining = int(connection.execute(
                "SELECT COUNT(*) FROM api_calls WHERE COALESCE(output_semantics_version, 0) < ?",
                (_OUTPUT_SEMANTICS_VERSION,),
            ).fetchone()[0])
        return {"remaining": remaining, "complete": remaining == 0}

    @staticmethod
    def _apply_payload_output_semantics(
        connection: sqlite3.Connection,
        call: sqlite3.Row,
        payload: dict[str, Any],
    ) -> None:
        semantics = _payload_output_semantics(
            payload,
            callback_status=str(call["status"] or "unknown"),
            output_tokens=_as_int(call["output_tokens"]),
        )
        existing_version = _as_int(call["output_semantics_version"]) or 0
        existing_source = str(call["output_semantics_source"] or "unknown")
        candidate_version = semantics["output_semantics_version"]
        candidate_source = semantics["output_semantics_source"]
        if candidate_version < existing_version:
            return
        if candidate_version == existing_version and (
            _OUTPUT_SEMANTICS_SOURCE_PRIORITY[candidate_source]
            < _OUTPUT_SEMANTICS_SOURCE_PRIORITY.get(existing_source, 0)
        ):
            return
        connection.execute(
            """UPDATE api_calls SET
                 provider_status=?,
                 stream_capture_state=?,
                 display_status=?,
                 output_item_count=?,
                 terminal_output_item_count=?,
                 tool_call_count=?,
                 output_semantics_version=?,
                 output_semantics_source=?,
                 aggregation_source=?,
                 stream_event_count=?,
                 stream_first_sequence=?,
                 stream_last_sequence=?,
                 stream_gap_count=?,
                 stream_text_delta_count=?,
                 stream_output_item_added_count=?,
                 stream_output_item_done_count=?,
                 stream_function_argument_delta_count=?,
                 stream_response_completed_count=?,
                 stream_has_terminal_event=?,
                 stream_capture_complete=?,
                 response_item_count=?,
                 visible_message_count=?,
                 reasoning_item_count=?,
                 refusal_count=?,
                 other_item_count=?
               WHERE id=?""",
            (
                semantics["provider_status"],
                semantics["stream_capture_state"],
                semantics["display_status"],
                semantics["output_item_count"],
                semantics["terminal_output_item_count"],
                semantics["tool_call_count"],
                semantics["output_semantics_version"],
                semantics["output_semantics_source"],
                semantics["aggregation_source"],
                semantics["stream_event_count"],
                semantics["stream_first_sequence"],
                semantics["stream_last_sequence"],
                semantics["stream_gap_count"],
                semantics["stream_text_delta_count"],
                semantics["stream_output_item_added_count"],
                semantics["stream_output_item_done_count"],
                semantics["stream_function_argument_delta_count"],
                semantics["stream_response_completed_count"],
                semantics["stream_has_terminal_event"],
                semantics["stream_capture_complete"],
                semantics["response_item_count"],
                semantics["visible_message_count"],
                semantics["reasoning_item_count"],
                semantics["refusal_count"],
                semantics["other_item_count"],
                call["id"],
            ),
        )

    def _insert_audit_record(self, connection: sqlite3.Connection, record: dict[str, Any]) -> bool:
        usage = record.get("usage")
        safe_metadata = {
            "audit_schema_version": record.get("audit_schema_version"),
            "routing_signals": record.get("routing_signals") if isinstance(record.get("routing_signals"), list) else [],
            "session_hash": record.get("session_hash"),
            "response_hash": record.get("response_hash"),
            "inherited_from": record.get("inherited_from"),
            "default_reason": record.get("default_reason"),
            "aggregation_source": record.get("aggregation_source"),
            "output_semantics_version": record.get("output_semantics_version"),
            "output_semantics_source": record.get("output_semantics_source"),
            "stream_event_count": record.get("stream_event_count"),
            "stream_first_sequence": record.get("stream_first_sequence"),
            "stream_last_sequence": record.get("stream_last_sequence"),
            "stream_gap_count": record.get("stream_gap_count"),
            "stream_text_delta_count": record.get("stream_text_delta_count"),
            "stream_output_item_added_count": record.get("stream_output_item_added_count"),
            "stream_output_item_done_count": record.get("stream_output_item_done_count"),
            "stream_function_argument_delta_count": record.get("stream_function_argument_delta_count"),
            "stream_response_completed_count": record.get("stream_response_completed_count"),
            "stream_has_terminal_event": record.get("stream_has_terminal_event"),
            "stream_capture_complete": record.get("stream_capture_complete"),
            "call_kind": record.get("call_kind"),
            "subagent_kind": record.get("subagent_kind"),
            "thread_source": record.get("thread_source"),
            "parent_thread_id": record.get("parent_thread_id"),
            "parent_turn_id": record.get("parent_turn_id"),
            "internal_call": record.get("internal_call"),
            "reviewed_action_type": record.get("reviewed_action_type"),
            "guardian_context_reduction": record.get("guardian_context_reduction"),
        }
        source = {
            "timestamp": record.get("timestamp"),
            "response_hash": record.get("response_hash"),
            "logical_turn_id": record.get("logical_turn_id"),
            "status": record.get("status"),
            "selected_model": record.get("selected_model"),
            "duration_ms": record.get("duration_ms"),
            "error_type": record.get("error_type"),
        }
        source_hash = hashlib.sha256(_json(source).encode("utf-8")).hexdigest()
        if connection.execute(
            "SELECT 1 FROM call_retention_tombstones WHERE source_hash=?",
            (source_hash,),
        ).fetchone() is not None:
            return False
        occurred_at = _parse_timestamp(record.get("timestamp"))
        if occurred_at is None:
            occurred_at = datetime.now(UTC)
        pricing = self._pricing_for(connection, record, occurred_at)
        input_tokens = _usage_value(usage, "input_tokens", "prompt_tokens")
        output_tokens = _usage_value(usage, "output_tokens", "completion_tokens")
        input_cost, output_cost = self._costs_for(
            pricing,
            input_tokens,
            output_tokens,
            _usage_value(usage, "cache_read_input_tokens", "cached_tokens"),
        )
        reported_cost = _money_to_microusd(record.get("response_cost"))
        total_cost = (input_cost or 0) + (output_cost or 0) if pricing else reported_cost
        cost_status = "priced" if pricing else "provider_reported" if reported_cost is not None else "unpriced"
        try:
            cursor = connection.execute(
                """INSERT INTO api_calls(
                     source_hash, occurred_at, status, provider_status, stream_capture_state, display_status,
                     output_item_count, terminal_output_item_count, tool_call_count, output_semantics_version,
                     output_semantics_source, aggregation_source,
                     stream_event_count, stream_first_sequence, stream_last_sequence, stream_gap_count,
                     stream_text_delta_count, stream_output_item_added_count, stream_output_item_done_count,
                     stream_function_argument_delta_count, stream_response_completed_count,
                     stream_has_terminal_event, stream_capture_complete,
                     response_item_count, visible_message_count, reasoning_item_count, refusal_count,
                     other_item_count, logical_turn_id, prompt_fingerprint, request_kind, call_kind,
                     subagent_kind, thread_source, parent_thread_id, parent_turn_id, internal_call,
                     reviewed_action_type, requested_alias, selected_alias, selected_model, provider_model, response_model,
                     selected_tier, reasoning_effort, routing_mode, routing_cause, route_score, duration_ms,
                     input_tokens, output_tokens, cache_read_tokens, error_category, error_type,
                     policy_version, payload_ref, input_cost_microusd, output_cost_microusd, total_cost_microusd,
                     reported_cost_microusd, currency, pricing_rule_id, pricing_version, cost_status,
                     safe_metadata_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    source_hash,
                    occurred_at.isoformat(),
                    str(record.get("status") or "unknown"),
                    self._text(record.get("provider_status")),
                    self._text(record.get("stream_capture_state")),
                    self._text(record.get("display_status")),
                    _as_int(record.get("output_item_count")),
                    _as_int(record.get("terminal_output_item_count")),
                    _as_int(record.get("tool_call_count")),
                    _as_int(record.get("output_semantics_version")) or 0,
                    self._text(record.get("output_semantics_source")),
                    self._text(record.get("aggregation_source")),
                    _as_int(record.get("stream_event_count")),
                    _as_int(record.get("stream_first_sequence")),
                    _as_int(record.get("stream_last_sequence")),
                    _as_int(record.get("stream_gap_count")),
                    _as_int(record.get("stream_text_delta_count")),
                    _as_int(record.get("stream_output_item_added_count")),
                    _as_int(record.get("stream_output_item_done_count")),
                    _as_int(record.get("stream_function_argument_delta_count")),
                    _as_int(record.get("stream_response_completed_count")),
                    _as_int(record.get("stream_has_terminal_event")),
                    _as_int(record.get("stream_capture_complete")),
                    _as_int(record.get("response_item_count")),
                    _as_int(record.get("visible_message_count")),
                    _as_int(record.get("reasoning_item_count")),
                    _as_int(record.get("refusal_count")),
                    _as_int(record.get("other_item_count")),
                    self._text(record.get("logical_turn_id")),
                    self._text(record.get("prompt_fingerprint")),
                    self._text(record.get("request_kind")),
                    self._text(record.get("call_kind")),
                    self._text(record.get("subagent_kind")),
                    self._text(record.get("thread_source")),
                    self._text(record.get("parent_thread_id")),
                    self._text(record.get("parent_turn_id")),
                    1 if record.get("internal_call") is True else 0 if record.get("internal_call") is False else None,
                    self._text(record.get("reviewed_action_type")),
                    self._text(record.get("requested_alias") or record.get("requested_model")),
                    self._text(record.get("selected_alias")),
                    self._text(record.get("selected_model")),
                    self._text(record.get("provider_model")),
                    self._text(record.get("response_model")),
                    self._text(record.get("selected_tier")),
                    self._text(record.get("reasoning_effort")),
                    self._text(record.get("routing_mode")),
                    self._text(record.get("routing_cause") or record.get("default_reason")),
                    record.get("routing_score") if isinstance(record.get("routing_score"), (int, float)) else None,
                    _as_int(record.get("duration_ms")),
                    input_tokens,
                    output_tokens,
                    _usage_value(usage, "cache_read_input_tokens", "cached_tokens"),
                    _error_category(record),
                    self._text(record.get("error_type")),
                    self._text(record.get("policy_version")),
                    self._text(record.get("payload_ref")),
                    input_cost,
                    output_cost,
                    total_cost,
                    reported_cost,
                    pricing["currency"] if pricing else "USD",
                    pricing["id"] if pricing else None,
                    pricing["version"] if pricing else None,
                    cost_status,
                    _json(safe_metadata),
                ),
            )
            self._write_billing_details(
                connection,
                call_id=int(cursor.lastrowid),
                record=record,
                occurred_at=occurred_at,
                provider_reported_cost_microusd=reported_cost,
            )
        except sqlite3.IntegrityError:
            return False
        return True

    @staticmethod
    def _costs_for(pricing: sqlite3.Row | None, input_tokens: int | None, output_tokens: int | None, cached_input_tokens: int | None = None) -> tuple[int | None, int | None]:
        if pricing is None:
            return None, None
        cached_tokens = min(max(0, input_tokens or 0), max(0, cached_input_tokens or 0))
        uncached_tokens = max(0, input_tokens or 0) - cached_tokens
        cached_rate = pricing["cached_input_microusd_per_million"]
        cached_rate = int(cached_rate) if cached_rate is not None else int(pricing["input_microusd_per_million"])
        input_cost = (
            (uncached_tokens * int(pricing["input_microusd_per_million"]))
            + (cached_tokens * cached_rate)
        ) // 1_000_000
        output_cost = (max(0, output_tokens or 0) * int(pricing["output_microusd_per_million"])) // 1_000_000
        return input_cost, output_cost

    @staticmethod
    def _pricing_for(connection: sqlite3.Connection, record: dict[str, Any], occurred_at: datetime) -> sqlite3.Row | None:
        candidates = [
            str(record.get(key) or "").lower()
            for key in ("selected_model", "selected_alias", "provider_model", "response_model")
        ]
        candidates = [value for value in candidates if value]
        if not candidates:
            return None
        for rule in connection.execute(
            "SELECT * FROM pricing_rules WHERE effective_at <= ? ORDER BY effective_at DESC, id DESC",
            (occurred_at.isoformat(),),
        ).fetchall():
            pattern = str(rule["model_pattern"]).lower()
            if any(value == pattern or value.endswith(pattern) for value in candidates):
                return rule
        return None

    @staticmethod
    def _text(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text[:500] if text else None

    def recent_calls(
        self,
        *,
        limit: int = 100,
        status: str | None = None,
        model: str | None = None,
        start_at: str | None = None,
        end_at: str | None = None,
        sort_by: str = "occurred_at",
        sort_order: str = "desc",
    ) -> list[dict[str, Any]]:
        self.initialize()
        where, params = self._call_filters(status=status, model=model, start_at=start_at, end_at=end_at)
        order_column = {
            "occurred_at": "occurred_at",
            "model": "selected_model",
            "tier": "selected_tier",
            "routing_cause": "routing_cause",
            "input_tokens": "input_tokens",
            "output_tokens": "output_tokens",
            "total_tokens": "COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)",
            "duration_ms": "duration_ms",
            "amount": "total_cost_microusd",
            "status": "status",
        }.get(sort_by, "occurred_at")
        direction = "ASC" if sort_order.lower() == "asc" else "DESC"
        params.append(max(1, min(limit, 500)))
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM api_calls {where} ORDER BY {order_column} {direction}, id DESC LIMIT ?", params
            ).fetchall()
        return [self._call_row(row) for row in rows]

    def turns(
        self,
        *,
        limit: int = 100,
        start_at: str | None = None,
        end_at: str | None = None,
        sort_by: str = "total_tokens",
        sort_order: str = "desc",
    ) -> list[dict[str, Any]]:
        self.initialize()
        where, params = self._call_filters(start_at=start_at, end_at=end_at)
        order_column = {
            "finished_at": "finished_at",
            "model": "selected_model",
            "call_count": "call_count",
            "input_tokens": "input_tokens",
            "output_tokens": "output_tokens",
            "total_tokens": "total_tokens",
            "duration_ms": "duration_ms_total",
            "amount": "total_cost_microusd",
        }.get(sort_by, "total_tokens")
        direction = "ASC" if sort_order.lower() == "asc" else "DESC"
        params.append(max(1, min(limit, 500)))
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT COALESCE(logical_turn_id, source_hash) AS turn_id,
                          MIN(occurred_at) AS started_at, MAX(occurred_at) AS finished_at,
                          COUNT(*) AS call_count,
                          SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success_count,
                          SUM(CASE WHEN status='failure' THEN 1 ELSE 0 END) AS failure_count,
                          SUM(COALESCE(duration_ms, 0)) AS duration_ms_total,
                          SUM(COALESCE(input_tokens, 0)) AS input_tokens,
                          SUM(COALESCE(output_tokens, 0)) AS output_tokens,
                          SUM(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)) AS total_tokens,
                          SUM(COALESCE(total_cost_microusd, 0)) AS total_cost_microusd,
                          MAX(selected_model) AS selected_model,
                          MAX(selected_alias) AS selected_alias,
                          MAX(selected_tier) AS selected_tier,
                          MAX(routing_cause) AS routing_cause
                   FROM api_calls {where} GROUP BY COALESCE(logical_turn_id, source_hash)
                   ORDER BY {order_column} {direction}, finished_at DESC LIMIT ?""",
                params,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["status"] = "success" if item["success_count"] else "failure"
            result.append(item)
        return result

    def tasks(
        self,
        *,
        limit: int = 100,
        cursor: str | None = None,
        start_at: str | None = None,
        end_at: str | None = None,
        sort_by: str = "last_activity_at",
        sort_order: str = "desc",
    ) -> dict[str, Any]:
        self.initialize()
        where, params = self._call_filters(start_at=start_at, end_at=end_at)
        order_column = {
            "last_activity_at": "last_activity_at",
            "started_at": "started_at",
            "model": "primary_model",
            "complexity": "complexity",
            "call_count": "call_count",
            "input_tokens": "input_tokens",
            "output_tokens": "output_tokens",
            "total_tokens": "total_tokens",
            "duration_ms": "duration_ms_total",
            "actual_amount": "actual_cost_microusd",
            "estimated_amount": "estimated_cost_microusd",
            "status": "failure_count",
        }.get(sort_by, "last_activity_at")
        direction = "ASC" if sort_order.lower() == "asc" else "DESC"
        page_limit = max(1, min(limit, 200))
        offset = _decode_cursor(cursor)
        with self._connect() as connection:
            rows = connection.execute(
                f"""WITH filtered AS (
                           SELECT *, {_TASK_KEY_SQL} AS task_key, {_TASK_KIND_SQL} AS task_kind,
                                  {_MODEL_SQL} AS model_label, {_COMPLEXITY_SQL} AS complexity_label
                           FROM api_calls {where}
                       ), ranked AS (
                           SELECT *,
                                  ROW_NUMBER() OVER (
                                      PARTITION BY task_key ORDER BY occurred_at DESC, id DESC
                                  ) AS latest_rank,
                                  ROW_NUMBER() OVER (
                                      PARTITION BY task_key
                                      ORDER BY CASE
                                          WHEN routing_cause IS NOT NULL AND TRIM(routing_cause) <> '' THEN 0
                                          ELSE 1
                                      END, occurred_at DESC, id DESC
                                  ) AS routing_rank
                           FROM filtered
                       ), summaries AS (
                           SELECT task_key, MAX(task_kind) AS task_kind, MAX(logical_turn_id) AS logical_turn_id,
                                  MIN(occurred_at) AS started_at, MAX(occurred_at) AS last_activity_at,
                                  COUNT(*) AS call_count,
                                  SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success_count,
                                  SUM(CASE WHEN status='failure' THEN 1 ELSE 0 END) AS failure_count,
                                  SUM(COALESCE(duration_ms, 0)) AS duration_ms_total,
                                  SUM(COALESCE(input_tokens, 0)) AS input_tokens,
                                  SUM(COALESCE(output_tokens, 0)) AS output_tokens,
                                  SUM(COALESCE(cache_read_tokens, 0)) AS cache_read_tokens,
                                  SUM(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)) AS total_tokens,
                                  COUNT(DISTINCT model_label) AS model_variant_count,
                                  COUNT(DISTINCT complexity_label) AS complexity_variant_count,
                                  SUM(CASE WHEN reported_cost_microusd IS NOT NULL THEN 1 ELSE 0 END) AS actual_cost_call_count,
                                  SUM(CASE WHEN reported_cost_microusd IS NOT NULL THEN COALESCE(reported_cost_microusd, 0) ELSE 0 END) AS actual_cost_microusd,
                                  SUM(CASE WHEN cost_status='priced' THEN 1 ELSE 0 END) AS estimated_cost_call_count,
                                  SUM(CASE WHEN cost_status='priced' THEN COALESCE(total_cost_microusd, 0) ELSE 0 END) AS estimated_cost_microusd,
                                  SUM(CASE WHEN cost_status='unpriced' THEN 1 ELSE 0 END) AS unpriced_call_count,
                                  MAX(currency) AS currency,
                                  MAX(CASE WHEN latest_rank=1 THEN model_label END) AS primary_model,
                                  MAX(CASE WHEN latest_rank=1 THEN complexity_label END) AS complexity,
                                  MAX(CASE WHEN routing_rank=1 AND routing_cause IS NOT NULL
                                           AND TRIM(routing_cause) <> '' THEN routing_cause END) AS routing_cause
                           FROM ranked GROUP BY task_key
                       )
                       SELECT *, COUNT(*) OVER () AS total_count FROM summaries
                       ORDER BY {order_column} {direction}, last_activity_at DESC, task_key ASC
                       LIMIT ? OFFSET ?""",
                [*params, page_limit, offset],
            ).fetchall()
            total = int(rows[0]["total_count"]) if rows else 0
            if not rows and offset:
                total = int(connection.execute(
                    f"SELECT COUNT(DISTINCT {_TASK_KEY_SQL}) FROM api_calls {where}",
                    params,
                ).fetchone()[0])

        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item.pop("total_count", None)
            item["status"] = _task_status(int(item["success_count"]), int(item["failure_count"]))
            items.append(_apply_cost_state(item))
        next_offset = offset + len(items)
        return {
            "items": items,
            "next_cursor": _encode_cursor(next_offset) if next_offset < total else None,
            "total": total,
        }

    def task_detail(self, task_key: str) -> dict[str, Any] | None:
        self.initialize()
        if task_key.startswith("logical:") and task_key.removeprefix("logical:"):
            where = "logical_turn_id=?"
            params = (task_key.removeprefix("logical:"),)
            task_kind = "logical_turn"
        elif task_key.startswith("standalone:") and task_key.removeprefix("standalone:"):
            where = "source_hash=? AND (logical_turn_id IS NULL OR TRIM(logical_turn_id)='')"
            params = (task_key.removeprefix("standalone:"),)
            task_kind = "standalone_call"
        else:
            return None
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM api_calls WHERE {where} ORDER BY occurred_at ASC, id ASC",
                params,
            ).fetchall()
        if not rows:
            return None

        calls = [self._call_row(row) for row in rows]
        models = [
            call.get("selected_alias") or call.get("selected_model") or call.get("provider_model") or "unknown"
            for call in calls
        ]
        complexities = [
            call.get("selected_tier") or call.get("reasoning_effort") or "unreported"
            for call in calls
        ]
        success_count = sum(call.get("status") == "success" for call in calls)
        failure_count = sum(call.get("status") == "failure" for call in calls)
        actual_calls = [call for call in calls if call.get("reported_cost_microusd") is not None]
        estimated_calls = [call for call in calls if call.get("cost_status") == "priced"]
        unpriced_count = sum(call.get("cost_status") == "unpriced" for call in calls)
        summary = {
            "task_key": task_key,
            "task_kind": task_kind,
            "logical_turn_id": calls[0].get("logical_turn_id"),
            "started_at": calls[0]["occurred_at"],
            "last_activity_at": calls[-1]["occurred_at"],
            "call_count": len(calls),
            "success_count": success_count,
            "failure_count": failure_count,
            "status": _task_status(success_count, failure_count),
            "duration_ms_total": sum(call.get("duration_ms") or 0 for call in calls),
            "input_tokens": sum(call.get("input_tokens") or 0 for call in calls),
            "output_tokens": sum(call.get("output_tokens") or 0 for call in calls),
            "cache_read_tokens": sum(call.get("cache_read_tokens") or 0 for call in calls),
            "total_tokens": sum((call.get("input_tokens") or 0) + (call.get("output_tokens") or 0) for call in calls),
            "primary_model": models[-1],
            "complexity": complexities[-1],
            "routing_cause": next((call.get("routing_cause") for call in reversed(calls) if call.get("routing_cause")), None),
            "models": sorted(set(models)),
            "complexities": sorted(set(complexities)),
            "model_variant_count": len(set(models)),
            "complexity_variant_count": len(set(complexities)),
            "actual_cost_call_count": len(actual_calls),
            "actual_cost_microusd": sum(call.get("reported_cost_microusd") or 0 for call in actual_calls),
            "estimated_cost_call_count": len(estimated_calls),
            "estimated_cost_microusd": sum(call.get("total_cost_microusd") or 0 for call in estimated_calls),
            "unpriced_call_count": unpriced_count,
            "currency": next((call.get("currency") for call in reversed(calls) if call.get("currency")), "USD"),
        }
        _apply_cost_state(summary)
        return {
            "summary": summary,
            "calls": list(reversed(calls)),
            "calls_order": "occurred_at_desc",
        }

    def routing_stats(
        self,
        *,
        limit: int = 200,
        start_at: str | None = None,
        end_at: str | None = None,
        sort_by: str = "call_count",
        sort_order: str = "desc",
    ) -> list[dict[str, Any]]:
        self.initialize()
        where, params = self._call_filters(start_at=start_at, end_at=end_at)
        order_column = {
            "model": "model",
            "complexity": "complexity",
            "task_count": "task_count",
            "call_count": "call_count",
            "input_tokens": "input_tokens",
            "output_tokens": "output_tokens",
            "total_tokens": "total_tokens",
            "duration_ms": "average_duration_ms",
            "actual_amount": "actual_cost_microusd",
            "estimated_amount": "estimated_cost_microusd",
        }.get(sort_by, "call_count")
        direction = "ASC" if sort_order.lower() == "asc" else "DESC"
        page_limit = max(1, min(limit, 500))
        with self._connect() as connection:
            rows = connection.execute(
                f"""WITH filtered AS (
                           SELECT *, {_TASK_KEY_SQL} AS task_key,
                                  {_MODEL_SQL} AS model, {_COMPLEXITY_SQL} AS complexity
                           FROM api_calls {where}
                       )
                       SELECT model, complexity,
                              COUNT(DISTINCT task_key) AS task_count,
                              COUNT(*) AS call_count,
                              SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success_count,
                              SUM(CASE WHEN status='failure' THEN 1 ELSE 0 END) AS failure_count,
                              SUM(COALESCE(input_tokens, 0)) AS input_tokens,
                              SUM(COALESCE(output_tokens, 0)) AS output_tokens,
                              SUM(COALESCE(cache_read_tokens, 0)) AS cache_read_tokens,
                              SUM(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)) AS total_tokens,
                              ROUND(AVG(duration_ms), 1) AS average_duration_ms,
                              SUM(CASE WHEN reported_cost_microusd IS NOT NULL THEN 1 ELSE 0 END) AS actual_cost_call_count,
                              SUM(CASE WHEN reported_cost_microusd IS NOT NULL THEN COALESCE(reported_cost_microusd, 0) ELSE 0 END) AS actual_cost_microusd,
                              SUM(CASE WHEN cost_status='priced' THEN 1 ELSE 0 END) AS estimated_cost_call_count,
                              SUM(CASE WHEN cost_status='priced' THEN COALESCE(total_cost_microusd, 0) ELSE 0 END) AS estimated_cost_microusd,
                              SUM(CASE WHEN cost_status='unpriced' THEN 1 ELSE 0 END) AS unpriced_call_count,
                              MAX(currency) AS currency
                       FROM filtered GROUP BY model, complexity
                       ORDER BY {order_column} {direction}, model ASC, complexity ASC LIMIT ?""",
                [*params, page_limit],
            ).fetchall()
            reason_rows = connection.execute(
                f"""WITH filtered AS (
                           SELECT {_TASK_KEY_SQL} AS task_key, {_MODEL_SQL} AS model,
                                  {_COMPLEXITY_SQL} AS complexity,
                                  COALESCE(NULLIF(TRIM(routing_cause), ''), 'unreported') AS routing_cause
                           FROM api_calls {where}
                       )
                       SELECT model, complexity, routing_cause, COUNT(*) AS call_count,
                              COUNT(DISTINCT task_key) AS task_count
                       FROM filtered GROUP BY model, complexity, routing_cause
                       ORDER BY call_count DESC, routing_cause ASC""",
                params,
            ).fetchall()

        reasons: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in reason_rows:
            key = (str(row["model"]), str(row["complexity"]))
            reasons.setdefault(key, []).append({
                "routing_cause": row["routing_cause"],
                "call_count": row["call_count"],
                "task_count": row["task_count"],
            })
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["success_rate"] = (
                round(int(item["success_count"]) / int(item["call_count"]), 4)
                if item["call_count"] else None
            )
            item["routing_reasons"] = reasons.get((str(item["model"]), str(item["complexity"])), [])
            items.append(_apply_cost_state(item))
        return items

    def call_detail(self, call_id: int) -> dict[str, Any] | None:
        """Return call metadata without decrypting captured request or response content."""
        self.initialize()
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM api_calls WHERE id=?", (call_id,)).fetchone()
            if row is None:
                return None
            result = self._call_row(row)
            payload = connection.execute(
                "SELECT captured_at FROM call_payloads WHERE api_call_id=?", (call_id,)
            ).fetchone()
            archive = connection.execute(
                "SELECT indexed_at FROM payload_archive_index WHERE payload_ref=?",
                (result.get("payload_ref"),),
            ).fetchone() if result.get("payload_ref") else None
        result["payload"] = self._payload_availability(
            payload,
            archive,
            payload_ref=result.get("payload_ref"),
        )
        result["counts"] = {
            key: result.get(key)
            for key in (
                "response_item_count", "visible_message_count", "reasoning_item_count",
                "refusal_count", "tool_call_count", "other_item_count", "terminal_output_item_count",
            )
        }
        result["usage"] = {
            key: result.get(key)
            for key in (
                "input_tokens", "output_tokens", "reasoning_tokens", "cached_input_tokens", "cache_write_tokens",
            )
        }
        result["cost"] = {
            key: result.get(key)
            for key in (
                "actual_incremental_cost_microusd", "api_equivalent_cost_microusd",
                "reported_cost_microusd", "total_cost_microusd", "currency", "cost_status",
            )
        }
        sections = result["payload"].get("sections", {}) if isinstance(result["payload"], dict) else {}
        result["advanced_available"] = {
            "raw_request": bool(sections.get("raw_request") or sections.get("input")),
            "terminal_response": bool(sections.get("terminal_response")),
            "normalized_output": bool(sections.get("normalized_output") or sections.get("output_items")),
            "stream_events": bool(sections.get("stream_events")),
        }
        return result

    def call_payload_section(
        self,
        call_id: int,
        section: str,
        *,
        cursor: int = 0,
        limit: int = 200,
        origin: str = "all",
        event_type: str = "all",
    ) -> dict[str, Any] | None:
        """Decrypt a single explicitly requested payload section for one call."""
        if section not in {
            "user-input",
            "input-context",
            "classified-input",
            "tool-results",
            "input",
            "raw-request",
            "visible-output",
            "output-items",
            "normalized-output",
            "terminal-response",
            "stream-events",
        }:
            raise ValueError("Unknown payload section.")
        payload = self._load_detail_payload(call_id)
        if payload is None:
            return None
        if payload.get("state") != "available":
            return payload
        if section in {"user-input", "input-context"}:
            input_sections = _request_input_sections(payload["request"])
            if section == "user-input":
                return {
                    "state": "available",
                    "captured_at": payload["captured_at"],
                    "source": payload.get("source"),
                    "payload_schema_version": payload["payload_schema_version"],
                    "user_messages": input_sections["user_messages"],
                    "request_metadata": input_sections["request_metadata"],
                }
            return {
                "state": "available",
                "captured_at": payload["captured_at"],
                "source": payload.get("source"),
                "payload_schema_version": payload["payload_schema_version"],
                "context_messages": input_sections["context_messages"],
                "tool_results": input_sections["tool_results"],
                "request_metadata": input_sections["request_metadata"],
            }
        if section == "classified-input":
            if origin not in _INPUT_ORIGINS | {"all", "runtime"}:
                raise ValueError("Unknown input origin.")
            classified_items = [
                item for item in _classified_request_input(payload["request"])
                if _matches_input_origin(item, origin)
            ]
            if origin == "human":
                classified_items.reverse()
            page_size = max(1, min(limit, 200))
            page_cursor = max(0, cursor)
            items = classified_items[page_cursor:page_cursor + page_size]
            next_cursor = page_cursor + len(items)
            return {
                "state": "available",
                "captured_at": payload["captured_at"],
                "source": payload.get("source"),
                "payload_schema_version": payload["payload_schema_version"],
                "origin": origin,
                "items": items,
                "cursor": page_cursor,
                "next_cursor": next_cursor if next_cursor < len(classified_items) else None,
                "total_item_count": len(classified_items),
            }
        if section == "tool-results":
            tool_results = [
                item for item in _classified_request_input(payload["request"])
                if item.get("origin") == "function_call_output"
            ]
            page_size = max(1, min(limit, 200))
            page_cursor = max(0, cursor)
            items = tool_results[page_cursor:page_cursor + page_size]
            next_cursor = page_cursor + len(items)
            return {
                "state": "available",
                "captured_at": payload["captured_at"],
                "source": payload.get("source"),
                "payload_schema_version": payload["payload_schema_version"],
                "items": items,
                "cursor": page_cursor,
                "next_cursor": next_cursor if next_cursor < len(tool_results) else None,
                "total_item_count": len(tool_results),
            }
        if section in {"input", "raw-request"}:
            return {
                "state": "available",
                "captured_at": payload["captured_at"],
                "source": payload.get("source"),
                "payload_schema_version": payload["payload_schema_version"],
                "request": payload["request"],
            }
        if section == "visible-output":
            return {
                "state": "available",
                "captured_at": payload["captured_at"],
                "source": payload.get("source"),
                "payload_schema_version": payload["payload_schema_version"],
                "visible_output_text": payload["output_text"],
                "tool_calls": payload["tool_calls"],
                "aggregation_source": payload["aggregation_source"],
            }
        if section in {"output-items", "normalized-output"}:
            return {
                "state": "available",
                "captured_at": payload["captured_at"],
                "source": payload.get("source"),
                "payload_schema_version": payload["payload_schema_version"],
                "normalized_output_items": payload["normalized_output_items"],
                "output_text": payload["output_text"],
                "tool_calls": payload["tool_calls"],
                "aggregation_source": payload["aggregation_source"],
            }
        if section == "terminal-response":
            return {
                "state": "available",
                "captured_at": payload["captured_at"],
                "source": payload.get("source"),
                "payload_schema_version": payload["payload_schema_version"],
                "terminal_response": payload["terminal_response"],
            }
        allowed_event_types = {"all", "lifecycle", "reasoning", "text_delta", "output_item", "function_call", "error", "unknown"}
        if event_type not in allowed_event_types:
            raise ValueError("Unknown stream event type.")
        stream_events = payload["stream_events"]
        filtered_events = [
            event for event in stream_events
            if event_type == "all" or _stream_event_category(event) == event_type
        ]
        page_size = max(1, min(limit, 500))
        page_cursor = max(0, cursor)
        events = filtered_events[page_cursor:page_cursor + page_size]
        next_cursor = page_cursor + len(events)
        return {
            "state": "available",
            "captured_at": payload["captured_at"],
            "source": payload.get("source"),
            "payload_schema_version": payload["payload_schema_version"],
            "event_type": event_type,
            "events": events,
            "cursor": page_cursor,
            "next_cursor": next_cursor if next_cursor < len(filtered_events) else None,
            "total_event_count": len(stream_events),
            "filtered_event_count": len(filtered_events),
        }

    def call_export(self, call_id: int, *, level: str) -> dict[str, Any] | None:
        if level not in {"summary", "debug"}:
            raise ValueError("Unknown export level.")
        summary = self.call_detail(call_id)
        if summary is None:
            return None
        summary_keys = (
            "id", "occurred_at", "status", "provider_status", "display_status", "call_kind",
            "requested_alias", "selected_alias", "selected_model", "routing_cause", "duration_ms",
            "aggregation_source", "output_semantics_source", "stream_capture_state", "counts", "usage", "cost",
        )
        export = {
            "export_schema_version": 1,
            "level": level,
            "call": {key: summary.get(key) for key in summary_keys},
        }
        if level == "summary":
            return redact_sensitive_value(export)
        payload = self._load_detail_payload(call_id)
        if payload is None:
            return None
        export["debug"] = {
            "state": payload.get("state"),
            "captured_at": payload.get("captured_at"),
            "source": payload.get("source"),
            "payload_schema_version": payload.get("payload_schema_version"),
        }
        if payload.get("state") == "available":
            export["debug"].update({
                "raw_request": payload.get("request"),
                "terminal_response": payload.get("terminal_response"),
                "normalized_output_items": payload.get("normalized_output_items"),
                "visible_output_text": payload.get("output_text"),
                "tool_calls": payload.get("tool_calls"),
                "stream_events": payload.get("stream_events"),
            })
        return redact_sensitive_value(export)

    def _load_detail_payload(self, call_id: int) -> dict[str, Any] | None:
        self.initialize()
        with self._connect() as connection:
            call = connection.execute("SELECT payload_ref FROM api_calls WHERE id=?", (call_id,)).fetchone()
            if call is None:
                return None
            row = connection.execute(
                "SELECT nonce, ciphertext, captured_at FROM call_payloads WHERE api_call_id=?", (call_id,)
            ).fetchone()
            archive = connection.execute(
                """SELECT source_path, source_inode, byte_offset, byte_length, indexed_at
                   FROM payload_archive_index WHERE payload_ref=?""",
                (call["payload_ref"],),
            ).fetchone() if call["payload_ref"] else None
            tombstone = connection.execute(
                """SELECT deleted_at, delete_reason, retention_job_id
                     FROM payload_retention_tombstones WHERE payload_ref=?""",
                (call["payload_ref"],),
            ).fetchone() if call["payload_ref"] else None
        if tombstone is not None:
            payload = {
                "state": "retention_deleted",
                "captured_at": tombstone["deleted_at"],
                "source": "retention",
                "delete_reason": tombstone["delete_reason"],
                "retention_job_id": tombstone["retention_job_id"],
            }
        elif row is not None:
            payload = self._decrypt_detail_payload(row, source="database")
        elif archive is not None and call["payload_ref"]:
            if self.payload_key is None:
                payload = {"state": "key_unavailable", "captured_at": archive["indexed_at"], "source": "encrypted_log"}
            else:
                try:
                    source_path = Path(str(archive["source_path"]))
                    stat = source_path.stat()
                    if stat.st_ino != int(archive["source_inode"]):
                        raise ValueError("Payload archive was rotated.")
                    envelope = read_payload_envelope(
                        source_path,
                        byte_offset=int(archive["byte_offset"]),
                        byte_length=int(archive["byte_length"]),
                        expected_payload_ref=str(call["payload_ref"]),
                    )
                    payload = self._decrypt_detail_payload(
                        {**envelope, "captured_at": archive["indexed_at"]},
                        source="encrypted_log",
                    )
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    payload = {"state": "unavailable", "captured_at": archive["indexed_at"], "source": "encrypted_log"}
        else:
            payload = self._payload_availability(None, None, payload_ref=call["payload_ref"])
        return payload

    def pricing_rules(self) -> list[dict[str, Any]]:
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM pricing_rules ORDER BY model_pattern COLLATE NOCASE"
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_pricing_rule(
        self,
        *,
        model_pattern: str,
        input_microusd_per_million: int,
        output_microusd_per_million: int,
    ) -> dict[str, Any]:
        self.initialize()
        normalized = model_pattern.strip().lower()
        if not normalized:
            raise ValueError("model_pattern is required")
        with self._connect() as connection:
            version = f"local-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
            effective_from = _utc_now()
            cached_rate = round(input_microusd_per_million * 0.1)
            cache_write_rate = round(input_microusd_per_million * 1.25)
            connection.execute(
                """INSERT INTO pricing_rules(model_pattern, currency, input_microusd_per_million,
                   cached_input_microusd_per_million, cache_write_microusd_per_million,
                   long_context_threshold, long_context_input_multiplier, long_context_output_multiplier,
                   output_microusd_per_million, version, effective_at)
                   VALUES (?, 'USD', ?, ?, ?, 272000, 2, 1.5, ?, ?, ?)
                   ON CONFLICT(model_pattern) DO UPDATE SET currency='USD',
                     input_microusd_per_million=excluded.input_microusd_per_million,
                     cached_input_microusd_per_million=excluded.cached_input_microusd_per_million,
                     cache_write_microusd_per_million=excluded.cache_write_microusd_per_million,
                     long_context_threshold=excluded.long_context_threshold,
                     long_context_input_multiplier=excluded.long_context_input_multiplier,
                     long_context_output_multiplier=excluded.long_context_output_multiplier,
                     output_microusd_per_million=excluded.output_microusd_per_million,
                     version=excluded.version, effective_at=excluded.effective_at""",
                (normalized, input_microusd_per_million, cached_rate, cache_write_rate, output_microusd_per_million, version, effective_from),
            )
            for context_class, input_multiplier, output_multiplier in (
                (ContextClass.SHORT, 1, 1),
                (ContextClass.LONG, 2, 1.5),
            ):
                connection.execute(
                    """INSERT INTO pricing_rule_versions(
                           catalog_version, model_pattern, service_tier, context_class,
                           regional_processing, currency, input_microusd_per_million,
                           cached_input_microusd_per_million, cache_write_microusd_per_million,
                           output_microusd_per_million, source, effective_from, created_at
                       ) VALUES (?, ?, 'standard', ?, 'none', 'USD', ?, ?, ?, ?, 'user_override', ?, ?)""",
                    (
                        version,
                        normalized,
                        context_class.value,
                        round(input_microusd_per_million * input_multiplier),
                        round(cached_rate * input_multiplier),
                        round(cache_write_rate * input_multiplier),
                        round(output_microusd_per_million * output_multiplier),
                        effective_from,
                        effective_from,
                    ),
                )
            self._reprice_unpriced_calls(connection)
            row = connection.execute("SELECT * FROM pricing_rules WHERE model_pattern=?", (normalized,)).fetchone()
        return dict(row)

    def _reprice_unpriced_calls(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute("SELECT * FROM api_calls WHERE cost_status IN ('unpriced', 'priced')").fetchall()
        for row in rows:
            record = dict(row)
            occurred_at = _parse_timestamp(record.get("occurred_at")) or datetime.now(UTC)
            pricing = self._pricing_for(connection, record, occurred_at)
            if pricing is None:
                continue
            input_cost, output_cost = self._costs_for(pricing, record.get("input_tokens"), record.get("output_tokens"), record.get("cache_read_tokens"))
            connection.execute(
                """UPDATE api_calls SET input_cost_microusd=?, output_cost_microusd=?, total_cost_microusd=?,
                   currency=?, pricing_rule_id=?, pricing_version=?, cost_status='priced' WHERE id=?""",
                (input_cost, output_cost, (input_cost or 0) + (output_cost or 0), pricing["currency"], pricing["id"], pricing["version"], row["id"]),
            )

    @staticmethod
    def _cost_filters(
        *,
        start_at: str | None = None,
        end_at: str | None = None,
        model: str | None = None,
        billing_mode: str | None = None,
        estimate_status: str | None = None,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for clause, value in (
            ("occurred_at >= ?", start_at),
            ("occurred_at <= ?", end_at),
            ("billing_model = ?", model),
            ("billing_mode = ?", billing_mode),
            ("api_equivalent_status = ?", estimate_status),
        ):
            if value:
                clauses.append(clause)
                params.append(value)
        return (f"WHERE {' AND '.join(clauses)}" if clauses else "", params)

    def cost_summary(
        self,
        *,
        start_at: str,
        end_at: str,
        model: str | None = None,
        billing_mode: str | None = None,
        estimate_status: str | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        where, params = self._cost_filters(
            start_at=start_at,
            end_at=end_at,
            model=model,
            billing_mode=billing_mode,
            estimate_status=estimate_status,
        )
        with self._connect() as connection:
            row = connection.execute(
                f"""SELECT COUNT(*) AS call_count,
                           SUM(CASE WHEN actual_incremental_cost_microusd IS NOT NULL THEN 1 ELSE 0 END) AS actual_known_call_count,
                           SUM(COALESCE(actual_incremental_cost_microusd, 0)) AS actual_incremental_cost_microusd,
                           SUM(CASE WHEN api_equivalent_cost_microusd IS NOT NULL THEN 1 ELSE 0 END) AS complete_estimate_call_count,
                           SUM(COALESCE(api_equivalent_cost_microusd, 0)) AS api_equivalent_cost_microusd,
                           SUM(CASE WHEN known_api_equivalent_cost_microusd IS NOT NULL THEN 1 ELSE 0 END) AS known_estimate_call_count,
                           SUM(COALESCE(known_api_equivalent_cost_microusd, 0)) AS known_api_equivalent_cost_microusd,
                           SUM(CASE WHEN provider_reported_cost_microusd IS NOT NULL THEN 1 ELSE 0 END) AS provider_reported_call_count,
                           SUM(COALESCE(provider_reported_cost_microusd, 0)) AS provider_reported_cost_microusd,
                           SUM(COALESCE(sol_baseline_cost_microusd, 0)) AS sol_baseline_cost_microusd,
                           SUM(COALESCE(sol_equivalent_savings_microusd, 0)) AS sol_equivalent_savings_microusd,
                           SUM(CASE WHEN api_equivalent_status='usage_missing' THEN 1 ELSE 0 END) AS usage_missing_call_count,
                           SUM(CASE WHEN api_equivalent_status='usage_inconsistent' THEN 1 ELSE 0 END) AS usage_inconsistent_call_count,
                           SUM(CASE WHEN api_equivalent_status='billing_model_unknown' THEN 1 ELSE 0 END) AS model_unknown_call_count,
                           SUM(CASE WHEN api_equivalent_status='model_unpriced' THEN 1 ELSE 0 END) AS model_unpriced_call_count,
                           SUM(CASE WHEN api_equivalent_status='tool_pricing_incomplete' THEN 1 ELSE 0 END) AS tool_incomplete_call_count,
                           SUM(CASE WHEN api_equivalent_status='legacy_partial' THEN 1 ELSE 0 END) AS legacy_partial_call_count
                    FROM api_calls {where}""",
                params,
            ).fetchone()
        result = dict(row)
        result.update({"start_at": start_at, "end_at": end_at, "currency": "USD", "pricing_basis": PRICING_BASIS})
        call_count = int(result.get("call_count") or 0)
        result["estimate_coverage"] = round(int(result.get("complete_estimate_call_count") or 0) / call_count, 4) if call_count else None
        result["known_estimate_coverage"] = round(int(result.get("known_estimate_call_count") or 0) / call_count, 4) if call_count else None
        result["actual_coverage"] = round(int(result.get("actual_known_call_count") or 0) / call_count, 4) if call_count else None
        known_count = int(result.get("known_estimate_call_count") or 0)
        complete_count = int(result.get("complete_estimate_call_count") or 0)
        result["display_api_equivalent_cost_microusd"] = (
            int(result.get("known_api_equivalent_cost_microusd") or 0)
            if known_count
            else None
        )
        result["estimate_display_status"] = (
            "complete"
            if call_count and complete_count == call_count
            else "partial"
            if known_count
            else "unavailable"
        )
        return result

    def cost_timeline(
        self,
        *,
        start_at: str,
        end_at: str,
        timezone_name: str,
        bucket: str = "auto",
        model: str | None = None,
        billing_mode: str | None = None,
        estimate_status: str | None = None,
    ) -> list[dict[str, Any]]:
        self.initialize()
        try:
            timezone_value = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as error:
            raise ValueError("Unknown timezone.") from error
        start = _parse_timestamp(start_at)
        end = _parse_timestamp(end_at)
        if start is None or end is None or start > end:
            raise ValueError("Invalid cost timeline range.")
        duration = end - start
        if bucket == "auto":
            bucket = "5m" if duration <= timedelta(hours=2) else "hour" if duration <= timedelta(days=2) else "day" if duration <= timedelta(days=90) else "week"
        if bucket not in {"5m", "hour", "day", "week"}:
            raise ValueError("Unsupported cost timeline bucket.")
        where, params = self._cost_filters(
            start_at=start_at,
            end_at=end_at,
            model=model,
            billing_mode=billing_mode,
            estimate_status=estimate_status,
        )
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT occurred_at, actual_incremental_cost_microusd,
                           api_equivalent_cost_microusd, known_api_equivalent_cost_microusd,
                           provider_reported_cost_microusd, input_tokens, cache_read_tokens,
                           cache_write_tokens, output_tokens
                    FROM api_calls {where} ORDER BY occurred_at ASC""",
                params,
            ).fetchall()
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            occurred = _parse_timestamp(row["occurred_at"])
            if occurred is None:
                continue
            local = occurred.astimezone(timezone_value)
            if bucket == "5m":
                local = local.replace(minute=(local.minute // 5) * 5, second=0, microsecond=0)
            elif bucket == "hour":
                local = local.replace(minute=0, second=0, microsecond=0)
            elif bucket == "day":
                local = local.replace(hour=0, minute=0, second=0, microsecond=0)
            else:
                local = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
            key = local.isoformat()
            item = grouped.setdefault(key, {
                "bucket_start": key,
                "call_count": 0,
                "actual_known_call_count": 0,
                "complete_estimate_call_count": 0,
                "actual_incremental_cost_microusd": 0,
                "api_equivalent_cost_microusd": 0,
                "known_api_equivalent_cost_microusd": 0,
                "provider_reported_cost_microusd": 0,
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "cache_write_tokens": 0,
                "output_tokens": 0,
            })
            item["call_count"] += 1
            if row["actual_incremental_cost_microusd"] is not None:
                item["actual_known_call_count"] += 1
            if row["api_equivalent_cost_microusd"] is not None:
                item["complete_estimate_call_count"] += 1
            for field in (
                "actual_incremental_cost_microusd",
                "api_equivalent_cost_microusd",
                "known_api_equivalent_cost_microusd",
                "provider_reported_cost_microusd",
                "input_tokens",
                "output_tokens",
                "cache_write_tokens",
            ):
                item[field] += int(row[field] or 0)
            item["cached_input_tokens"] += int(row["cache_read_tokens"] or 0)
        return list(grouped.values())

    def cost_models(
        self,
        *,
        start_at: str,
        end_at: str,
        billing_mode: str | None = None,
        estimate_status: str | None = None,
    ) -> list[dict[str, Any]]:
        self.initialize()
        where, params = self._cost_filters(
            start_at=start_at,
            end_at=end_at,
            billing_mode=billing_mode,
            estimate_status=estimate_status,
        )
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT COALESCE(billing_model, 'unknown') AS model,
                           COUNT(*) AS call_count,
                           SUM(COALESCE(input_tokens, 0) - COALESCE(cache_read_tokens, 0) - COALESCE(cache_write_tokens, 0)) AS regular_input_tokens,
                           SUM(COALESCE(cache_read_tokens, 0)) AS cached_input_tokens,
                           SUM(COALESCE(cache_write_tokens, 0)) AS cache_write_tokens,
                           SUM(COALESCE(output_tokens, 0)) AS output_tokens,
                           SUM(CASE WHEN api_equivalent_cost_microusd IS NOT NULL THEN 1 ELSE 0 END) AS complete_estimate_call_count,
                           SUM(CASE WHEN known_api_equivalent_cost_microusd IS NOT NULL THEN 1 ELSE 0 END) AS known_estimate_call_count,
                           SUM(COALESCE(api_equivalent_cost_microusd, 0)) AS api_equivalent_cost_microusd,
                           SUM(COALESCE(known_api_equivalent_cost_microusd, 0)) AS known_api_equivalent_cost_microusd,
                           SUM(COALESCE(actual_incremental_cost_microusd, 0)) AS actual_incremental_cost_microusd,
                           SUM(COALESCE(provider_reported_cost_microusd, 0)) AS provider_reported_cost_microusd,
                           SUM(COALESCE(sol_baseline_cost_microusd, 0)) AS sol_baseline_cost_microusd,
                           SUM(COALESCE(sol_equivalent_savings_microusd, 0)) AS sol_equivalent_savings_microusd
                    FROM api_calls {where}
                    GROUP BY COALESCE(billing_model, 'unknown')
                    ORDER BY known_api_equivalent_cost_microusd DESC, call_count DESC""",
                params,
            ).fetchall()
        total_cost = sum(int(row["known_api_equivalent_cost_microusd"] or 0) for row in rows)
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            call_count = int(item["call_count"] or 0)
            cost = int(item["known_api_equivalent_cost_microusd"] or 0)
            item["display_api_equivalent_cost_microusd"] = cost if item["known_estimate_call_count"] else None
            item["average_api_equivalent_cost_microusd"] = round(cost / call_count) if call_count else None
            item["cost_share"] = round(cost / total_cost, 4) if total_cost else None
            item["estimate_coverage"] = round(int(item["complete_estimate_call_count"] or 0) / call_count, 4) if call_count else None
            item["known_estimate_coverage"] = round(int(item["known_estimate_call_count"] or 0) / call_count, 4) if call_count else None
            result.append(item)
        return result

    def cost_calls(
        self,
        *,
        start_at: str,
        end_at: str,
        limit: int = 100,
        cursor: str | None = None,
        model: str | None = None,
        billing_mode: str | None = None,
        estimate_status: str | None = None,
        sort_by: str = "occurred_at",
        sort_order: str = "desc",
    ) -> dict[str, Any]:
        self.initialize()
        where, params = self._cost_filters(
            start_at=start_at,
            end_at=end_at,
            model=model,
            billing_mode=billing_mode,
            estimate_status=estimate_status,
        )
        order_column = {
            "occurred_at": "occurred_at",
            "api_equivalent_cost": "api_equivalent_cost_microusd",
            "actual_incremental_cost": "actual_incremental_cost_microusd",
            "model": "billing_model",
        }.get(sort_by, "occurred_at")
        direction = "ASC" if sort_order.lower() == "asc" else "DESC"
        page_limit = max(1, min(limit, 200))
        offset = _decode_cursor(cursor)
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT id, occurred_at, billing_model, billing_mode, context_class,
                           input_tokens, cache_read_tokens, cache_write_tokens, output_tokens,
                           builtin_tool_cost_microusd, actual_incremental_cost_microusd,
                           known_api_equivalent_cost_microusd, api_equivalent_cost_microusd,
                           api_equivalent_status, COUNT(*) OVER () AS total_count
                    FROM api_calls {where}
                    ORDER BY {order_column} {direction}, id {direction} LIMIT ? OFFSET ?""",
                [*params, page_limit, offset],
            ).fetchall()
            total = int(rows[0]["total_count"]) if rows else 0
            if not rows and offset:
                total = int(connection.execute(f"SELECT COUNT(*) FROM api_calls {where}", params).fetchone()[0])
        items = []
        for row in rows:
            item = dict(row)
            item.pop("total_count", None)
            items.append(item)
        next_offset = offset + len(items)
        return {"items": items, "total": total, "next_cursor": _encode_cursor(next_offset) if next_offset < total else None}

    def cost_call_detail(self, call_id: int) -> dict[str, Any] | None:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM api_calls WHERE id=?", (call_id,)).fetchone()
            if row is None:
                return None
            items = connection.execute(
                "SELECT * FROM call_cost_items WHERE api_call_id=? ORDER BY id ASC", (call_id,)
            ).fetchall()
        result = self._call_row(row)
        legacy_partial = result.get("api_equivalent_status") == EstimateStatus.LEGACY_PARTIAL.value
        cost_items: list[dict[str, Any]] = []
        assumptions: list[dict[str, Any]] = []
        for item_row in items:
            item = dict(item_row)
            quantity_missing = item.get("quantity") is None
            assumed_zero = legacy_partial and quantity_missing and item.get("item_type") in {
                "cached_input",
                "cache_write",
            }
            item["quantity_status"] = "unknown" if quantity_missing else "observed"
            item["amount_status"] = (
                "assumed_zero_for_partial_estimate"
                if assumed_zero
                else "unknown" if item.get("amount_microusd") is None
                else "calculated"
            )
            item["assumed_quantity"] = 0 if assumed_zero else None
            if assumed_zero:
                assumptions.append({
                    "code": f"{item['item_type']}_tokens_missing_assumed_zero",
                    "item_type": item["item_type"],
                    "observed_quantity": None,
                    "assumed_quantity": 0,
                    "unit": item.get("unit") or "tokens",
                    "affects": "known_api_equivalent_cost_microusd",
                })
            cost_items.append(item)
        result["cost_items"] = cost_items
        result["applied_rates"] = json.loads(result["applied_rates_json"]) if result.get("applied_rates_json") else None
        result["billing_warnings"] = json.loads(result["billing_warnings_json"]) if result.get("billing_warnings_json") else []
        result["billing_warning_codes"] = [
            _BILLING_WARNING_CODES.get(warning, "unclassified_billing_warning")
            for warning in result["billing_warnings"]
        ]
        result["billing_assumptions"] = assumptions
        result["estimate_disclosure"] = (
            "partial_estimate_missing_cache_assumed_zero"
            if assumptions
            else "complete_estimate" if result.get("api_equivalent_cost_microusd") is not None
            else "partial_or_unavailable_estimate"
        )
        return result

    def pricing_catalog(self) -> list[dict[str, Any]]:
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM pricing_rule_versions
                   ORDER BY model_pattern, context_class, source, effective_from DESC"""
            ).fetchall()
        return [dict(row) for row in rows]

    def _decrypt_detail_payload(
        self,
        row: Mapping[str, Any] | None,
        *,
        source: str = "database",
    ) -> dict[str, Any]:
        if row is None:
            return {"state": "not_captured"}
        if self.payload_key is None:
            return {"state": "key_unavailable", "captured_at": row["captured_at"], "source": source}
        try:
            envelope = {"nonce": row["nonce"], "ciphertext": row["ciphertext"]}
            payload_format = row.get("format") if hasattr(row, "get") else None
            if payload_format is not None:
                envelope["format"] = payload_format
            payload = json.loads(decrypt_payload(self.payload_key, envelope))
        except Exception:
            return {"state": "unavailable", "captured_at": row["captured_at"], "source": source}
        terminal_response = payload.get("terminal_response") or payload.get("response") or {}
        normalized_output_items = payload.get("normalized_output_items")
        if not isinstance(normalized_output_items, list):
            output = terminal_response.get("output") if isinstance(terminal_response, dict) else None
            normalized_output_items = output if isinstance(output, list) else []
        output_text = payload.get("visible_output_text") or payload.get("output_text")
        if not isinstance(output_text, str) or not output_text:
            output_text = _derive_output_text(normalized_output_items)
        tool_calls = payload.get("tool_calls")
        if not isinstance(tool_calls, list):
            tool_calls = [
                item for item in normalized_output_items
                if isinstance(item, dict) and item.get("type") == "function_call"
            ]
        stream_events = payload.get("stream_events")
        return {
            "state": "available",
            "captured_at": row["captured_at"],
            "source": source,
            "payload_schema_version": payload.get("payload_schema_version", 1),
            "request": payload.get("request"),
            "terminal_response": terminal_response,
            "normalized_output_items": normalized_output_items,
            "output_text": output_text,
            "tool_calls": tool_calls,
            "stream_events": stream_events if isinstance(stream_events, list) else [],
            "aggregation_source": _normalized_output_semantics_source(payload),
        }

    def _payload_availability(
        self,
        row: Mapping[str, Any] | None,
        archive: Mapping[str, Any] | None = None,
        *,
        payload_ref: str | None = None,
    ) -> dict[str, Any]:
        source = "database" if row is not None else "encrypted_log" if archive is not None else None
        captured_at = row["captured_at"] if row is not None else archive["indexed_at"] if archive is not None else None
        if source is None:
            return {"state": "captured_pending" if payload_ref else "not_captured"}
        if self.payload_key is None:
            return {"state": "key_unavailable", "captured_at": captured_at, "source": source}
        return {
            "state": "available",
            "captured_at": captured_at,
            "source": source,
            "sections": {
                "user_input": True,
                "input_context": True,
                "input": True,
                "classified_input": True,
                "tool_results": True,
                "raw_request": True,
                "visible_output": True,
                "output_items": True,
                "normalized_output": True,
                "terminal_response": True,
                "stream_events": True,
            },
        }

    @staticmethod
    def _call_filters(
        *,
        status: str | None = None,
        model: str | None = None,
        start_at: str | None = None,
        end_at: str | None = None,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if model:
            clauses.append("(selected_model=? OR selected_alias=? OR provider_model=?)")
            params.extend([model, model, model])
        if start_at:
            clauses.append("occurred_at >= ?")
            params.append(start_at)
        if end_at:
            clauses.append("occurred_at <= ?")
            params.append(end_at)
        return (f"WHERE {' AND '.join(clauses)}" if clauses else "", params)

    def daily_metrics(self, *, days: int = 30) -> list[dict[str, Any]]:
        self.initialize()
        day_count = max(1, min(days, 365))
        today = datetime.now(UTC).date()
        first_day = today - timedelta(days=day_count - 1)
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT substr(occurred_at, 1, 10) AS day,
                          COUNT(*) AS call_count,
                          COUNT(DISTINCT COALESCE(logical_turn_id, source_hash)) AS turn_count,
                          SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success_count,
                          SUM(CASE WHEN status='failure' THEN 1 ELSE 0 END) AS failure_count,
                          SUM(COALESCE(input_tokens, 0)) AS input_tokens,
                          SUM(COALESCE(output_tokens, 0)) AS output_tokens,
                          SUM(COALESCE(cache_read_tokens, 0)) AS cache_read_tokens,
                          ROUND(AVG(duration_ms), 1) AS average_duration_ms
                   FROM api_calls WHERE occurred_at >= ?
                   GROUP BY substr(occurred_at, 1, 10) ORDER BY day ASC""",
                (first_day.isoformat(),),
            ).fetchall()
        metrics_by_day = {str(row["day"]): dict(row) for row in rows}
        empty_metrics = {
            "call_count": 0,
            "turn_count": 0,
            "success_count": 0,
            "failure_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "average_duration_ms": 0.0,
        }
        return [
            metrics_by_day.get(
                (first_day + timedelta(days=offset)).isoformat(),
                {"day": (first_day + timedelta(days=offset)).isoformat(), **empty_metrics},
            )
            for offset in range(day_count)
        ]

    def weekly_metrics(self, *, weeks: int = 12) -> list[dict[str, Any]]:
        self.initialize()
        cutoff = (datetime.now(UTC) - timedelta(days=max(1, min(weeks, 52)) * 7)).date().isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT strftime('%Y-W%W', occurred_at) AS week,
                          COUNT(*) AS call_count,
                          COUNT(DISTINCT COALESCE(logical_turn_id, source_hash)) AS turn_count,
                          SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success_count,
                          SUM(CASE WHEN status='failure' THEN 1 ELSE 0 END) AS failure_count,
                          SUM(COALESCE(input_tokens, 0)) AS input_tokens,
                          SUM(COALESCE(output_tokens, 0)) AS output_tokens
                   FROM api_calls WHERE occurred_at >= ?
                   GROUP BY strftime('%Y-W%W', occurred_at) ORDER BY week ASC""",
                (cutoff,),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_event(
        self,
        *,
        event_type: str,
        component: str,
        severity: str = "info",
        request_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.initialize()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO service_events(occurred_at, event_type, component, severity, request_id, details_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (_utc_now(), event_type, component, severity, request_id, _json(details or {})),
            )

    def recent_events(self, *, limit: int = 100) -> list[dict[str, Any]]:
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM service_events ORDER BY occurred_at DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [
            {**dict(row), "details": json.loads(row["details_json"])}
            for row in rows
        ]

    def record_quota_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        self.initialize()
        raw_windows = snapshot.get("windows") if isinstance(snapshot.get("windows"), list) else []
        windows: dict[str, dict[str, Any]] = {}
        for window in raw_windows:
            if not isinstance(window, dict):
                continue
            window_key = str(window.get("key") or window.get("window_key") or "unknown")
            windows[window_key] = window
        summary = self._safe_persisted_summary(snapshot.get("summary"))
        usage = snapshot.get("usage") if isinstance(snapshot.get("usage"), dict) else {}
        reset_credits = snapshot.get("reset_credits") if isinstance(snapshot.get("reset_credits"), dict) else {}
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO quota_snapshots(profile_id, observed_at, source_status, error_code, error_message, raw_summary_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    snapshot.get("profile_id"),
                    snapshot.get("observed_at") or _utc_now(),
                    snapshot.get("status") or "unavailable",
                    snapshot.get("error_code"),
                    self._text(snapshot.get("error_message")),
                    _json(summary),
                ),
            )
            snapshot_id = int(cursor.lastrowid)
            for window_key, window in windows.items():
                connection.execute(
                    """INSERT INTO quota_windows(
                           snapshot_id, window_key, label, remaining_percent, reset_at, used_percent,
                           limit_name, limit_id, window_kind, window_duration_mins
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        snapshot_id,
                        window_key,
                        str(window.get("label") or "Unknown"),
                        window.get("remaining_percent"),
                        window.get("reset_at"),
                        window.get("used_percent"),
                        self._text(window.get("limit_name")),
                        self._text(window.get("limit_id")),
                        self._text(window.get("window_kind")),
                        _as_int(window.get("window_duration_mins")),
                    ),
                )
            usage_summary = usage.get("summary") if isinstance(usage.get("summary"), dict) else {}
            connection.execute(
                """INSERT INTO account_usage_snapshots(
                       quota_snapshot_id, lifetime_tokens, peak_daily_tokens, current_streak_days,
                       longest_streak_days, longest_running_turn_sec
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    snapshot_id,
                    _as_int(usage_summary.get("lifetimeTokens")),
                    _as_int(usage_summary.get("peakDailyTokens")),
                    _as_int(usage_summary.get("currentStreakDays")),
                    _as_int(usage_summary.get("longestStreakDays")),
                    _as_int(usage_summary.get("longestRunningTurnSec")),
                ),
            )
            daily = usage.get("daily_usage_buckets")
            if isinstance(daily, list):
                for bucket in daily:
                    if not isinstance(bucket, dict) or not isinstance(bucket.get("start_date"), str):
                        continue
                    connection.execute(
                        "INSERT OR REPLACE INTO account_usage_daily(quota_snapshot_id, start_date, tokens) VALUES (?, ?, ?)",
                        (snapshot_id, bucket["start_date"], _as_int(bucket.get("tokens")) or 0),
                    )
            connection.execute(
                """INSERT INTO quota_reset_credits(
                       quota_snapshot_id, available_count, observed_at, source, count_source,
                       detail_lower_bound, details_state, returned_detail_count,
                       missing_detail_count, contract_warning
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot_id,
                    _as_int(reset_credits.get("available_count")),
                    reset_credits.get("observed_at") or snapshot.get("observed_at") or _utc_now(),
                    str(reset_credits.get("source") or "account/rateLimits/read"),
                    self._text(reset_credits.get("count_source")),
                    _as_int(reset_credits.get("detail_lower_bound")),
                    self._text(reset_credits.get("details_state")),
                    _as_int(reset_credits.get("returned_detail_count")),
                    _as_int(reset_credits.get("missing_detail_count")),
                    self._text(reset_credits.get("contract_warning")),
                ),
            )
            details = reset_credits.get("details")
            if isinstance(details, list):
                for ordinal, detail in enumerate(details):
                    if not isinstance(detail, dict):
                        continue
                    connection.execute(
                        """INSERT INTO quota_reset_credit_details(
                               quota_snapshot_id, ordinal, status, reset_type, granted_at,
                               expires_at, title, description
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            snapshot_id,
                            ordinal,
                            self._text(detail.get("status")),
                            self._text(detail.get("reset_type")),
                            self._text(detail.get("granted_at")),
                            self._text(detail.get("expires_at")),
                            self._text(detail.get("title")),
                            self._text(detail.get("description")),
                        ),
                    )
            self._upsert_account_profile(connection, snapshot, summary)
        return {"id": snapshot_id, **snapshot}

    def latest_quota(self) -> dict[str, Any] | None:
        self.initialize()
        with self._connect() as connection:
            snapshot = connection.execute(
                "SELECT * FROM quota_snapshots ORDER BY observed_at DESC, id DESC LIMIT 1"
            ).fetchone()
            if snapshot is None:
                return None
            windows = connection.execute(
                """SELECT window_key, label, remaining_percent, reset_at, used_percent, limit_name,
                          limit_id, window_kind, window_duration_mins
                   FROM quota_windows WHERE snapshot_id=?""",
                (snapshot["id"],),
            ).fetchall()
            usage_summary = connection.execute(
                "SELECT * FROM account_usage_snapshots WHERE quota_snapshot_id=?",
                (snapshot["id"],),
            ).fetchone()
            usage_daily = connection.execute(
                "SELECT start_date, tokens FROM account_usage_daily WHERE quota_snapshot_id=? ORDER BY start_date",
                (snapshot["id"],),
            ).fetchall()
            reset_credits = connection.execute(
                """SELECT available_count, observed_at, source, count_source, detail_lower_bound,
                          details_state, returned_detail_count, missing_detail_count, contract_warning
                   FROM quota_reset_credits WHERE quota_snapshot_id=?""",
                (snapshot["id"],),
            ).fetchone()
            reset_details = connection.execute(
                """SELECT status, reset_type, granted_at, expires_at, title, description
                   FROM quota_reset_credit_details WHERE quota_snapshot_id=? ORDER BY ordinal""",
                (snapshot["id"],),
            ).fetchall()
            profile = connection.execute(
                "SELECT * FROM account_profiles WHERE id=?",
                (snapshot["profile_id"],),
            ).fetchone() if snapshot["profile_id"] else None
        usage = {
            "summary": {
                "lifetimeTokens": usage_summary["lifetime_tokens"] if usage_summary else None,
                "peakDailyTokens": usage_summary["peak_daily_tokens"] if usage_summary else None,
                "currentStreakDays": usage_summary["current_streak_days"] if usage_summary else None,
                "longestStreakDays": usage_summary["longest_streak_days"] if usage_summary else None,
                "longestRunningTurnSec": usage_summary["longest_running_turn_sec"] if usage_summary else None,
            },
            "daily_usage_buckets": [dict(row) for row in usage_daily],
        }
        persisted_reset_credits = dict(reset_credits) if reset_credits else {
            "available_count": json.loads(snapshot["raw_summary_json"]).get("available_reset_credits"),
            "count_source": "legacy_snapshot",
            "detail_lower_bound": 0,
            "details_state": "count_only",
            "returned_detail_count": 0,
            "missing_detail_count": None,
            "observed_at": snapshot["observed_at"],
            "source": "legacy_snapshot",
            "contract_warning": None,
        }
        persisted_reset_credits["details"] = [dict(row) for row in reset_details]
        return {
            "profile_id": snapshot["profile_id"],
            "observed_at": snapshot["observed_at"],
            "status": snapshot["source_status"],
            "error_code": snapshot["error_code"],
            "error_message": snapshot["error_message"],
            "summary": json.loads(snapshot["raw_summary_json"]),
            "windows": [dict(row) for row in windows],
            "usage": usage,
            "account": self._profile_view(profile),
            "reset_credits": persisted_reset_credits,
        }

    def clear_account_profile(self) -> None:
        self.initialize()
        with self._connect() as connection:
            connection.execute("UPDATE account_profiles SET is_active=0, status='logged_out', updated_at=?", (_utc_now(),))

    @staticmethod
    def _ensure_columns(connection: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
        existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    @staticmethod
    def _safe_persisted_summary(value: Any) -> dict[str, Any]:
        summary = dict(value) if isinstance(value, dict) else {}
        summary.pop("account_label", None)
        return summary

    @staticmethod
    def _mask_account_identifier(name: Any) -> str | None:
        if not isinstance(name, str) or not name.strip():
            return None
        value = name.strip()
        if "@" in value or "*" in value or value == "ChatGPT 订阅账户":
            return None
        letters = "".join(character for character in value if character.isalnum())
        if not letters:
            return None
        return f"{letters[:2].lower()}****"

    @staticmethod
    def _profile_view(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        display_name = row["display_name"]
        return {
            "state": row["status"],
            "account_type": row["account_type"],
            "name": display_name,
            "account_identifier": DashboardStore._mask_account_identifier(display_name),
            "email_masked": row["email_masked"],
            "plan_type": row["plan_type"],
            "requires_openai_auth": bool(row["requires_openai_auth"]) if row["requires_openai_auth"] is not None else None,
            "last_verified_at": row["last_verified_at"],
            "last_error_code": row["last_error_code"],
            "last_error_message": row["last_error_message"],
            "source": row["source"],
            "email_verified": bool(row["email_verified"]) if row["email_verified"] is not None else None,
            "identity_expires_at": row["identity_expires_at"],
        }

    def _upsert_account_profile(
        self,
        connection: sqlite3.Connection,
        snapshot: dict[str, Any],
        summary: dict[str, Any],
    ) -> None:
        account = snapshot.get("account") if isinstance(snapshot.get("account"), dict) else {}
        profile_id = snapshot.get("profile_id") or "chatgpt-default"
        state = account.get("state") or summary.get("account_state") or "unknown"
        connection.execute(
            """INSERT INTO account_profiles(
                   id, display_name, auth_mode, plan_type, is_active, status, updated_at,
                   account_type, email_masked, email_fingerprint, requires_openai_auth,
                   last_verified_at, last_error_code, last_error_message, source,
                   account_id_fingerprint, email_verified, identity_expires_at
               ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   display_name=excluded.display_name, auth_mode=excluded.auth_mode,
                   plan_type=excluded.plan_type, is_active=1, status=excluded.status,
                   updated_at=excluded.updated_at, account_type=excluded.account_type,
                   email_masked=COALESCE(excluded.email_masked, account_profiles.email_masked),
                   email_fingerprint=COALESCE(excluded.email_fingerprint, account_profiles.email_fingerprint),
                   requires_openai_auth=excluded.requires_openai_auth,
                   last_verified_at=excluded.last_verified_at,
                   last_error_code=excluded.last_error_code,
                   last_error_message=excluded.last_error_message, source=excluded.source,
                   account_id_fingerprint=COALESCE(excluded.account_id_fingerprint, account_profiles.account_id_fingerprint),
                   email_verified=excluded.email_verified, identity_expires_at=excluded.identity_expires_at""",
            (
                profile_id,
                account.get("name") or account.get("email_masked") or "ChatGPT 订阅账户",
                summary.get("auth_mode") or "unknown",
                summary.get("plan_type"),
                state,
                _utc_now(),
                account.get("account_type"),
                account.get("email_masked"),
                account.get("fingerprint"),
                1 if summary.get("requires_openai_auth") else 0,
                snapshot.get("observed_at"),
                snapshot.get("error_code"),
                self._text(snapshot.get("error_message")),
                account.get("identity_source") or "app_server",
                account.get("account_id_fingerprint"),
                1 if account.get("email_verified") is True else 0 if account.get("email_verified") is False else None,
                account.get("identity_expires_at"),
            ),
        )

    def start_redemption(self, *, idempotency_key: str, profile_id: str | None, credit_id: str | None) -> bool:
        self.initialize()
        with self._connect() as connection:
            try:
                connection.execute(
                    """INSERT INTO reset_redemptions(idempotency_key, profile_id, credit_id, created_at)
                       VALUES (?, ?, ?, ?)""",
                    (idempotency_key, profile_id, None, _utc_now()),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def finish_redemption(self, *, idempotency_key: str, outcome: str, error_code: str | None = None) -> None:
        self.initialize()
        with self._connect() as connection:
            connection.execute(
                """UPDATE reset_redemptions SET outcome=?, error_code=?, completed_at=? WHERE idempotency_key=?""",
                (outcome, error_code, _utc_now(), idempotency_key),
            )

    def health(self) -> dict[str, Any]:
        self.initialize()
        with self._connect() as connection:
            count = connection.execute("SELECT COUNT(*) FROM api_calls").fetchone()[0]
        return {
            "database_path": str(self.database_path),
            "integrity": self._schedule_integrity_check() if self._integrity_check_enabled else "deferred",
            "call_count": int(count),
        }

    def _schedule_integrity_check(self) -> str:
        with self._health_lock:
            if (
                self._integrity_thread is None
                and time.monotonic() - self._integrity_checked_at >= 300
            ):
                self._integrity_thread = threading.Thread(
                    target=self._run_integrity_check,
                    name="routerd-integrity-check",
                    daemon=True,
                )
                self._integrity_thread.start()
            return self._integrity_result

    def _run_integrity_check(self) -> None:
        try:
            with self._connect() as connection:
                result = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        except sqlite3.Error:
            result = "unavailable"
        with self._health_lock:
            self._integrity_result = result
            self._integrity_checked_at = time.monotonic()
            self._integrity_thread = None

    @staticmethod
    def _call_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["safe_metadata"] = json.loads(result.pop("safe_metadata_json"))
        return result

    @contextmanager
    def exclusive_database_maintenance(self, *, timeout: float = 30.0) -> Iterator[None]:
        """Wait for ordinary connections to close, then block new ones during replacement."""
        with self._database_access.exclusive(timeout=timeout):
            yield

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with self._database_access.shared():
            connection = sqlite3.connect(self.database_path)
            connection.row_factory = sqlite3.Row
            try:
                with connection:
                    yield connection
            finally:
                connection.close()
