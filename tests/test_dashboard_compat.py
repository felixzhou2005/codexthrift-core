from __future__ import annotations

from pathlib import Path

import pytest

from codexthrift.controller import RouterController
from codexthrift.daemon.dashboard_compat import DashboardCompatService
from codexthrift.errors import ControlError
from codexthrift.paths import WorkspacePaths


def test_dashboard_compat_runs_inside_daemon_without_tcp(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    controller = RouterController(
        WorkspacePaths(tmp_path / "router-home"),
        ensure_paths=True,
    )
    service = DashboardCompatService(controller)
    try:
        health = service.request(method="GET", path="/healthz")
        assert health["ok"] is True
    finally:
        service.close()


def test_dashboard_compat_rejects_unapproved_transport_shape(tmp_path: Path) -> None:
    controller = RouterController(WorkspacePaths(tmp_path / "router-home"), ensure_paths=True)
    service = DashboardCompatService(controller)
    try:
        with pytest.raises(ControlError):
            service.request(method="DELETE", path="/v1/overview")
        with pytest.raises(ControlError):
            service.request(method="GET", path="/private")
    finally:
        service.close()
