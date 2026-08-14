from __future__ import annotations

import threading
import time

from codexthrift.application.mode_switch_coordinator import ModeSwitchCoordinator
from codexthrift.application.mode_switch_service import ModeSwitchService
from codexthrift.paths import WorkspacePaths


class _Clients:
    def __init__(self, observed=None, *, converged=True) -> None:
        self.observed = observed or []
        self.converged = converged
        self.terminated = []

    def preview(self, **_kwargs):
        return {
            "clients": self.observed,
            "counts": {},
            "detection_status": "available",
        }

    def convergence(self, observed):
        return {
            "detection_status": "available",
            "stale_clients": [] if self.converged else observed,
            "converged": self.converged,
        }

    def terminate_selected(self, selected, *, force=False):
        self.terminated.append((selected, force))
        return {"ok": True, "force": force, "terminated": selected, "still_running": []}


class _Continuity:
    def __init__(self, *, verified=True) -> None:
        self.verified = verified
        self.captures = 0

    def capture(self):
        self.captures += 1
        return {
            "available": True,
            "active": {"count": 2, "ids_hash": "active", "provider_counts": {"openai": 1, "litellm": 1}},
            "archived": {"count": 1, "ids_hash": "archived", "provider_counts": {"openai": 1}},
            "combined_count": 3,
            "combined_ids_hash": "combined" if self.verified else f"combined-{self.captures}",
            "clients": {
                "desktop": {"all_provider_history": True, "cross_provider_resume": True},
                "vscode": {"all_provider_history": True, "cross_provider_resume": True},
                "cli": {"all_provider_history": True, "cross_provider_resume": True},
            },
        }

    def verify(self, before, after):
        verified = before["combined_ids_hash"] == after["combined_ids_hash"]
        return {
            "verified": verified,
            "reason": "verified" if verified else "missing_sessions_after_mode_switch",
            "session_set_unchanged": verified,
            "native_telemetry_required": False,
            "before": before,
            "after": after,
        }


class _Controller:
    def __init__(
        self,
        paths,
        *,
        mode="native_openai",
        proxy=False,
        provider_managed=True,
    ) -> None:
        self.paths = paths
        self.mode = mode
        self.proxy = proxy
        self.provider_managed = provider_managed
        self.calls = []

    def codex_mode_status(self):
        return {
            "client_mode": self.mode,
            "proxy_running": self.proxy,
            "new_session_mode": "routed" if self.mode == "smart_router" else "native",
            "provider_managed_by_routerllm": self.provider_managed,
        }

    def proxy_status(self):
        return {
            "ok": True,
            "running": self.proxy,
            "health": {"ok": self.proxy},
            "pid": 99 if self.proxy else None,
        }

    def enable_smart_routing(self, *, confirm, allow_unverified=False):
        self.calls.append(("smart", confirm, allow_unverified))
        self.mode = "smart_router"
        self.proxy = True
        self.provider_managed = True
        return {"ok": True, "mode": self.codex_mode_status()}

    def switch_to_native_codex(self, *, confirm):
        self.calls.append(("native", confirm))
        self.mode = "native_openai"
        self.proxy = True
        self.provider_managed = True
        return {"ok": True, "mode": self.codex_mode_status()}

    def stop_proxy(self, *, confirm):
        self.calls.append(("stop", confirm))
        self.proxy = False
        return {"ok": True, "stopped_pid": 99}


def _service(
    tmp_path,
    *,
    mode="native_openai",
    proxy=False,
    provider_managed=True,
    clients=None,
    continuity=None,
):
    paths = WorkspacePaths(tmp_path / "router-home")
    paths.ensure_mutable()
    controller = _Controller(
        paths,
        mode=mode,
        proxy=proxy,
        provider_managed=provider_managed,
    )
    service = ModeSwitchService(
        controller,
        coordinator=ModeSwitchCoordinator(paths),
        clients=clients or _Clients(),
        continuity=continuity or _Continuity(),
        recover=False,
    )
    service._schedule = lambda *_args, **_kwargs: None
    return service, controller


