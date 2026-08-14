from __future__ import annotations

import sqlite3

import pytest

from codexthrift.codex_sessions import launch_codex_session, list_codex_sessions
from codexthrift.errors import ControlError
from codexthrift.paths import WorkspacePaths


def _paths(tmp_path, monkeypatch) -> WorkspacePaths:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    paths = WorkspacePaths(tmp_path / "router-home")
    paths.codex_home.mkdir(parents=True)
    database = paths.codex_home / "state_5.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                model_provider TEXT NOT NULL,
                cwd TEXT NOT NULL,
                name TEXT,
                title TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                recency_at INTEGER NOT NULL,
                archived INTEGER NOT NULL,
                source TEXT NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("session-openai", "openai", "/openai", "Native", "", 1, 2, 2, 0, "vscode"),
                ("session-litellm", "litellm", "/litellm", None, "Routed", 2, 3, 3, 0, "cli"),
                ("session-agent", "litellm", "/agent", None, "Agent", 3, 4, 4, 0, "subagent"),
                ("session-archive", "openai", "/archive", None, "Archive", 4, 5, 5, 1, "vscode"),
            ],
        )
    return paths


def test_lists_interactive_sessions_across_all_providers(tmp_path, monkeypatch) -> None:
    paths = _paths(tmp_path, monkeypatch)

    result = list_codex_sessions(paths)

    assert result["provider_counts"] == {"litellm": 1, "openai": 1}
    assert [item["id"] for item in result["sessions"]] == [
        "session-litellm",
        "session-openai",
    ]
    assert result["sessions"][0]["resume_command"] == [
        "codex",
        "resume",
        "session-litellm",
    ]


def test_filters_provider_and_archived_sessions(tmp_path, monkeypatch) -> None:
    paths = _paths(tmp_path, monkeypatch)

    routed = list_codex_sessions(paths, provider="litellm")
    archived = list_codex_sessions(paths, archived=True)

    assert [item["id"] for item in routed["sessions"]] == ["session-litellm"]
    assert [item["id"] for item in archived["sessions"]] == ["session-archive"]


def test_launches_exact_session_id_with_native_codex_cli(tmp_path, monkeypatch) -> None:
    paths = _paths(tmp_path, monkeypatch)
    calls = []

    def fake_execvp(executable, argv):
        calls.append((executable, argv))
        raise OSError("test stop")

    with pytest.raises(ControlError, match="test stop"):
        launch_codex_session(paths, "session-litellm", execvp=fake_execvp)

    assert calls == [("codex", ["codex", "resume", "session-litellm"])]


def test_resume_rejects_unknown_or_invalid_session(tmp_path, monkeypatch) -> None:
    paths = _paths(tmp_path, monkeypatch)

    with pytest.raises(ControlError, match="格式无效"):
        launch_codex_session(paths, "bad id")
    with pytest.raises(ControlError, match="未找到"):
        launch_codex_session(paths, "session-missing")
