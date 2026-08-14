from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from codexthrift.content_capture import encrypt_payload
from codexthrift.dashboard_store import (
    DashboardStore,
    _apply_cost_state,
    _parse_timestamp,
)


def test_initialize_performs_schema_work_once_per_store(tmp_path: Path, monkeypatch) -> None:
    store = DashboardStore(tmp_path / "routerllm.sqlite3")
    original = store._initialize_once
    calls = 0

    def initialize_once() -> None:
        nonlocal calls
        calls += 1
        original()

    monkeypatch.setattr(store, "_initialize_once", initialize_once)

    store.initialize()
    store.initialize()

    assert calls == 1


def test_store_connection_context_closes_connection(tmp_path: Path) -> None:
    store = DashboardStore(tmp_path / "routerllm.sqlite3")
    store.initialize()

    with store._connect() as connection:
        assert connection.execute("SELECT 1").fetchone()[0] == 1

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")


def test_exclusive_database_maintenance_waits_and_blocks_new_connections(tmp_path: Path) -> None:
    store = DashboardStore(tmp_path / "routerllm.sqlite3")
    store.initialize()
    reader_started = threading.Event()
    release_reader = threading.Event()
    writer_acquired = threading.Event()
    release_writer = threading.Event()
    late_reader_acquired = threading.Event()

    def hold_reader() -> None:
        with store._connect() as connection:
            connection.execute("SELECT 1").fetchone()
            reader_started.set()
            release_reader.wait(2)

    def hold_writer() -> None:
        with store.exclusive_database_maintenance(timeout=2):
            # Store methods may re-enter the shared connection path from the
            # maintenance owner without deadlocking.
            assert store.storage_overview()["page_count"] > 0
            writer_acquired.set()
            release_writer.wait(2)

    def late_reader() -> None:
        with store._connect() as connection:
            connection.execute("SELECT 1").fetchone()
            late_reader_acquired.set()

    reader_thread = threading.Thread(target=hold_reader)
    writer_thread = threading.Thread(target=hold_writer)
    reader_thread.start()
    assert reader_started.wait(1)
    writer_thread.start()
    assert not writer_acquired.wait(0.05)

    release_reader.set()
    assert writer_acquired.wait(1)
    late_reader_thread = threading.Thread(target=late_reader)
    late_reader_thread.start()
    assert not late_reader_acquired.wait(0.05)

    release_writer.set()
    assert late_reader_acquired.wait(1)
    reader_thread.join(1)
    writer_thread.join(1)
    late_reader_thread.join(1)
    assert not reader_thread.is_alive()
    assert not writer_thread.is_alive()
    assert not late_reader_thread.is_alive()


def test_parse_timestamp_supports_iso_offsets_and_naive_utc() -> None:
    assert _parse_timestamp("2026-08-11T12:34:56Z") == datetime(2026, 8, 11, 12, 34, 56, tzinfo=UTC)
    assert _parse_timestamp("2026-08-11T08:34:56-04:00") == datetime.fromisoformat("2026-08-11T08:34:56-04:00")
    assert _parse_timestamp("2026-08-11T12:34:56.123456") == datetime(2026, 8, 11, 12, 34, 56, 123456, tzinfo=UTC)
    assert _parse_timestamp("not-a-timestamp") is None


def test_legacy_provider_zero_does_not_imply_subscription_coverage() -> None:
    item = _apply_cost_state({
        "actual_cost_call_count": 2,
        "estimated_cost_call_count": 0,
        "unpriced_call_count": 0,
        "call_count": 2,
        "actual_cost_microusd": 0,
    })

    assert item["cost_state"] == "actual"


def test_daily_metrics_always_returns_complete_requested_calendar_window(tmp_path: Path) -> None:
    today = datetime.now(UTC).date()
    first_day = today - timedelta(days=29)
    audit_path = tmp_path / "route-audit.jsonl"
    audit_path.write_text(
        "\n".join([
            json.dumps({
                "timestamp": f"{first_day.isoformat()}T12:00:00+00:00",
                "status": "success",
                "logical_turn_id": "first-day",
                "usage": {"input_tokens": 10, "output_tokens": 2},
            }),
            json.dumps({
                "timestamp": f"{today.isoformat()}T12:00:00+00:00",
                "status": "success",
                "logical_turn_id": "today",
                "usage": {"input_tokens": 20, "output_tokens": 3},
            }),
        ]) + "\n",
        encoding="utf-8",
    )
    store = DashboardStore(tmp_path / "routerllm.sqlite3")
    store.import_audit_jsonl(audit_path)

    metrics = store.daily_metrics(days=30)

    assert len(metrics) == 30
    assert [item["day"] for item in metrics] == [
        (first_day + timedelta(days=offset)).isoformat() for offset in range(30)
    ]
    assert metrics[0]["call_count"] == 1
    assert metrics[1] == {
        "day": (first_day + timedelta(days=1)).isoformat(),
        "call_count": 0,
        "turn_count": 0,
        "success_count": 0,
        "failure_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "average_duration_ms": 0.0,
    }
    assert metrics[-1]["call_count"] == 1


def test_daily_metrics_one_day_window_returns_today_even_without_calls(tmp_path: Path) -> None:
    store = DashboardStore(tmp_path / "routerllm.sqlite3")

    metrics = store.daily_metrics(days=1)

    assert metrics == [{
        "day": datetime.now(UTC).date().isoformat(),
        "call_count": 0,
        "turn_count": 0,
        "success_count": 0,
        "failure_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "average_duration_ms": 0.0,
    }]


def test_calendar_metrics_use_the_existing_occurred_at_range_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = DashboardStore(tmp_path / "routerllm.sqlite3")
    store.initialize()
    statements: list[str] = []
    original_connect = store._connect

    @contextmanager
    def traced_connect():
        with original_connect() as connection:
            connection.set_trace_callback(statements.append)
            yield connection

    monkeypatch.setattr(store, "_connect", traced_connect)
    store.daily_metrics(days=30)
    store.weekly_metrics(weeks=12)

    metric_queries = [statement for statement in statements if "FROM api_calls WHERE" in statement]
    assert len(metric_queries) == 2
    assert all("WHERE occurred_at >=" in statement for statement in metric_queries)
    assert all("WHERE substr(occurred_at" not in statement for statement in metric_queries)

    with sqlite3.connect(store.database_path) as connection:
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM api_calls WHERE occurred_at >= ?",
            ("2026-01-01",),
        ).fetchall()
    assert any("api_calls_occurred_at_idx" in str(row[3]) for row in plan)