def test_native_to_routed_runs_proxy_config_and_converges(tmp_path) -> None:
    service, controller = _service(tmp_path)
    job = service.create(
        target="routed",
        client_policy="wait",
        selected_clients=[],
        confirm="SWITCH_CODEX_MODE",
    )

    service._run(job["job_id"])
    result = service.status(job["job_id"])

    assert result["state"] == "completed"
    assert controller.calls == [("smart", "ENABLE_ROUTER", True)]
    assert result["result"]["mode"]["client_mode"] == "smart_router"
    assert result["result"]["proxy"]["running"] is True


def test_routed_to_native_keeps_proxy_in_standby(tmp_path) -> None:
    service, controller = _service(tmp_path, mode="smart_router", proxy=True)
    job = service.create(
        target="native",
        client_policy="wait",
        selected_clients=[],
        confirm="SWITCH_CODEX_MODE",
    )

    service._run(job["job_id"])
    result = service.status(job["job_id"])

    assert result["state"] == "completed"
    assert controller.calls == [("native", "SWITCH_NATIVE")]
    assert result["result"]["mode"]["client_mode"] == "native_openai"
    assert result["result"]["proxy"]["running"] is True


def test_wait_policy_persists_until_old_generation_exits(tmp_path) -> None:
    observed = [{
        "pid": 42,
        "create_time": 100.0,
        "kind": "codex_cli",
        "command_fingerprint": "a" * 64,
        "has_config_override": False,
        "eligible_for_termination": True,
        "restart_behavior": "user_relaunch_required",
    }]
    clients = _Clients(observed, converged=False)
    service, _ = _service(tmp_path, clients=clients)
    job = service.create(
        target="routed",
        client_policy="wait",
        selected_clients=[],
        confirm="SWITCH_CODEX_MODE",
    )
    service._run(job["job_id"])

    assert service.status(job["job_id"])["state"] == "waiting"
    clients.converged = True
    assert service.status(job["job_id"])["state"] == "completed"
    service.close()


def test_new_target_supersedes_waiting_switch_and_rebases_from_live_mode(tmp_path) -> None:
    observed = [{
        "pid": 42,
        "create_time": 100.0,
        "kind": "codex_cli",
        "command_fingerprint": "a" * 64,
        "has_config_override": False,
        "eligible_for_termination": True,
        "restart_behavior": "user_relaunch_required",
    }]
    clients = _Clients(observed, converged=False)
    service, controller = _service(tmp_path, clients=clients)
    first = service.create(
        target="routed",
        client_policy="wait",
        selected_clients=[],
        confirm="SWITCH_CODEX_MODE",
        replace_active=True,
    )
    service._run(first["job_id"])
    assert service.status(first["job_id"])["state"] == "waiting"
    assert controller.mode == "smart_router"

    replacement = service.create(
        target="native",
        client_policy="defer",
        selected_clients=[],
        confirm="SWITCH_CODEX_MODE",
        replace_active=True,
    )

    assert service.status(first["job_id"])["state"] == "superseded"
    assert replacement["supersedes"] == first["job_id"]
    assert replacement["superseded_job"]["job_id"] == first["job_id"]
    assert replacement["superseded_job"]["state"] == "superseded"
    assert replacement["waiting_for_safe_boundary"] is False
    assert replacement["snapshot"]["mode"]["client_mode"] == "smart_router"

    service._run(replacement["job_id"])
    result = service.status(replacement["job_id"])
    service.close()

    assert result["state"] == "action_required"
    assert result["result"]["mode"]["client_mode"] == "native_openai"
    assert controller.calls == [
        ("smart", "ENABLE_ROUTER", True),
        ("native", "SWITCH_NATIVE"),
    ]


def test_superseded_worker_cannot_complete_after_newer_intent(tmp_path) -> None:
    service, controller = _service(tmp_path)
    first = service.create(
        target="routed",
        client_policy="wait",
        selected_clients=[],
        confirm="SWITCH_CODEX_MODE",
        replace_active=True,
    )
    replacement = service.create(
        target="native",
        client_policy="wait",
        selected_clients=[],
        confirm="SWITCH_CODEX_MODE",
        replace_active=True,
    )

    service._run(first["job_id"])
    service._run(replacement["job_id"])

    assert service.status(first["job_id"])["state"] == "superseded"
    assert service.status(replacement["job_id"])["state"] == "completed"
    assert controller.calls == [("native", "SWITCH_NATIVE")]


