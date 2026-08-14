from __future__ import annotations

from types import SimpleNamespace

from codexthrift.application import QuotaService
from codexthrift.application.services import ApplicationServices
from codexthrift.controller import RouterController
from codexthrift.paths import WorkspacePaths


def test_controller_builds_focused_application_services(tmp_path) -> None:
    controller = RouterController(WorkspacePaths(tmp_path / "router-home"))

    assert isinstance(controller.services, ApplicationServices)
    assert controller.services.proxy._controller is controller
    assert controller.services.codex_mode._controller is controller
    assert controller.services.routing._controller is controller
    assert not controller.paths.root.exists()


def test_quota_service_preserves_adapter_contract() -> None:
    calls: list[tuple[str, object]] = []
    adapter = SimpleNamespace(
        close=lambda: calls.append(("close", None)),
        read_snapshot=lambda *, profile_id=None: {"profile_id": profile_id},
        has_account_update=lambda: True,
        start_login=lambda: {"status": "pending"},
        login_status=lambda login_id: {"login_id": login_id},
        cancel_login=lambda login_id: {"cancelled": login_id},
        logout=lambda: {"status": "logged_out"},
        consume_reset=lambda **kwargs: kwargs,
    )
    service = QuotaService(adapter)

    assert service.read_snapshot(profile_id="profile") == {"profile_id": "profile"}
    assert service.has_account_update() is True
    assert service.start_login() == {"status": "pending"}
    assert service.login_status("login") == {"login_id": "login"}
    assert service.cancel_login("login") == {"cancelled": "login"}
    assert service.logout() == {"status": "logged_out"}
    assert service.consume_reset(idempotency_key="request", credit_id="credit") == {
        "idempotency_key": "request",
        "credit_id": "credit",
    }
    service.close()
    assert calls == [("close", None)]
