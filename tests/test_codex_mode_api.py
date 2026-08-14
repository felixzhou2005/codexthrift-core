from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from codexthrift.dashboard_api import create_dashboard_app
from codexthrift.dashboard_store import DashboardStore
from codexthrift.paths import WorkspacePaths
from codexthrift.quota_adapter import QuotaAdapter


class _ModeController:
    def __init__(self, paths: WorkspacePaths):
        self.paths = paths
        self.smart_calls: list[dict[str, object]] = []
        self.native_calls = 0

    def codex_mode_status(self) -> dict[str, object]:
        return {
            "client_mode": "native_openai",
            "config_path": str(self.paths.codex_config),
            "codex_home": str(self.paths.codex_home),
            "proxy_running": True,
            "proxy_used_by_codex": False,
            "restart_required": False,
        }

    def enable_smart_routing(self, *, confirm: str, allow_unverified: bool) -> dict[str, object]:
        self.smart_calls.append({"confirm": confirm, "allow_unverified": allow_unverified})
        return {"ok": True, "generation": 2, "proxy": {"ok": True}, "mode": self.codex_mode_status()}

    def switch_to_native_codex(self, *, confirm: str) -> dict[str, object]:
        self.native_calls += 1
        return {"ok": True, "generation": 3, "mode": self.codex_mode_status()}


def test_codex_mode_api_uses_explicit_confirmation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    paths = WorkspacePaths(tmp_path / "router-home")
    controller = _ModeController(paths)
    app = create_dashboard_app(
        controller=controller,
        store=DashboardStore(tmp_path / "routerllm.sqlite3"),
        quota=QuotaAdapter(),
        access_token="test-token",
    )
    headers = {"Authorization": "Bearer test-token"}
    with TestClient(app) as client:
        status = client.get("/v1/codex-mode", headers=headers)
        bad = client.post("/v1/codex-mode/smart", headers=headers, json={"confirm": "APPLY"})
        smart = client.post(
            "/v1/codex-mode/smart",
            headers=headers,
            json={"confirm": "ENABLE_ROUTER", "allow_unverified": True},
        )
        native = client.post(
            "/v1/codex-mode/native",
            headers=headers,
            json={"confirm": "SWITCH_NATIVE"},
        )
        bad_recovery = client.post(
            "/v1/codex-sessions/recover-unification",
            headers=headers,
            json={"confirm": "SWITCH_NATIVE"},
        )
        recovery = client.post(
            "/v1/codex-sessions/recover-unification",
            headers=headers,
            json={"confirm": "RESTORE_SESSION_UNIFICATION"},
        )

    assert status.json()["client_mode"] == "native_openai"
    assert bad.status_code == 400
    assert smart.status_code == 200
    assert controller.smart_calls == [{"confirm": "ENABLE_ROUTER", "allow_unverified": True}]
    assert native.status_code == 200
    assert controller.native_calls == 2
    assert bad_recovery.status_code == 400
    assert recovery.status_code == 200
    assert recovery.json()["recovery"] == {
        "managed_config_restored": True,
        "session_data_modified": False,
        "migration_copies_preserved": True,
        "client_files_modified": False,
    }