def test_session_continuity_failure_requires_action_without_rolling_back_mode(tmp_path) -> None:
    continuity = _Continuity(verified=False)
    service, controller = _service(tmp_path, continuity=continuity)
    job = service.create(
        target="routed",
        client_policy="wait",
        selected_clients=[],
        confirm="SWITCH_CODEX_MODE",
        replace_active=True,
    )

    service._run(job["job_id"])
    result = service.status(job["job_id"])

    assert result["state"] == "action_required"
    assert result["result"]["session_continuity"]["verified"] is False
    assert result["result"]["mode"]["client_mode"] == "smart_router"
    assert controller.proxy is True
    assert "session_ids" not in repr(result)


def test_atomic_predecessor_releases_latest_job_at_safe_boundary(tmp_path) -> None:
    service, controller = _service(tmp_path, mode="smart_router", proxy=True)
    entered_atomic = threading.Event()
    release_atomic = threading.Event()
    original_native = controller.switch_to_native_codex

    def blocking_native(*, confirm):
        entered_atomic.set()
        assert release_atomic.wait(2)
        return original_native(confirm=confirm)

    controller.switch_to_native_codex = blocking_native
    first = service.create(
        target="native",
        client_policy="wait",
        selected_clients=[],
        confirm="SWITCH_CODEX_MODE",
        replace_active=True,
    )
    first_thread = threading.Thread(target=service._run, args=(first["job_id"],))
    first_thread.start()
    assert entered_atomic.wait(2)

    replacement = service.create(
        target="routed",
        client_policy="wait",
        selected_clients=[],
        confirm="SWITCH_CODEX_MODE",
        replace_active=True,
    )
    assert replacement["waiting_for_predecessor"] is True
    replacement_thread = threading.Thread(
        target=service._run, args=(replacement["job_id"],)
    )
    replacement_thread.start()
    release_atomic.set()
    first_thread.join(2)
    replacement_thread.join(2)

    assert not first_thread.is_alive()
    assert not replacement_thread.is_alive()
    assert service.status(first["job_id"])["state"] == "superseded"
    assert service.status(replacement["job_id"])["state"] == "completed"
    assert controller.mode == "smart_router"
    assert controller.calls == [
        ("native", "SWITCH_NATIVE"),
        ("smart", "ENABLE_ROUTER", True),
    ]


def test_wait_policy_converges_in_background_without_status_poll(tmp_path) -> None:
    observed = [{
        "pid": 43,
        "create_time": 101.0,
        "kind": "codex_cli",
        "command_fingerprint": "b" * 64,
        "has_config_override": False,
        "eligible_for_termination": True,
        "restart_behavior": "user_relaunch_required",
    }]
    clients = _Clients(observed, converged=False)
    service, _ = _service(tmp_path, clients=clients)
    job = service.create(
        target="routed",
        client_policy="wait",
        selected_clients=[],
        confirm="SWITCH_CODEX_MODE",
    )
    service._run(job["job_id"])
    clients.converged = True

    deadline = time.monotonic() + 2.5
    persisted = service.coordinator.job(job["job_id"])
    while persisted["state"] != "completed" and time.monotonic() < deadline:
        time.sleep(0.05)
        persisted = service.coordinator.job(job["job_id"])

    service.close()
    assert persisted["state"] == "completed"


