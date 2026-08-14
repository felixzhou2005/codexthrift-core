from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from codexthrift.controller import RouterController
from codexthrift.daemon.desired_state import DesiredStateStore
from codexthrift.daemon.supervisor import DaemonSupervisor
from codexthrift.errors import ControlError
from codexthrift.paths import WorkspacePaths


class FakeProxyController:
    def __init__(self, *, healthy: bool = True, start_ok: bool = True) -> None:
        self.healthy = healthy
        self.start_ok = start_ok
        self.start_calls: list[dict[str, Any]] = []

    def start_proxy(self, *, confirm: str, allow_unverified: bool = False) -> dict[str, Any]:
        self.start_calls.append(
            {"confirm": confirm, "allow_unverified": allow_unverified}
        )
        return {"ok": self.start_ok}

    def proxy_status(self) -> dict[str, Any]:
        return {
            "running": self.healthy,
            "pid": 4242 if self.healthy else None,
            "health": {"ok": self.healthy},
        }


def test_desired_state_defaults_and_persists_securely(tmp_path: Path) -> None:
    store = DesiredStateStore(tmp_path / "data" / "desired-state.json")

    assert store.load()["proxy"] == {
        "desired": "stopped",
        "allowUnverified": False,
    }

    state = store.set_proxy_desired(running=True, allow_unverified=True)

    assert state["proxy"] == {"desired": "running", "allowUnverified": True}
    assert store.load()["proxy"] == state["proxy"]
    if os.name != "nt":
        assert store.path.stat().st_mode & 0o777 == 0o600


def test_corrupt_desired_state_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "desired-state.json"
    path.write_text("not-json", encoding="utf-8")
    store = DesiredStateStore(path)

    with pytest.raises(ControlError, match="不会自动启动"):
        store.load()

    status = store.status()
    assert status["valid"] is False
    assert status["proxy"]["desired"] == "stopped"


def test_supervisor_restores_requested_proxy_and_records_result(tmp_path: Path) -> None:
    store = DesiredStateStore(tmp_path / "desired-state.json")
    store.set_proxy_desired(running=True, allow_unverified=True)
    controller = FakeProxyController()
    supervisor = DaemonSupervisor(controller, store)

    result = supervisor.restore()

    assert result["status"] == "restored"
    assert controller.start_calls == [{"confirm": "APPLY", "allow_unverified": True}]
    assert store.load()["lastRecovery"]["status"] == "restored"


def test_supervisor_keeps_daemon_available_when_restore_fails(tmp_path: Path) -> None:
    store = DesiredStateStore(tmp_path / "desired-state.json")
    store.set_proxy_desired(running=True)
    supervisor = DaemonSupervisor(FakeProxyController(healthy=False), store)

    result = supervisor.restore()

    assert result["status"] == "failed"
    assert "认证健康检查" in result["message"]
    assert supervisor.status()["startup"]["status"] == "failed"
    assert store.load()["proxy"]["desired"] == "running"


def test_controller_persists_only_successful_proxy_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = DesiredStateStore(tmp_path / "desired-state.json")
    controller = RouterController(WorkspacePaths(tmp_path / "workspace"))
    controller.bind_proxy_desired_state_writer(
        lambda running, allow_unverified: store.set_proxy_desired(
            running=running,
            allow_unverified=allow_unverified,
        )
    )
    monkeypatch.setattr(controller.services.proxy, "start_proxy", lambda **_params: {"ok": True})
    monkeypatch.setattr(controller.services.proxy, "restart_proxy", lambda **_params: {"ok": False})
    monkeypatch.setattr(controller.services.proxy, "stop_proxy", lambda **_params: {"ok": True})

    controller.start_proxy(confirm="APPLY", allow_unverified=True)
    assert store.load()["proxy"] == {"desired": "running", "allowUnverified": True}

    controller.restart_proxy(confirm="APPLY")
    assert store.load()["proxy"] == {"desired": "running", "allowUnverified": True}

    controller.stop_proxy(confirm="APPLY")
    assert store.load()["proxy"] == {"desired": "stopped", "allowUnverified": False}
