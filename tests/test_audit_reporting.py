from __future__ import annotations

import json
from pathlib import Path

from codexthrift.paths import WorkspacePaths
from codexthrift.reporting import (
    explain_turn,
    recent_calls,
    recent_turns,
    routing_report,
)


def _paths(tmp_path: Path) -> WorkspacePaths:
    paths = WorkspacePaths(tmp_path / "router")
    paths.ensure()
    return paths


def _write_rows(paths: WorkspacePaths, rows: list[dict]) -> None:
    paths.audit_log.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_report_counts_one_user_turn_instead_of_internal_calls(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    rows = []
    for index in range(6):
        rows.append(
            {
                "audit_schema_version": 2,
                "timestamp": f"2026-08-04T20:00:0{index}+00:00",
                "status": "success",
                "logical_turn_id": "turn-a",
                "request_kind": "user_turn" if index == 0 else "tool_continuation",
                "selected_tier": "COMPLEX",
                "selected_alias": "sol-medium",
                "provider_model": "chatgpt/gpt-5.6-sol",
                "reasoning_effort": "medium",
                "duration_ms": 100,
            }
        )
    _write_rows(paths, rows)
    report = routing_report(paths, unit="turn")
    assert report["logical_turns"] == 1
    assert report["successful"] == 1
    assert report["turn_tier_counts"] == {"COMPLEX": 1}
    assert report["call_tier_counts"] == {"COMPLEX": 6}
    turn = recent_turns(paths, limit=5)["records"][0]
    assert turn["internal_calls"] == 6
    assert turn["continuation_calls"] == 5


def test_legacy_calls_are_marked_excluded_not_falsely_grouped(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _write_rows(
        paths,
        [
            {
                "timestamp": "2026-08-04T20:00:00+00:00",
                "status": "success",
                "selected_model": "chatgpt/gpt-5.6-terra",
            }
            for _ in range(12)
        ],
    )
    report = routing_report(paths, unit="turn")
    assert report["logical_turns"] == 0
    assert report["legacy_call_records_excluded_from_turns"] == 12
    assert any("病态集中" in item for item in report["recommendations"])


def test_recent_calls_and_explain_do_not_return_prompt_or_error_body(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _write_rows(
        paths,
        [
            {
                "audit_schema_version": 2,
                "timestamp": "2026-08-04T20:00:00+00:00",
                "status": "failure",
                "logical_turn_id": "turn-safe",
                "selected_tier": "MEDIUM",
                "selected_alias": "terra-medium",
                "prompt_fingerprint": "abc123",
                "prompt": "must-not-return",
                "error": "Bearer must-not-return",
                "error_type": "ExampleError",
            }
        ],
    )
    payloads = [recent_calls(paths), explain_turn(paths, turn_id="turn-safe")]
    serialized = json.dumps(payloads)
    assert "must-not-return" not in serialized
    assert "abc123" in serialized