def test_defer_and_terminate_selected_report_client_outcome(tmp_path) -> None:
    observed = [{
        "pid": 42,
        "create_time": 100.0,
        "kind": "codex_cli",
        "command_fingerprint": "a" * 64,
        "has_config_override": False,
        "eligible_for_termination": True,
        "restart_behavior": "user_relaunch_required",
    }]
    deferred_clients = _Clients(observed, converged=False)
    deferred, _ = _service(tmp_path / "defer", clients=deferred_clients)
    deferred_job = deferred.create(
        target="routed",
        client_policy="defer",
        selected_clients=[],
        confirm="SWITCH_CODEX_MODE",
    )
    deferred._run(deferred_job["job_id"])
    assert deferred.status(deferred_job["job_id"])["state"] == "action_required"

    terminated_clients = _Clients(observed, converged=True)
    terminated, _ = _service(tmp_path / "terminate", clients=terminated_clients)
    terminated_job = terminated.create(
        target="routed",
        client_policy="terminate_selected",
        selected_clients=[observed[0]],
        confirm="SWITCH_CODEX_MODE",
    )
    terminated._run(terminated_job["job_id"])
    assert terminated.status(terminated_job["job_id"])["state"] == "completed"
    selected = terminated_clients.terminated[0][0][0]
    assert terminated_clients.terminated[0][1] is False
    assert selected == {
        "pid": 42,
        "create_time": 100.0,
        "kind": "codex_cli",
        "command_fingerprint": "a" * 64,
    }


def test_recovery_skips_config_write_when_target_already_applied(tmp_path) -> None:
    service, controller = _service(tmp_path, mode="smart_router", proxy=True)
    job = service.coordinator.create_job(
        source="native",
        target="routed",
        client_policy="wait",
        snapshot={"observed_clients": []},
    )
    service.coordinator.update_stage(
        job["job_id"],
        stage="commit_config",
        progress=50,
        cancellable=False,
    )

    service._run(job["job_id"])

    assert service.status(job["job_id"])["state"] == "completed"
    assert controller.calls == []


def test_native_target_repairs_a_stopped_standby_proxy(tmp_path) -> None:
    service, controller = _service(tmp_path, mode="native_openai", proxy=False)
    job = service.create(
        target="native",
        client_policy="wait",
        selected_clients=[],
        confirm="SWITCH_CODEX_MODE",
    )

    service._run(job["job_id"])
    result = service.status(job["job_id"])

    assert result["state"] == "completed"
    assert controller.calls == [("native", "SWITCH_NATIVE")]
    assert result["result"]["mode"]["client_mode"] == "native_openai"
    assert result["result"]["proxy"]["running"] is True


def test_native_target_repairs_a_missing_managed_provider(tmp_path) -> None:
    service, controller = _service(
        tmp_path,
        mode="native_openai",
        proxy=True,
        provider_managed=False,
    )
    job = service.create(
        target="native",
        client_policy="wait",
        selected_clients=[],
        confirm="SWITCH_CODEX_MODE",
    )

    service._run(job["job_id"])
    result = service.status(job["job_id"])

    assert result["state"] == "completed"
    assert controller.calls == [("native", "SWITCH_NATIVE")]
    assert result["result"]["mode"]["provider_managed_by_routerllm"] is True


def test_routed_target_repairs_a_stopped_proxy(tmp_path) -> None:
    service, controller = _service(tmp_path, mode="smart_router", proxy=False)
    job = service.create(
        target="routed",
        client_policy="wait",
        selected_clients=[],
        confirm="SWITCH_CODEX_MODE",
    )

    service._run(job["job_id"])

    assert service.status(job["job_id"])["state"] == "completed"
    assert controller.calls == [("smart", "ENABLE_ROUTER", True)]


def test_routed_failure_restores_original_stopped_proxy_state(tmp_path) -> None:
    service, controller = _service(tmp_path, mode="native_openai", proxy=False)

    def failed_smart(*, confirm, allow_unverified=False):
        controller.calls.append(("smart", confirm, allow_unverified))
        controller.proxy = True
        return {
            "ok": False,
            "stage": "config_verification",
            "rollback": {"performed": True},
        }

    controller.enable_smart_routing = failed_smart
    job = service.create(
        target="routed",
        client_policy="wait",
        selected_clients=[],
        confirm="SWITCH_CODEX_MODE",
        request_id="request-rollback-1",
    )

    service._run(job["job_id"])
    result = service.status(job["job_id"])

    assert result["state"] == "failed"
    assert result["snapshot"]["request_id"] == "request-rollback-1"
    assert result["rollback"]["performed"] is True
    assert result["rollback"]["proxy"]["ok"] is True
    assert controller.calls[-1] == ("stop", "APPLY")
