from __future__ import annotations

from copy import deepcopy

from codexthrift.application.session_continuity_service import (
    CodexSessionContinuityService,
)


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.closed = False

    def request(self, method, params):
        self.requests.append((method, deepcopy(params)))
        return deepcopy(self.responses.pop(0))

    def close(self):
        self.closed = True


def test_snapshot_queries_all_providers_and_archived_without_exposing_session_ids() -> None:
    session = _Session([
        {
            "data": [
                {"id": "native-session", "modelProvider": "openai", "title": "private"},
                {"id": "routed-session", "modelProvider": "litellm", "cwd": "/private"},
            ],
            "nextCursor": "page-2",
        },
        {"data": [{"id": "native-session-2", "modelProvider": "openai"}]},
        {"data": [{"id": "archived-session", "modelProvider": "litellm"}]},
    ])
    service = CodexSessionContinuityService(session_factory=lambda: session)

    snapshot = service.capture()

    assert snapshot["available"] is True
    assert snapshot["active"]["count"] == 3
    assert snapshot["active"]["provider_counts"] == {"litellm": 1, "openai": 2}
    assert snapshot["archived"]["count"] == 1
    assert snapshot["combined_count"] == 4
    assert len(snapshot["combined_id_hashes"]) == 4
    assert "native-session" not in repr(snapshot)
    assert "private" not in repr(snapshot)
    assert session.requests == [
        ("thread/list", {"archived": False, "limit": 100, "modelProviders": []}),
        (
            "thread/list",
            {
                "archived": False,
                "cursor": "page-2",
                "limit": 100,
                "modelProviders": [],
            },
        ),
        ("thread/list", {"archived": True, "limit": 100, "modelProviders": []}),
    ]
    assert session.closed is True


def test_verify_detects_missing_history_but_allows_native_telemetry_to_be_empty() -> None:
    service = CodexSessionContinuityService(session_factory=lambda: _Session([]))
    before = {
        "available": True,
        "active": {"count": 2, "ids_hash": "active-before", "provider_counts": {"openai": 1, "litellm": 1}},
        "archived": {"count": 1, "ids_hash": "archived", "provider_counts": {"openai": 1}},
        "combined_count": 3,
        "combined_ids_hash": "combined-before",
        "clients": {},
    }
    after = {
        **before,
        "active": {"count": 1, "ids_hash": "active-after", "provider_counts": {"openai": 1}},
        "combined_count": 2,
        "combined_ids_hash": "combined-after",
    }

    result = service.verify(before, after)

    assert result["verified"] is False
    assert result["session_set_unchanged"] is False
    assert result["native_telemetry_required"] is False
    assert "missing" in result["reason"]


def test_capture_failure_is_an_actionable_unavailable_state() -> None:
    class _BrokenSession:
        def request(self, _method, _params):
            raise RuntimeError("Bearer private-token should not escape")

        def close(self):
            pass

    service = CodexSessionContinuityService(session_factory=_BrokenSession)

    snapshot = service.capture()
    result = service.verify(snapshot, snapshot)

    assert snapshot["available"] is False
    assert "private-token" not in snapshot["error"]
    assert result["verified"] is False
    assert result["reason"] == "session_inventory_unavailable"


def test_verify_allows_new_sessions_when_every_previous_id_hash_remains() -> None:
    service = CodexSessionContinuityService(session_factory=lambda: _Session([]))
    before = {
        "available": True,
        "combined_count": 2,
        "combined_ids_hash": "before",
        "combined_id_hashes": ["hash-a", "hash-b"],
    }
    after = {
        "available": True,
        "combined_count": 3,
        "combined_ids_hash": "after",
        "combined_id_hashes": ["hash-a", "hash-b", "hash-c"],
    }

    result = service.verify(before, after)

    assert result["verified"] is True
    assert result["session_set_unchanged"] is False
    assert result["added_count"] == 1
    assert result["missing_count"] == 0
    assert result["reason"] == "verified_with_new_sessions"
