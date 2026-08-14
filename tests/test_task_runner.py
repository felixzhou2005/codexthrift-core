from __future__ import annotations

import json
from pathlib import Path

from codexthrift.task_runner import (
    _detect_test_commands,
    _looks_like_infrastructure_failure,
    _sanitized_env,
)


def _names(commands: list[tuple[str, list[str]]]) -> list[str]:
    return [name for name, _ in commands]


def test_auto_does_not_assume_every_pyproject_uses_pytest(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    commands, _ = _detect_test_commands(tmp_path, "auto")
    assert "python" not in _names(commands)
    assert _names(commands) == ["git-diff-check", "git-index-diff-check"]


def test_auto_detects_pytest_and_plain_npm_test(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest run"}}),
        encoding="utf-8",
    )
    commands, _ = _detect_test_commands(tmp_path, "auto")
    by_name = dict(commands)
    assert "python" in by_name
    assert by_name["node"][-1] == "test"
    assert "--runInBand" not in by_name["node"]


def test_none_still_runs_nonexecuting_diff_checks(tmp_path: Path) -> None:
    commands, mode = _detect_test_commands(tmp_path, "none")
    assert mode == "none"
    assert _names(commands) == ["git-diff-check", "git-index-diff-check"]


def test_sanitized_env_drops_api_secrets(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "should-not-leak")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "should-not-leak")
    monkeypatch.setenv("CODEX_ROUTER_HOME", "/tmp/router")
    env = _sanitized_env(for_tests=True)
    assert "OPENAI_API_KEY" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert env["CODEX_ROUTER_HOME"] == "/tmp/router"
    assert env["CI"] == "1"


def test_infrastructure_failure_detection() -> None:
    assert _looks_like_infrastructure_failure({"stderr": "OAuth token expired"})
    assert not _looks_like_infrastructure_failure({"stderr": "unit test expected 3 but got 4"})


def test_codex_exec_uses_supported_noninteractive_approval_override(
    tmp_path: Path, monkeypatch
) -> None:
    from codexthrift import task_runner

    captured: dict[str, object] = {}
    monkeypatch.setattr(task_runner.shutil, "which", lambda name: "/usr/local/bin/codex" if name == "codex" else None)

    def fake_run(command, **kwargs):
        captured["command"] = command
        return {
            "command": command,
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "duration_seconds": 0.1,
            "timed_out": False,
        }

    monkeypatch.setattr(task_runner, "_run", fake_run)
    attempt_dir = tmp_path / "attempt"
    attempt_dir.mkdir()
    record = task_runner._run_codex(tmp_path, "make a safe edit", "codex-task-auto", attempt_dir)
    command = captured["command"]
    assert isinstance(command, list)
    assert "--ask-for-approval" not in command
    assert 'approval_policy="never"' in command
    assert 'model_provider="openai"' in command
    assert "sandbox_workspace_write.network_access=false" in command
    assert command[0:2] == ["/usr/local/bin/codex", "exec"]
    assert record["returncode"] == 0


def test_execute_escalates_once_after_functional_verification_failure(
    tmp_path: Path, monkeypatch
) -> None:
    from codexthrift import task_runner

    repo = tmp_path / "repo"
    repo.mkdir()
    spec_path = tmp_path / "spec.json"
    result_path = tmp_path / "result.json"
    spec_path.write_text(
        json.dumps(
            {
                "task_id": "task-1",
                "repo_path": str(repo),
                "task": "fix the behavior",
                "test_profile": "none",
                "max_escalations": 1,
            }
        ),
        encoding="utf-8",
    )
    models: list[str] = []
    verification_calls = 0

    def fake_codex(repo_path, prompt, model, attempt_dir):
        models.append(model)
        return {
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "duration_seconds": 0.1,
            "timed_out": False,
            "model": model,
        }

    def fake_tests(repo_path, commands, attempt_dir):
        nonlocal verification_calls
        verification_calls += 1
        code = 1 if verification_calls == 1 else 0
        return [{"name": "verification", "returncode": code, "stdout": "", "stderr": "failed" if code else ""}]

    monkeypatch.setattr(task_runner, "_run_codex", fake_codex)
    monkeypatch.setattr(task_runner, "_run_tests", fake_tests)
    monkeypatch.setattr(task_runner, "_git_snapshot", lambda repo_path: {"head": "abc", "status_porcelain": "", "diff_stat": ""})

    result = task_runner.execute(spec_path, result_path)
    assert models == ["codex-task-auto", "sol-xhigh"]
    assert result["success"] is True
    assert result["attempt_count"] == 2
    assert result["escalations_used"] == 1


def test_execute_does_not_escalate_infrastructure_failure(tmp_path: Path, monkeypatch) -> None:
    from codexthrift import task_runner

    repo = tmp_path / "repo"
    repo.mkdir()
    spec_path = tmp_path / "spec.json"
    result_path = tmp_path / "result.json"
    spec_path.write_text(
        json.dumps(
            {
                "task_id": "task-2",
                "repo_path": str(repo),
                "task": "fix the behavior",
                "test_profile": "none",
                "max_escalations": 1,
            }
        ),
        encoding="utf-8",
    )
    models: list[str] = []

    def fake_codex(repo_path, prompt, model, attempt_dir):
        models.append(model)
        return {
            "returncode": 1,
            "stdout": "",
            "stderr": "OAuth token expired",
            "duration_seconds": 0.1,
            "timed_out": False,
            "model": model,
        }

    monkeypatch.setattr(task_runner, "_run_codex", fake_codex)
    monkeypatch.setattr(task_runner, "_git_snapshot", lambda repo_path: {"head": "abc", "status_porcelain": "", "diff_stat": ""})

    result = task_runner.execute(spec_path, result_path)
    assert models == ["codex-task-auto"]
    assert result["success"] is False
    assert result["attempt_count"] == 1
    assert "escalation_skipped_reason" in result