def test_storage_buckets_backfill_calls_and_payload_bytes(tmp_path: Path) -> None:
    audit_path = tmp_path / "route-audit.jsonl"
    audit_path.write_text(
        json.dumps({
            "timestamp": "2026-08-10T12:34:00+00:00",
            "status": "success",
            "payload_ref": "payload-storage",
            "usage": {"input_tokens": 2, "output_tokens": 1},
        }) + "\n",
        encoding="utf-8",
    )
    store = DashboardStore(tmp_path / "routerllm.sqlite3")
    store.import_audit_jsonl(audit_path)
    with sqlite3.connect(store.database_path) as connection:
        call_id = connection.execute("SELECT id FROM api_calls").fetchone()[0]
        connection.execute(
            """INSERT INTO call_payloads(payload_ref, api_call_id, nonce, ciphertext, captured_at)
               VALUES ('payload-storage', ?, 'nonce', 'ciphertext', '2026-08-10T12:35:00+00:00')""",
            (call_id,),
        )

    result = store.refresh_storage_buckets(max_records=100)
    status = store.storage_bucket_status()

    assert result["complete"] is True
    assert status["call_count"] == 1
    assert status["embedded_payload_bytes"] == len("nonce") + len("ciphertext")
    assert status["bucket_count"] == 1


