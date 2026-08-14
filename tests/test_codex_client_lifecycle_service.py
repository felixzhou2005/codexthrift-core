from __future__ import annotations

import pytest

from codexthrift.application.codex_client_lifecycle_service import (
    CodexClientLifecycleService,
)
from codexthrift.errors import ControlError


def _client(
    *,
    pid: int = 42,
    create_time: float = 100.0,
    kind: str = "codex_cli",
    fingerprint: str = "a" * 64,
    override: bool = False,
) -> dict[str, object]:
    return {
        "pid": pid,
        "create_time": create_time,
        "kind": kind,
        "command_fingerprint": fingerprint,
        "has_config_override": override,
        "eligible_for_termination": not override,
        "restart_behavior": (
            "user_relaunch_required" if kind == "codex_cli" else "host_managed_respawn"
        ),
    }


class _FakeProcess:
    def __init__(self, pid: int, create_time: float, *, alive_after_term: bool = False) -> None:
        self.pid = pid
        self._create_time = create_time
        self.alive_after_term = alive_after_term
        self.terminated = False
        self.killed = False

    def create_time(self) -> float:
        return self._create_time

    def children(self, recursive: bool = False) -> list[object]:
        assert recursive is True
        return []

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


def test_terminate_selected_rechecks_exact_identity_and_uses_sigterm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = _client()
    process = _FakeProcess(42, 100.0)
    service = CodexClientLifecycleService(
        detector=lambda **_kwargs: {
            "clients": [expected],
            "detection_status": "available",
        },
        identity_reader=lambda _pid: expected,
        process_factory=lambda _pid: process,
    )
    monkeypatch.setattr(
        "codexthrift.application.codex_client_lifecycle_service.psutil.wait_procs",
        lambda targets, timeout: (targets, []),
    )

    result = service.terminate_selected([expected], timeout=0.01)

    assert result["ok"] is True
    assert result["terminated"][0]["restart_behavior"] == "user_relaunch_required"
    assert process.terminated is True
    assert process.killed is False


def test_force_is_separate_from_graceful_termination(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = _client(kind="vscode_app_server")
    process = _FakeProcess(42, 100.0, alive_after_term=True)
    service = CodexClientLifecycleService(
        detector=lambda **_kwargs: {"clients": [expected], "detection_status": "available"},
        identity_reader=lambda _pid: expected,
        process_factory=lambda _pid: process,
    )
    calls = 0

    def wait_procs(targets, timeout):
        nonlocal calls
        calls += 1
        return (targets, [process]) if calls == 1 else (targets, [])

    monkeypatch.setattr(
        "codexthrift.application.codex_client_lifecycle_service.psutil.wait_procs",
        wait_procs,
    )

    result = service.terminate_selected([expected], force=True, timeout=0.01)

    assert result["ok"] is True
    assert process.terminated is True
    assert process.killed is True


def test_permission_limited_override_and_pid_reuse_are_rejected() -> None:
    expected = _client()
    limited = CodexClientLifecycleService(
        detector=lambda **_kwargs: {"clients": [expected], "detection_status": "limited"}
    )
    with pytest.raises(ControlError, match="权限受限"):
        limited.terminate_selected([expected])

    override = _client(override=True)
    overridden = CodexClientLifecycleService(
        detector=lambda **_kwargs: {"clients": [override], "detection_status": "available"},
        identity_reader=lambda _pid: override,
    )
    with pytest.raises(ControlError, match="独立 profile/config"):
        overridden.terminate_selected([override])

    reused = CodexClientLifecycleService(
        detector=lambda **_kwargs: {"clients": [expected], "detection_status": "available"},
        identity_reader=lambda _pid: _client(create_time=200.0),
    )
    with pytest.raises(ControlError, match="身份复核失败"):
        reused.terminate_selected([expected])


def test_convergence_uses_process_generation_not_only_pid() -> None:
    previous = _client(create_time=100.0)
    replacement = _client(create_time=200.0)
    service = CodexClientLifecycleService(
        detector=lambda **_kwargs: {
            "clients": [replacement],
            "detection_status": "available",
        }
    )

    result = service.convergence([previous])

    assert result["converged"] is True
    assert result["stale_clients"] == []
