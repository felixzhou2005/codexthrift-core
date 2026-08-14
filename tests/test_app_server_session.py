from __future__ import annotations

from codexthrift.app_server_session import CodexAppServerSession, codex_child_env


def test_codex_child_env_removes_only_codexthrift_runtime_switches() -> None:
    env = codex_child_env(
        {
            "PATH": "/usr/bin",
            "CODEX_HOME": "/tmp/codex-home",
            "CODEXTHRIFT_MACOS_APP_DATA": "1",
            "CODEXTHRIFT_RUNTIME_ROOT": "/tmp/runtime",
        }
    )

    assert env["PATH"] == "/usr/bin"
    assert env["CODEX_HOME"] == "/tmp/codex-home"
    assert "CODEXTHRIFT_MACOS_APP_DATA" not in env
    assert "CODEXTHRIFT_RUNTIME_ROOT" not in env


def test_experimental_api_is_opt_in() -> None:
    assert CodexAppServerSession().experimental_api is False
    assert CodexAppServerSession(experimental_api=True).experimental_api is True
