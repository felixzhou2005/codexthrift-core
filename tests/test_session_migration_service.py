from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from codexthrift.application.session_migration_service import SessionMigrationService
from codexthrift.paths import WorkspacePaths


class FakeSession:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    def request(self, method: str, params: Any = None) -> dict[str, Any]:
        self.requests.append((method, dict(params or {})))
        if params.get("archived"):
            return {
                "data": [
                    {"id": "legacy-2", "modelProvider": "litellm", "name": "private"},
                    {"id": "other-1", "modelProvider": "custom"},
                ]
            }
        if params.get("cursor") == "page-2":
            return {
                "data": [
                    {"id": "legacy-2", "modelProvider": "litellm"},
                    {"id": "native-1", "modelProvider": "openai"},
                ]
            }
        return {
            "data": [{"id": "legacy-1", "modelProvider": "litellm"}],
            "nextCursor": "page-2",
        }

    def close(self) -> None:
        self.closed = True


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def test_preview_is_read_only_private_paginated_and_idempotent(tmp_path: Path) -> None:
    paths = WorkspacePaths(tmp_path)
    paths.data_dir.mkdir(parents=True)
    paths.session_migration_ledger.write_text(
        json.dumps(
            {
                "version": 1,
                "migrations": [
                    {
                        "old_id_hash": _digest("legacy-1"),
                        "new_id_hash": _digest("native-1"),
                        "state": "completed",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    sessions: list[FakeSession] = []

    def factory() -> FakeSession:
        session = FakeSession()
        sessions.append(session)
        return session

    service = SessionMigrationService(paths, session_factory=factory)
    first = service.preview()
    second = service.preview()

    assert first == second
    assert first == {
        "available": True,
        "source": "codex_app_server",
        "total_count": 4,
        "provider_counts": {"custom": 1, "litellm": 2, "openai": 1},
        "openai_count": 1,
        "legacy_litellm_count": 2,
        "other_provider_count": 1,
        "already_migrated_count": 1,
        "needs_migration_count": 1,
        "projected_openai_copies": 1,
        "needs_verification_count": 0,
        "ledger_state": "available",
        "migration_ready": True,
        "originals_preserved": True,
        "client_files_modified": False,
        "content_read": False,
        "preview_created_sessions": False,
        "scope": "active_and_archived",
    }
    assert all(session.closed for session in sessions)
    assert all(method == "thread/list" for session in sessions for method, _ in session.requests)
    assert all(
        params["modelProviders"] == [] and params["limit"] == 100
        for session in sessions
        for _, params in session.requests
    )
    serialized = json.dumps(first)
    for private_value in ("legacy-1", "legacy-2", "native-1", "other-1", "private"):
        assert private_value not in serialized


def test_preview_does_not_create_ledger_and_masks_app_server_errors(tmp_path: Path) -> None:
    paths = WorkspacePaths(tmp_path)

    class FailedSession:
        def request(self, method: str, params: Any = None) -> dict[str, Any]:
            raise RuntimeError("secret /Users/person/.codex/sessions")

        def close(self) -> None:
            pass

    result = SessionMigrationService(paths, session_factory=FailedSession).preview()

    assert result["available"] is False
    assert result["error_code"] == "codex_app_server_unavailable"
    assert "secret" not in json.dumps(result)
    assert not paths.session_migration_ledger.exists()
    assert not paths.data_dir.exists()


class MigrationSession:
    def __init__(self, *, existing_copy: bool = False, fail_fork: bool = False) -> None:
        self.fail_fork = fail_fork
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.threads: dict[str, dict[str, Any]] = {
            "legacy-1": {"modelProvider": "litellm", "turns": [{}, {}]},
            "native-1": {"modelProvider": "openai", "turns": [{}]},
        }
        if existing_copy:
            self.threads["copy-1"] = {
                "modelProvider": "openai",
                "forkedFromId": "legacy-1",
                "turns": [{}, {}],
            }

    def request(self, method: str, params: Any = None) -> dict[str, Any]:
        request = dict(params or {})
        self.calls.append((method, request))
        if method == "thread/list":
            if request.get("archived"):
                return {"data": []}
            return {
                "data": [
                    {
                        "id": thread_id,
                        "modelProvider": item["modelProvider"],
                        "forkedFromId": item.get("forkedFromId"),
                    }
                    for thread_id, item in self.threads.items()
                ]
            }
        if method == "thread/read":
            thread_id = request["threadId"]
            item = self.threads[thread_id]
            return {
                "thread": {
                    "id": thread_id,
                    "modelProvider": item["modelProvider"],
                    "turns": list(item["turns"]),
                }
            }
        if method == "thread/turns/list":
            return {"data": list(self.threads[request["threadId"]]["turns"])}
        if method == "thread/fork":
            if self.fail_fork:
                raise RuntimeError("private failure text")
            source = request["threadId"]
            self.threads["copy-1"] = {
                "modelProvider": request["modelProvider"],
                "forkedFromId": source,
                "turns": list(self.threads[source]["turns"]),
            }
            return {"thread": {"id": "copy-1"}}
        raise AssertionError(method)

    def close(self) -> None:
        pass


def test_migrate_next_forks_verifies_and_never_repeats_completed_source(tmp_path: Path) -> None:
    paths = WorkspacePaths(tmp_path)
    session = MigrationSession()
    service = SessionMigrationService(paths, session_factory=lambda: session)

    first = service.migrate_next(confirm="FORK_LEGACY_CODEX_SESSIONS")
    second = service.migrate_next(confirm="FORK_LEGACY_CODEX_SESSIONS")

    assert first["ok"] is True
    assert first["result_code"] == "fork_verified"
    assert first["preview"]["needs_migration_count"] == 0
    assert second["result_code"] == "nothing_to_migrate"
    assert [method for method, _ in session.calls].count("thread/fork") == 1
    fork_params = next(params for method, params in session.calls if method == "thread/fork")
    assert fork_params["ephemeral"] is False
    ledger_text = paths.session_migration_ledger.read_text(encoding="utf-8")
    ledger = json.loads(ledger_text)
    assert ledger["migrations"][0]["state"] == "completed"
    assert ledger["migrations"][0]["source_turn_count"] == 2
    assert ledger["migrations"][0]["copy_turn_count"] == 2
    for private_value in ("legacy-1", "copy-1", "native-1", "private failure text"):
        assert private_value not in ledger_text
        assert private_value not in json.dumps(first)


def test_migrate_next_recovers_existing_fork_without_duplicate(tmp_path: Path) -> None:
    session = MigrationSession(existing_copy=True)
    service = SessionMigrationService(WorkspacePaths(tmp_path), session_factory=lambda: session)

    result = service.migrate_next(confirm="FORK_LEGACY_CODEX_SESSIONS")

    assert result["ok"] is True
    assert result["result_code"] == "recovered_existing_copy"
    assert all(method != "thread/fork" for method, _ in session.calls)


def test_migrate_next_records_only_safe_failure_codes_and_requires_confirmation(tmp_path: Path) -> None:
    paths = WorkspacePaths(tmp_path)
    session = MigrationSession(fail_fork=True)
    service = SessionMigrationService(paths, session_factory=lambda: session)

    rejected = service.migrate_next(confirm="wrong")
    failed = service.migrate_next(confirm="FORK_LEGACY_CODEX_SESSIONS")

    assert rejected["result_code"] == "confirmation_required"
    assert failed["result_code"] == "fork_failed"
    serialized = json.dumps(failed)
    ledger_text = paths.session_migration_ledger.read_text(encoding="utf-8")
    assert "private failure text" not in serialized
    assert "private failure text" not in ledger_text
    assert "legacy-1" not in ledger_text


def test_migrate_next_blocks_invalid_ledger_without_app_server_call(tmp_path: Path) -> None:
    paths = WorkspacePaths(tmp_path)
    paths.data_dir.mkdir(parents=True)
    paths.session_migration_ledger.write_text("not-json", encoding="utf-8")
    calls = 0

    def factory() -> MigrationSession:
        nonlocal calls
        calls += 1
        return MigrationSession()

    result = SessionMigrationService(paths, session_factory=factory).migrate_next(
        confirm="FORK_LEGACY_CODEX_SESSIONS"
    )

    assert result["result_code"] == "migration_ledger_invalid"
    assert calls == 0


def test_completed_ledger_entry_is_retried_when_copy_is_not_persisted(tmp_path: Path) -> None:
    paths = WorkspacePaths(tmp_path)
    paths.data_dir.mkdir(parents=True)
    paths.session_migration_ledger.write_text(
        json.dumps(
            {
                "version": 1,
                "migrations": [
                    {
                        "old_id_hash": _digest("legacy-1"),
                        "new_id_hash": _digest("missing-copy"),
                        "state": "completed",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    session = MigrationSession()
    service = SessionMigrationService(paths, session_factory=lambda: session)

    assert service.preview()["needs_migration_count"] == 1
    result = service.migrate_next(confirm="FORK_LEGACY_CODEX_SESSIONS")

    assert result["result_code"] == "copy_not_persisted"
    assert [method for method, _ in session.calls].count("thread/fork") == 0


def test_new_fork_must_survive_a_fresh_app_server_session(tmp_path: Path) -> None:
    sessions: list[MigrationSession] = []

    def factory() -> MigrationSession:
        session = MigrationSession()
        sessions.append(session)
        return session

    result = SessionMigrationService(
        WorkspacePaths(tmp_path), session_factory=factory
    ).migrate_next(confirm="FORK_LEGACY_CODEX_SESSIONS")

    assert result["ok"] is False
    assert result["result_code"] == "copy_not_persisted"
    assert [method for method, _ in sessions[0].calls].count("thread/fork") == 1