def test_audit_import_is_incremental_and_does_not_persist_prompt_or_error_body(tmp_path: Path) -> None:
    audit_path = tmp_path / "route-audit.jsonl"
    audit_path.write_text(
        json.dumps(
            {
                "timestamp": "2026-08-10T12:00:00+00:00",
                "status": "failure",
                "logical_turn_id": "turn-safe",
                "selected_model": "chatgpt/gpt-5.6-terra",
                "prompt": "must-not-persist",
                "error": "Bearer must-not-persist",
                "error_type": "RateLimitError",
                "usage": {"input_tokens": 20, "output_tokens": 10},
                "routing_signals": ["short_follow_up"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    store = DashboardStore(tmp_path / "routerllm.sqlite3")

    first = store.import_audit_jsonl(audit_path)
    with sqlite3.connect(store.database_path) as connection:
        first_cursor_updated_at = connection.execute(
            "SELECT updated_at FROM import_cursors WHERE source_path=?",
            (str(audit_path),),
        ).fetchone()[0]
    second = store.import_audit_jsonl(audit_path)
    with sqlite3.connect(store.database_path) as connection:
        second_cursor_updated_at = connection.execute(
            "SELECT updated_at FROM import_cursors WHERE source_path=?",
            (str(audit_path),),
        ).fetchone()[0]
    rows = store.recent_calls()

    assert first["imported"] == 1
    assert second["imported"] == 0
    assert second_cursor_updated_at == first_cursor_updated_at
    assert rows[0]["error_category"] == "rate_limit"
    assert rows[0]["input_tokens"] == 20
    raw_database = (tmp_path / "routerllm.sqlite3").read_bytes()
    assert b"must-not-persist" not in raw_database


def test_quota_snapshot_and_redemption_idempotency(tmp_path: Path) -> None:
    store = DashboardStore(tmp_path / "routerllm.sqlite3")
    store.record_quota_snapshot(
        {
            "profile_id": "chatgpt-default",
            "observed_at": "2026-08-10T12:00:00+00:00",
            "status": "available",
            "account": {"state": "connected", "email": "private@example.com", "email_masked": "pr****@example.com", "fingerprint": "safe-fingerprint", "account_type": "chatgpt", "name": "QA Team RIFO", "identity_source": "local_codex_id_token"},
            "summary": {"plan_type": "pro", "account_label": "private@example.com", "auth_mode": "chatgpt"},
            "windows": [{"key": "primary", "label": "Primary", "limit_id": "codex", "window_kind": "primary", "remaining_percent": 42.0}],
            "usage": {"summary": {"lifetimeTokens": 100}, "daily_usage_buckets": [{"start_date": "2026-08-10", "tokens": 10}]},
            "reset_credits": {
                "available_count": 1,
                "count_source": "official_available_count",
                "detail_lower_bound": 1,
                "details_state": "complete",
                "returned_detail_count": 1,
                "missing_detail_count": 0,
                "details": [{
                    "status": "available",
                    "reset_type": "codexRateLimits",
                    "granted_at": "2026-08-01T12:00:00+00:00",
                    "expires_at": "2026-08-31T12:00:00+00:00",
                    "title": "Rate-limit reset",
                    "description": "Reset an eligible Codex rate-limit window.",
                }],
                "observed_at": "2026-08-10T12:00:00+00:00",
                "source": "account/rateLimits/read",
                "contract_warning": None,
            },
        }
    )

    latest = store.latest_quota()
    assert latest["windows"][0]["remaining_percent"] == 42.0
    assert latest["account"]["email_masked"] == "pr****@example.com"
    assert latest["account"]["source"] == "local_codex_id_token"
    assert latest["account"]["name"] == "QA Team RIFO"
    assert latest["account"]["account_identifier"] == "qa****"
    assert latest["usage"]["summary"]["lifetimeTokens"] == 100
    assert latest["reset_credits"]["available_count"] == 1
    assert latest["reset_credits"]["details_state"] == "complete"
    assert latest["reset_credits"]["details"][0]["expires_at"] == "2026-08-31T12:00:00+00:00"
    assert b"private@example.com" not in (tmp_path / "routerllm.sqlite3").read_bytes()
    assert store.start_redemption(idempotency_key="redemption-0001", profile_id=None, credit_id="must-not-persist-credit-id")
    assert not store.start_redemption(idempotency_key="redemption-0001", profile_id=None, credit_id=None)
    assert b"must-not-persist-credit-id" not in (tmp_path / "routerllm.sqlite3").read_bytes()


def test_quota_snapshot_round_trips_multiple_cached_windows(tmp_path: Path) -> None:
    store = DashboardStore(tmp_path / "routerllm.sqlite3")
    store.record_quota_snapshot(
        {
            "profile_id": "chatgpt-default",
            "observed_at": "2026-08-10T12:00:00+00:00",
            "status": "available",
            "windows": [
                {"key": "primary", "label": "Primary", "remaining_percent": 80},
                {"key": "secondary", "label": "Secondary", "remaining_percent": 60},
            ],
        }
    )

    cached = store.latest_quota()
    assert cached is not None
    stored = store.record_quota_snapshot(cached)

    assert stored["id"] >= 2
    assert [window["window_key"] for window in store.latest_quota()["windows"]] == [
        "primary",
        "secondary",
    ]


def test_cached_account_identifier_is_not_derived_from_email_only(tmp_path: Path) -> None:
    store = DashboardStore(tmp_path / "routerllm.sqlite3")
    store.record_quota_snapshot({
        "profile_id": "chatgpt-default",
        "observed_at": "2026-08-10T12:00:00+00:00",
        "status": "available",
        "account": {
            "state": "connected",
            "email_masked": "ch****@example.com",
            "account_type": "chatgpt",
        },
        "summary": {"plan_type": "pro", "auth_mode": "chatgpt"},
        "windows": [],
        "usage": {"summary": {}, "daily_usage_buckets": []},
        "reset_credits": {"available_count": 0, "details": []},
    })

    latest = store.latest_quota()

    assert latest is not None
    assert latest["account"]["account_identifier"] is None


def test_legacy_schema_migrates_call_semantics_and_clears_raw_ids(tmp_path: Path) -> None:
    database_path = tmp_path / "routerllm.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE reset_redemptions (
                idempotency_key TEXT PRIMARY KEY,
                profile_id TEXT,
                credit_id TEXT,
                outcome TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                error_code TEXT
            );
            INSERT INTO reset_redemptions(idempotency_key, credit_id, created_at)
            VALUES ('old-redemption', 'raw-credit-id', '2026-08-01T00:00:00+00:00');
            CREATE TABLE quota_reset_credits (
                quota_snapshot_id INTEGER PRIMARY KEY,
                available_count INTEGER,
                observed_at TEXT NOT NULL,
                source TEXT NOT NULL
            );
            """
        )

    store = DashboardStore(database_path)
    store.initialize()

    with sqlite3.connect(database_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(quota_reset_credits)")}
        raw_credit_id = connection.execute(
            "SELECT credit_id FROM reset_redemptions WHERE idempotency_key='old-redemption'"
        ).fetchone()[0]
        api_call_columns = {row[1] for row in connection.execute("PRAGMA table_info(api_calls)")}
        migration = connection.execute("SELECT version FROM schema_migrations WHERE version=16").fetchone()
        pricing_version_count = connection.execute("SELECT COUNT(*) FROM pricing_rule_versions").fetchone()[0]

    assert {"details_state", "returned_detail_count", "missing_detail_count"} <= columns
    assert {
        "provider_status",
        "stream_capture_state",
        "display_status",
        "output_item_count",
        "terminal_output_item_count",
        "tool_call_count",
        "output_semantics_version",
        "output_semantics_source",
        "aggregation_source",
        "stream_event_count",
        "stream_first_sequence",
        "stream_gap_count",
        "stream_output_item_added_count",
        "stream_output_item_done_count",
        "stream_function_argument_delta_count",
        "stream_response_completed_count",
        "stream_capture_complete",
        "visible_message_count",
        "reasoning_item_count",
        "call_kind",
        "subagent_kind",
        "parent_turn_id",
        "cache_write_tokens",
        "billing_model",
        "api_equivalent_cost_microusd",
        "actual_incremental_cost_microusd",
        "pricing_catalog_version",
        "estimator_version",
    } <= api_call_columns
    assert raw_credit_id is None
    assert migration == (16,)
    assert pricing_version_count == 8
    assert b"raw-credit-id" not in database_path.read_bytes()


def test_calls_filter_sort_price_and_decrypt_payload_detail(tmp_path: Path) -> None:
    audit_path = tmp_path / "route-audit.jsonl"
    payload_path = tmp_path / "call-payloads.jsonl.enc"
    key = b"a" * 32
    records = [
        {
            "timestamp": "2026-08-10T10:00:00+00:00",
            "status": "success",
            "selected_model": "router/model-a",
            "logical_turn_id": "turn-a",
            "payload_ref": "payload-a",
            "usage": {"input_tokens": 10, "output_tokens": 20},
        },
        {
            "timestamp": "2026-08-10T11:00:00+00:00",
            "status": "success",
            "provider_status": "completed",
            "stream_capture_state": "captured",
            "display_status": "completed_with_output",
            "output_item_count": 99,
            "tool_call_count": 99,
            "output_semantics_version": 2,
            "output_semantics_source": "terminal_response",
            "selected_model": "router/model-a",
            "logical_turn_id": "turn-b",
            "payload_ref": "payload-b",
            "usage": {"input_tokens": 50, "output_tokens": 5},
        },
    ]
    audit_path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    store = DashboardStore(tmp_path / "routerllm.sqlite3", payload_path=payload_path, payload_key=key)
    store.initialize()
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            """INSERT INTO pricing_rules(model_pattern, currency, input_microusd_per_million,
               output_microusd_per_million, version, effective_at) VALUES (?, ?, ?, ?, ?, ?)""",
            ("model-a", "USD", 1_000_000, 2_000_000, "test-v1", "2026-01-01T00:00:00+00:00"),
        )

    store.import_audit_jsonl(audit_path)
    calls = store.recent_calls(
        start_at="2026-08-10T10:30:00+00:00",
        sort_by="input_tokens",
        sort_order="desc",
    )

    assert [call["logical_turn_id"] for call in calls] == ["turn-b"]
    assert calls[0]["total_cost_microusd"] == 60
    assert calls[0]["pricing_version"] == "test-v1"

    payload = {
        "payload_schema_version": 2,
        "payload_ref": "payload-b",
        "request": {"input": "private prompt"},
        "terminal_response": {"status": "completed", "output": []},
        "normalized_output_items": [
            {"id": "msg-1", "type": "message", "content": [{"type": "output_text", "text": "private answer"}]},
            {"id": "fc-1", "type": "function_call", "call_id": "call-1", "name": "lookup", "arguments": "{}"},
        ],
        "output_text": "private answer",
        "tool_calls": [{"id": "fc-1", "type": "function_call", "call_id": "call-1", "name": "lookup", "arguments": "{}"}],
        "stream_events": [{"type": "response.output_text.delta", "delta": "private answer"}],
        "aggregation_source": "stream_deltas",
    }
    envelope = {"payload_ref": "payload-b", **encrypt_payload(key, json.dumps(payload).encode("utf-8"))}
    payload_path.write_text(json.dumps(envelope) + "\n", encoding="utf-8")
    imported = store.import_encrypted_payloads()
    detail = store.call_detail(calls[0]["id"])
    input_section = store.call_payload_section(calls[0]["id"], "input")
    output_section = store.call_payload_section(calls[0]["id"], "output-items")
    stream_section = store.call_payload_section(calls[0]["id"], "stream-events")

    assert imported["imported"] == 1
    assert detail is not None
    assert detail["provider_status"] == "completed"
    assert detail["stream_capture_state"] == "captured_partial"
    assert detail["display_status"] == "completed_with_visible_output"
    assert detail["output_item_count"] == 2
    assert detail["tool_call_count"] == 1
    assert detail["output_semantics_version"] == 5
    assert detail["output_semantics_source"] == "sse_delta_reconstruction"
    assert detail["response_item_count"] == 2
    assert detail["visible_message_count"] == 1
    assert detail["reasoning_item_count"] == 0
    assert detail["other_item_count"] == 0
    assert detail["payload"]["state"] == "available"
    assert "request" not in detail["payload"]
    assert input_section is not None
    assert input_section["request"]["input"] == "private prompt"
    assert output_section is not None
    assert output_section["payload_schema_version"] == 2
    assert output_section["output_text"] == "private answer"
    assert output_section["normalized_output_items"][1]["type"] == "function_call"
    assert stream_section is not None
    assert stream_section["events"][0]["type"] == "response.output_text.delta"
    assert b"private prompt" not in store.database_path.read_bytes()
    assert b"private answer" not in store.database_path.read_bytes()


def test_encrypted_payload_import_commits_cursor_per_batch(tmp_path: Path) -> None:
    audit_path = tmp_path / "route-audit.jsonl"
    payload_path = tmp_path / "call-payloads.jsonl.enc"
    key = b"b" * 32
    records = [
        {"timestamp": "2026-08-10T10:00:00+00:00", "status": "success", "payload_ref": "payload-1"},
        {"timestamp": "2026-08-10T10:01:00+00:00", "status": "success", "payload_ref": "payload-2"},
    ]
    audit_path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    payload_path.write_text(
        "".join(
            json.dumps({"payload_ref": payload_ref, **encrypt_payload(key, json.dumps({"payload_ref": payload_ref}).encode("utf-8"))}) + "\n"
            for payload_ref in ("payload-1", "payload-2")
        ),
        encoding="utf-8",
    )
    store = DashboardStore(tmp_path / "routerllm.sqlite3", payload_path=payload_path, payload_key=key)
    store.import_audit_jsonl(audit_path)

    first = store.import_encrypted_payloads(max_records=1)
    with sqlite3.connect(store.database_path) as connection:
        first_payload_count = connection.execute("SELECT COUNT(*) FROM call_payloads").fetchone()[0]
        first_index_count = connection.execute("SELECT COUNT(*) FROM payload_archive_index").fetchone()[0]
        first_offset = connection.execute(
            "SELECT last_offset FROM import_cursors WHERE source_path=?",
            (str(payload_path),),
        ).fetchone()[0]

    second = store.import_encrypted_payloads(max_records=1)
    with sqlite3.connect(store.database_path) as connection:
        second_payload_count = connection.execute("SELECT COUNT(*) FROM call_payloads").fetchone()[0]
        second_index_count = connection.execute("SELECT COUNT(*) FROM payload_archive_index").fetchone()[0]

    assert first["complete"] is False
    assert first_payload_count == 0
    assert first_index_count == 1
    assert 0 < first_offset < payload_path.stat().st_size
    assert second["complete"] is True
    assert second_payload_count == 0
    assert second_index_count == 2


def test_retention_tombstone_blocks_payload_reimport_and_reindex(tmp_path: Path) -> None:
    audit_path = tmp_path / "route-audit.jsonl"
    payload_path = tmp_path / "call-payloads.jsonl.enc"
    key = b"t" * 32
    payload_ref = "payload-retained-away"
    audit_path.write_text(
        json.dumps({"timestamp": "2026-01-01T10:00:00+00:00", "status": "success", "payload_ref": payload_ref}) + "\n",
        encoding="utf-8",
    )
    envelope = {
        "payload_ref": payload_ref,
        **encrypt_payload(key, json.dumps({"payload_ref": payload_ref}).encode("utf-8")),
    }
    payload_path.write_text(json.dumps(envelope) + "\n", encoding="utf-8")
    store = DashboardStore(tmp_path / "routerllm.sqlite3", payload_path=payload_path, payload_key=key)
    store.import_audit_jsonl(audit_path)
    with sqlite3.connect(store.database_path) as connection:
        call_id = connection.execute("SELECT id FROM api_calls WHERE payload_ref=?", (payload_ref,)).fetchone()[0]
        connection.execute(
            """INSERT INTO payload_retention_tombstones(
                   payload_ref, api_call_id, occurred_at, deleted_at, delete_reason,
                   retention_job_id, original_storage, original_bytes
               ) VALUES (?, ?, ?, ?, 'capacity_limit', 'job-test', 'encrypted_log', 1)""",
            (payload_ref, call_id, "2026-01-01T10:00:00+00:00", "2026-08-12T10:00:00+00:00"),
        )

    imported = store.import_encrypted_payloads()
    indexed = store.index_payload_archive(max_records=10)
    with sqlite3.connect(store.database_path) as connection:
        payload_count = connection.execute("SELECT COUNT(*) FROM call_payloads").fetchone()[0]
        index_count = connection.execute("SELECT COUNT(*) FROM payload_archive_index").fetchone()[0]

    assert imported["imported"] == 0
    assert indexed["indexed"] == 0
    assert payload_count == 0
    assert index_count == 0
    assert store.call_payload_section(call_id, "visible-output")["state"] == "retention_deleted"


def test_payload_retention_deduplicates_shared_old_payload_refs(tmp_path: Path) -> None:
    audit_path = tmp_path / "route-audit.jsonl"
    payload_path = tmp_path / "call-payloads.jsonl.enc"
    payload_ref = "shared-old-payload"
    audit_path.write_text(
        "".join(
            json.dumps({"timestamp": timestamp, "status": "success", "payload_ref": payload_ref}) + "\n"
            for timestamp in ("2026-01-01T10:00:00+00:00", "2026-01-02T10:00:00+00:00")
        ),
        encoding="utf-8",
    )
    payload_path.write_text(
        json.dumps({"payload_ref": payload_ref, "nonce": "a", "ciphertext": "b"}) + "\n",
        encoding="utf-8",
    )
    store = DashboardStore(tmp_path / "routerllm.sqlite3", payload_path=payload_path)
    store.import_audit_jsonl(audit_path)
    store.index_payload_archive(max_records=10)

    preflight = store.retention_payload_preflight("2026-02-01T00:00:00+00:00")
    result = store.apply_payload_retention(
        cutoff_at="2026-02-01T00:00:00+00:00",
        job_id="shared-old-job",
        archive_entries=[],
        archive_stat=None,
    )

    assert preflight["candidate_call_count"] == 2
    assert preflight["unique_payload_count"] == 1
    assert preflight["duplicate_reference_count"] == 1
    assert preflight["protected_shared_payload_count"] == 0
    assert result["payload_count"] == 1
    assert result["affected_call_count"] == 2
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM payload_retention_tombstones WHERE retention_job_id=?",
            ("shared-old-job",),
        ).fetchone()[0] == 1


def test_payload_retention_protects_refs_shared_with_retained_calls(tmp_path: Path) -> None:
    audit_path = tmp_path / "route-audit.jsonl"
    payload_path = tmp_path / "call-payloads.jsonl.enc"
    payload_ref = "shared-across-cutoff"
    audit_path.write_text(
        "".join(
            json.dumps({"timestamp": timestamp, "status": "success", "payload_ref": payload_ref}) + "\n"
            for timestamp in ("2026-01-01T10:00:00+00:00", "2026-03-01T10:00:00+00:00")
        ),
        encoding="utf-8",
    )
    payload_path.write_text(
        json.dumps({"payload_ref": payload_ref, "nonce": "a", "ciphertext": "b"}) + "\n",
        encoding="utf-8",
    )
    store = DashboardStore(tmp_path / "routerllm.sqlite3", payload_path=payload_path)
    store.import_audit_jsonl(audit_path)
    store.index_payload_archive(max_records=10)

    preflight = store.retention_payload_preflight("2026-02-01T00:00:00+00:00")
    result = store.apply_payload_retention(
        cutoff_at="2026-02-01T00:00:00+00:00",
        job_id="shared-retained-job",
        archive_entries=[],
        archive_stat=None,
    )

    assert preflight["candidate_call_count"] == 0
    assert preflight["unique_payload_count"] == 0
    assert preflight["duplicate_reference_count"] == 0
    assert preflight["protected_shared_payload_count"] == 1
    assert result["payload_count"] == 0
    assert result["affected_call_count"] == 0
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM payload_retention_tombstones").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM payload_archive_index WHERE payload_ref=?",
            (payload_ref,),
        ).fetchone()[0] == 1


def test_payload_archive_index_reads_without_copying_ciphertext(tmp_path: Path) -> None:
    audit_path = tmp_path / "route-audit.jsonl"
    payload_path = tmp_path / "call-payloads.jsonl.enc"
    key = b"i" * 32
    audit_path.write_text(
        json.dumps({
            "timestamp": "2026-08-11T10:00:00+00:00",
            "status": "success",
            "payload_ref": "payload-indexed",
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }) + "\n",
        encoding="utf-8",
    )
    payload = {
        "payload_schema_version": 3,
        "payload_ref": "payload-indexed",
        "request": {"input": [{"role": "user", "content": "indexed prompt"}]},
        "terminal_response": {"status": "completed", "output": []},
    }
    payload_path.write_text(
        json.dumps({"payload_ref": "payload-indexed", **encrypt_payload(key, json.dumps(payload).encode("utf-8"))}) + "\n",
        encoding="utf-8",
    )
    store = DashboardStore(tmp_path / "routerllm.sqlite3", payload_path=payload_path, payload_key=key)
    store.import_audit_jsonl(audit_path)

    indexed = store.index_payload_archive()
    call = store.recent_calls()[0]
    detail = store.call_detail(call["id"])
    input_section = store.call_payload_section(call["id"], "input")

    assert indexed["indexed"] == 1
    assert indexed["complete"] is True
    assert detail is not None
    assert detail["payload"]["state"] == "available"
    assert detail["payload"]["source"] == "encrypted_log"
    assert input_section is not None
    assert input_section["request"]["input"][0]["content"] == "indexed prompt"
    assert input_section["source"] == "encrypted_log"
    user_input = store.call_payload_section(call["id"], "user-input")
    assert user_input is not None
    assert user_input["user_messages"] == [{"role": "user", "content": "indexed prompt"}]
    assert user_input["request_metadata"]["user_message_count"] == 1
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM call_payloads").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM payload_archive_index").fetchone()[0] == 1


def test_payload_projection_audit_retains_items_without_live_archive_coverage(tmp_path: Path) -> None:
    audit_path = tmp_path / "route-audit.jsonl"
    payload_path = tmp_path / "call-payloads.jsonl.enc"
    key = b"p" * 32
    payload_ref = "payload-projection-audit"
    audit_path.write_text(
        json.dumps({
            "timestamp": "2026-08-11T10:00:00+00:00",
            "status": "success",
            "payload_ref": payload_ref,
        }) + "\n",
        encoding="utf-8",
    )
    envelope = {
        "payload_ref": payload_ref,
        **encrypt_payload(key, json.dumps({"payload_ref": payload_ref}).encode("utf-8")),
    }
    payload_path.write_text(json.dumps(envelope) + "\n", encoding="utf-8")
    store = DashboardStore(tmp_path / "routerllm.sqlite3", payload_path=payload_path, payload_key=key)
    store.import_audit_jsonl(audit_path)
    store.import_encrypted_payloads()
    with sqlite3.connect(store.database_path) as connection:
        call_id = connection.execute("SELECT id FROM api_calls WHERE payload_ref=?", (payload_ref,)).fetchone()[0]
        connection.execute(
            """INSERT INTO call_payloads(payload_ref, api_call_id, nonce, ciphertext, captured_at)
               VALUES (?, ?, ?, ?, ?)""",
            (payload_ref, call_id, envelope["nonce"], envelope["ciphertext"], "2026-08-11T10:00:00+00:00"),
        )

    ready = store.payload_projection_audit()

    assert ready["state"] == "ready"
    assert ready["embedded_payload_count"] == 1
    assert ready["archive_covered_payload_count"] == 1
    assert ready["missing_archive_payload_count"] == 0
    assert ready["removable_payload_bytes"] > 0

    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE payload_archive_index SET source_inode=source_inode+1 WHERE payload_ref=?",
            (payload_ref,),
        )

    partial = store.payload_projection_audit()

    assert partial["state"] == "ready_with_retained_items"
    assert partial["archive_covered_payload_count"] == 0
    assert partial["missing_archive_payload_count"] == 1
    assert partial["removable_payload_bytes"] == 0


def test_payload_archive_identity_rebinds_migrated_path_with_same_inode(tmp_path: Path) -> None:
    database_path = tmp_path / "routerllm.sqlite3"
    legacy_path = tmp_path / "legacy" / "call-payloads.jsonl.enc"
    current_path = tmp_path / "current" / "call-payloads.jsonl.enc"
    audit_path = tmp_path / "route-audit.jsonl"
    payload_ref = "payload-migrated-path"
    key = b"r" * 32
    audit_path.write_text(
        json.dumps({
            "timestamp": "2026-08-11T10:00:00+00:00",
            "status": "success",
            "payload_ref": payload_ref,
        }) + "\n",
        encoding="utf-8",
    )
    envelope = {
        "payload_ref": payload_ref,
        **encrypt_payload(key, json.dumps({"payload_ref": payload_ref}).encode("utf-8")),
    }
    legacy_path.parent.mkdir()
    legacy_path.write_text(json.dumps(envelope) + "\n", encoding="utf-8")
    legacy_store = DashboardStore(database_path, payload_path=legacy_path, payload_key=key)
    legacy_store.import_audit_jsonl(audit_path)
    legacy_store.index_payload_archive()
    with sqlite3.connect(database_path) as connection:
        call_id = connection.execute("SELECT id FROM api_calls WHERE payload_ref=?", (payload_ref,)).fetchone()[0]
        connection.execute(
            """INSERT INTO call_payloads(payload_ref, api_call_id, nonce, ciphertext, captured_at)
               VALUES (?, ?, ?, ?, ?)""",
            (payload_ref, call_id, envelope["nonce"], envelope["ciphertext"], "2026-08-11T10:00:00+00:00"),
        )

    source_inode = legacy_path.stat().st_ino
    current_path.parent.mkdir()
    legacy_path.replace(current_path)
    current_store = DashboardStore(database_path, payload_path=current_path, payload_key=key)

    reconciled = current_store.reconcile_payload_archive_identity()

    assert reconciled == {"available": True, "rebound_index_count": 1, "rebound_cursor_count": 1}
    with sqlite3.connect(database_path) as connection:
        index_row = connection.execute(
            "SELECT source_path, source_inode FROM payload_archive_index WHERE payload_ref=?",
            (payload_ref,),
        ).fetchone()
        cursors = connection.execute(
            "SELECT source_path, source_inode FROM payload_archive_cursors"
        ).fetchall()
    assert index_row == (str(current_path), source_inode)
    assert cursors == [(str(current_path), source_inode)]
    assert current_store.payload_projection_audit()["state"] == "ready"


def test_payload_archive_identity_does_not_rebind_unproven_inode(tmp_path: Path) -> None:
    payload_path = tmp_path / "call-payloads.jsonl.enc"
    payload_path.write_text("{}\n", encoding="utf-8")
    store = DashboardStore(tmp_path / "routerllm.sqlite3", payload_path=payload_path, payload_key=b"i" * 32)
    store.initialize()
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            """INSERT INTO payload_archive_index(
                   payload_ref, source_path, source_inode, byte_offset, byte_length, indexed_at
               ) VALUES ('unproven', '/legacy/archive', ?, 0, 3, '2026-08-11T10:00:00+00:00')""",
            (payload_path.stat().st_ino + 1,),
        )

    reconciled = store.reconcile_payload_archive_identity()

    assert reconciled["rebound_index_count"] == 0
    with sqlite3.connect(store.database_path) as connection:
        source_path = connection.execute(
            "SELECT source_path FROM payload_archive_index WHERE payload_ref='unproven'"
        ).fetchone()[0]
    assert source_path == "/legacy/archive"


def test_payload_archive_index_tracks_appended_records_and_skips_invalid_lines(tmp_path: Path) -> None:
    audit_path = tmp_path / "route-audit.jsonl"
    payload_path = tmp_path / "call-payloads.jsonl.enc"
    key = b"j" * 32
    audit_path.write_text("".join(json.dumps(record) + "\n" for record in [
        {"timestamp": "2026-08-11T10:00:00+00:00", "status": "success", "payload_ref": "payload-one"},
        {"timestamp": "2026-08-11T10:01:00+00:00", "status": "success", "payload_ref": "payload-two"},
    ]), encoding="utf-8")

    def envelope(payload_ref: str) -> str:
        payload = {"payload_ref": payload_ref, "request": {"input": payload_ref}}
        return json.dumps({"payload_ref": payload_ref, **encrypt_payload(key, json.dumps(payload).encode("utf-8"))}) + "\n"

    payload_path.write_text(envelope("payload-one"), encoding="utf-8")
    store = DashboardStore(tmp_path / "routerllm.sqlite3", payload_path=payload_path, payload_key=key)
    store.import_audit_jsonl(audit_path)
    first = store.index_payload_archive()
    with payload_path.open("a", encoding="utf-8") as handle:
        handle.write("not-json\n")
        handle.write(envelope("payload-two"))
    second = store.index_payload_archive()

    calls = {call["payload_ref"]: call for call in store.recent_calls()}
    assert first["indexed"] == 1
    assert second["indexed"] == 1
    assert second["skipped"] == 1
    assert second["complete"] is True
    assert store.payload_archive_status()["complete"] is True
    assert store.call_payload_section(calls["payload-one"]["id"], "input")["request"]["input"] == "payload-one"
    assert store.call_payload_section(calls["payload-two"]["id"], "input")["request"]["input"] == "payload-two"


def test_payload_semantics_migration_marks_historical_missing_output(tmp_path: Path) -> None:
    audit_path = tmp_path / "route-audit.jsonl"
    payload_path = tmp_path / "call-payloads.jsonl.enc"
    key = b"c" * 32
    audit_path.write_text(
        json.dumps({
            "timestamp": "2026-08-11T03:48:45+00:00",
            "status": "success",
            "selected_alias": "sol-medium",
            "payload_ref": "payload-missing",
            "usage": {"input_tokens": 93162, "output_tokens": 2646},
        }) + "\n",
        encoding="utf-8",
    )
    payload = {
        "payload_ref": "payload-missing",
        "request": {"stream": True, "input": [{"role": "user", "content": "private prompt"}]},
        "response": {"id": "resp-missing", "status": "completed", "output": []},
    }
    payload_path.write_text(
        json.dumps({"payload_ref": "payload-missing", **encrypt_payload(key, json.dumps(payload).encode("utf-8"))}) + "\n",
        encoding="utf-8",
    )
    store = DashboardStore(tmp_path / "routerllm.sqlite3", payload_path=payload_path, payload_key=key)
    store.import_audit_jsonl(audit_path)
    store.import_encrypted_payloads()
    call = store.recent_calls()[0]

    assert call["provider_status"] == "completed"
    assert call["stream_capture_state"] == "capture_missing"
    assert call["display_status"] == "completed_output_missing"
    assert call["output_item_count"] == 0

    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            """UPDATE api_calls SET provider_status=NULL, stream_capture_state=NULL,
               display_status=NULL, output_item_count=NULL, tool_call_count=NULL,
               output_semantics_version=0, output_semantics_source=NULL WHERE id=?""",
            (call["id"],),
        )
    restarted_store = DashboardStore(store.database_path, payload_path=payload_path, payload_key=key)
    restarted_store.initialize()
    pending = restarted_store.call_detail(call["id"])
    assert pending is not None
    assert pending["output_semantics_version"] == 0

    migration = restarted_store.migrate_output_semantics(max_records=1)
    backfilled = restarted_store.call_detail(call["id"])

    assert migration["migrated"] == 1
    assert migration["complete"] is True
    assert backfilled is not None
    assert backfilled["display_status"] == "completed_output_missing"
    assert backfilled["output_item_count"] == 0
    assert backfilled["tool_call_count"] == 0
    assert backfilled["output_semantics_version"] == 5
    assert backfilled["output_semantics_source"] == "historical_inference"


def test_output_semantics_migration_is_bounded_and_infers_calls_without_payloads(tmp_path: Path) -> None:
    audit_path = tmp_path / "route-audit.jsonl"
    audit_path.write_text(
        "".join(
            json.dumps({
                "timestamp": f"2026-08-11T10:0{index}:00+00:00",
                "status": "success",
                "logical_turn_id": f"turn-{index}",
            }) + "\n"
            for index in range(3)
        ),
        encoding="utf-8",
    )
    store = DashboardStore(tmp_path / "routerllm.sqlite3")
    store.import_audit_jsonl(audit_path)

    first = store.migrate_output_semantics(max_records=2)
    second = store.migrate_output_semantics(max_records=2)

    assert first == {
        "migrated": 0,
        "inferred": 2,
        "skipped": 0,
        "processed_bytes": 0,
        "remaining": 1,
        "complete": False,
    }
    assert second["inferred"] == 1
    assert second["remaining"] == 0
    assert second["complete"] is True
    assert store.output_semantics_migration_status() == {"remaining": 0, "complete": True}
    assert all(call["output_semantics_version"] == 5 for call in store.recent_calls())
    assert all(call["output_semantics_source"] == "historical_inference" for call in store.recent_calls())


def test_tasks_and_routing_stats_preserve_task_and_cost_semantics(tmp_path: Path) -> None:
    audit_path = tmp_path / "route-audit.jsonl"
    records = [
        {
            "timestamp": "2026-08-10T10:00:00+00:00",
            "status": "success",
            "logical_turn_id": "turn-mixed",
            "selected_alias": "sol-medium",
            "selected_model": "router/model-a",
            "selected_tier": "COMPLEX",
            "routing_cause": "initial_route",
            "response_cost": 0,
            "usage": {"input_tokens": 10, "output_tokens": 2},
        },
        {
            "timestamp": "2026-08-10T10:01:00+00:00",
            "status": "failure",
            "logical_turn_id": "turn-mixed",
            "selected_alias": "sol-high",
            "selected_model": "router/model-b",
            "selected_tier": "HIGH",
            "routing_cause": "turn_inheritance",
            "usage": {"input_tokens": 20, "output_tokens": 1},
        },
        {
            "timestamp": "2026-08-10T10:02:00+00:00",
            "status": "success",
            "selected_alias": "sol-medium",
            "selected_model": "router/model-a",
            "selected_tier": "COMPLEX",
            "routing_cause": "standalone",
            "usage": {"input_tokens": 5, "output_tokens": 3},
        },
    ]
    audit_path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    store = DashboardStore(tmp_path / "routerllm.sqlite3")
    store.import_audit_jsonl(audit_path)

    first_page = store.tasks(limit=1, sort_by="started_at", sort_order="asc")
    second_page = store.tasks(limit=1, cursor=first_page["next_cursor"], sort_by="started_at", sort_order="asc")

    assert first_page["total"] == 2
    assert first_page["next_cursor"] is not None
    assert first_page["items"][0]["task_key"] == "logical:turn-mixed"
    assert first_page["items"][0]["call_count"] == 2
    assert first_page["items"][0]["status"] == "partial"
    assert first_page["items"][0]["primary_model"] == "sol-high"
    assert first_page["items"][0]["model_variant_count"] == 2
    assert first_page["items"][0]["actual_cost_microusd"] == 0
    assert first_page["items"][0]["unpriced_call_count"] == 1
    assert first_page["items"][0]["cost_state"] == "partial_actual"
    assert second_page["items"][0]["task_kind"] == "standalone_call"

    detail = store.task_detail("logical:turn-mixed")
    assert detail is not None
    assert detail["summary"]["models"] == ["sol-high", "sol-medium"]
    assert detail["summary"]["started_at"] == "2026-08-10T10:00:00+00:00"
    assert detail["summary"]["last_activity_at"] == "2026-08-10T10:01:00+00:00"
    assert detail["summary"]["primary_model"] == "sol-high"
    assert detail["calls_order"] == "occurred_at_desc"
    assert [call["occurred_at"] for call in detail["calls"]] == [
        "2026-08-10T10:01:00+00:00",
        "2026-08-10T10:00:00+00:00",
    ]
    assert len(detail["calls"]) == 2

    stats = store.routing_stats(sort_by="model", sort_order="asc")
    medium = next(item for item in stats if item["model"] == "sol-medium")
    assert medium["task_count"] == 2
    assert medium["call_count"] == 2
    assert medium["actual_cost_microusd"] == 0
    assert medium["cost_state"] == "partial_actual"
    assert {reason["routing_cause"] for reason in medium["routing_reasons"]} == {"initial_route", "standalone"}


def test_default_openai_pricing_estimates_gpt_56_token_costs(tmp_path: Path) -> None:
    audit_path = tmp_path / "route-audit.jsonl"
    records = [
        {
            "timestamp": "2026-08-11T10:00:00+00:00",
            "status": "success",
            "selected_model": "chatgpt/gpt-5.6-sol",
            "usage": {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
        },
        {
            "timestamp": "2026-08-11T10:01:00+00:00",
            "status": "success",
            "selected_model": "chatgpt/gpt-5.6-terra",
            "usage": {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
        },
        {
            "timestamp": "2026-08-11T10:02:00+00:00",
            "status": "success",
            "selected_model": "chatgpt/gpt-5.6-luna",
            "usage": {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
        },
    ]
    audit_path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    store = DashboardStore(tmp_path / "routerllm.sqlite3")

    store.import_audit_jsonl(audit_path)
    calls = store.recent_calls(limit=10, sort_order="asc")

    assert [call["total_cost_microusd"] for call in calls] == [35_000_000, 14_000_000, 1_400_000]
    assert all(call["cost_status"] == "priced" for call in calls)
    assert all(call["pricing_version"] == "openai-standard-2026-08-11" for call in calls)


def test_v2_billing_persists_cache_aware_equivalent_and_actual_cost_separately(tmp_path: Path) -> None:
    audit_path = tmp_path / "route-audit.jsonl"
    audit_path.write_text(json.dumps({
        "audit_schema_version": 5,
        "timestamp": "2026-08-11T12:00:00+00:00",
        "status": "success",
        "response_model": "chatgpt/gpt-5.6-sol",
        "billing_mode": "chatgpt_subscription_included",
        "billing_mode_source": "router_profile",
        "observed_service_tier": "default",
        "response_cost": 0,
        "usage": {
            "input_tokens": 120_477,
            "cache_read_input_tokens": 3_584,
            "cache_write_tokens": 0,
            "output_tokens": 81,
            "reasoning_tokens": 20,
        },
    }) + "\n", encoding="utf-8")
    store = DashboardStore(tmp_path / "routerllm.sqlite3")

    store.import_audit_jsonl(audit_path)
    call = store.recent_calls(limit=1)[0]

    assert call["billing_model"] == "gpt-5.6-sol"
    assert call["pricing_match_source"] == "response_model"
    assert call["context_class"] == "short"
    assert call["regular_input_cost_microusd"] == 584_465
    assert call["cached_input_cost_microusd"] == 1_792
    assert call["output_cost_microusd"] == 2_430
    assert call["api_equivalent_cost_microusd"] == 588_687
    assert call["provider_reported_cost_microusd"] == 0
    assert call["actual_incremental_cost_microusd"] == 0
    assert call["api_equivalent_status"] == "estimated_complete"
    assert call["actual_incremental_status"] == "subscription_included"
    assert call["pricing_catalog_version"] == "openai-standard-2026-08-11"
    assert call["estimator_version"] == "v2"
    with sqlite3.connect(store.database_path) as connection:
        items = connection.execute(
            "SELECT item_type, amount_microusd FROM call_cost_items ORDER BY item_type"
        ).fetchall()
    assert items == [
        ("cache_write", 0),
        ("cached_input", 1_792),
        ("output", 2_430),
        ("regular_input", 584_465),
    ]
    detail = store.cost_call_detail(call["id"])
    assert detail is not None
    assert detail["estimate_disclosure"] == "complete_estimate"
    assert detail["billing_assumptions"] == []
    assert all(item["quantity_status"] == "observed" for item in detail["cost_items"])


def test_v2_billing_explains_legacy_missing_cache_as_assumed_zero(tmp_path: Path) -> None:
    audit_path = tmp_path / "route-audit.jsonl"
    audit_path.write_text(json.dumps({
        "audit_schema_version": 4,
        "timestamp": "2026-08-11T23:46:30.704682+00:00",
        "status": "success",
        "selected_alias": "terra-medium",
        "response_model": "gpt-5.6-terra",
        "usage": {"input_tokens": 112_285, "output_tokens": 1_685},
    }) + "\n", encoding="utf-8")
    store = DashboardStore(tmp_path / "routerllm.sqlite3")

    store.import_audit_jsonl(audit_path)
    call = store.recent_calls(limit=1)[0]
    detail = store.cost_call_detail(call["id"])

    assert detail is not None
    assert detail["known_api_equivalent_cost_microusd"] == 244_790
    assert detail["api_equivalent_cost_microusd"] is None
    assert detail["api_equivalent_status"] == "legacy_partial"
    assert detail["estimate_disclosure"] == "partial_estimate_missing_cache_assumed_zero"
    assert detail["billing_warning_codes"] == ["historical_v2_signals_incomplete"]
    assert {item["code"] for item in detail["billing_assumptions"]} == {
        "cached_input_tokens_missing_assumed_zero",
        "cache_write_tokens_missing_assumed_zero",
    }
    items = {item["item_type"]: item for item in detail["cost_items"]}
    assert items["regular_input"]["quantity"] == 112_285
    assert items["regular_input"]["amount_microusd"] == 224_570
    assert items["output"]["quantity"] == 1_685
    assert items["output"]["amount_microusd"] == 20_220
    for item_type in ("cached_input", "cache_write"):
        assert items[item_type]["quantity"] is None
        assert items[item_type]["quantity_status"] == "unknown"
        assert items[item_type]["assumed_quantity"] == 0
        assert items[item_type]["amount_microusd"] == 0
        assert items[item_type]["amount_status"] == "assumed_zero_for_partial_estimate"


def test_v2_billing_keeps_missing_usage_unknown_and_uses_long_context_rates(tmp_path: Path) -> None:
    audit_path = tmp_path / "route-audit.jsonl"
    records = [
        {
            "audit_schema_version": 5,
            "timestamp": "2026-08-11T12:00:00+00:00",
            "status": "success",
            "response_model": "gpt-5.6-luna",
            "usage": {
                "input_tokens": 272_001,
                "cache_read_input_tokens": 0,
                "cache_write_tokens": 0,
                "output_tokens": 1_000,
            },
        },
        {
            "audit_schema_version": 5,
            "timestamp": "2026-08-11T12:01:00+00:00",
            "status": "success",
            "response_model": "gpt-5.6-sol",
            "usage": {},
        },
    ]
    audit_path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    store = DashboardStore(tmp_path / "routerllm.sqlite3")

    store.import_audit_jsonl(audit_path)
    calls = store.recent_calls(limit=2, sort_order="asc")

    assert calls[0]["context_class"] == "long"
    assert calls[0]["api_equivalent_cost_microusd"] == 110_600
    assert calls[1]["api_equivalent_status"] == "usage_missing"
    assert calls[1]["api_equivalent_cost_microusd"] is None
    assert calls[1]["actual_incremental_cost_microusd"] is None
