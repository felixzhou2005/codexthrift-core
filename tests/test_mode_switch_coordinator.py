from __future__ import annotations

import json
import os

import pytest

from codexthrift.application.mode_switch_coordinator import ModeSwitchCoordinator
from codexthrift.errors import ControlError
from codexthrift.paths import WorkspacePaths


def _coordinator(tmp_path) -> ModeSwitchCoordinator:
    paths = WorkspacePaths(tmp_path / "router-home")
    paths.ensure_mutable()
    return ModeSwitchCoordinator(paths)


def test_create_job_persists_private_state_and_recovers(tmp_path) -> None:
    coordinator = _coordinator(tmp_path)

    job = coordinator.create_job(
        source="native",
        target="routed",
        client_policy="wait",
        snapshot={"provider": "openai"},
    )
    recovered = ModeSwitchCoordinator(coordinator.paths).recover()

    assert recovered == job
    assert coordinator.paths.mode_switch_current.exists()
    assert coordinator.paths.mode_switch_current.stat().st_mode & 0o777 == 0o600
    job_path = coordinator.paths.mode_switch_dir / f"job-{job['job_id']}.json"
    assert job_path.stat().st_mode & 0o777 == 0o600


def test_create_job_reuses_same_target_and_supersedes_different_target(tmp_path) -> None:
    coordinator = _coordinator(tmp_path)
    first = coordinator.create_job(
        source="native",
        target="routed",
        client_policy="terminate_selected",
        selected_clients=[{"pid": 22, "create_time": 10.5}],
    )

    reused = coordinator.create_job(
        source="native",
        target="routed",
        client_policy="terminate_selected",
        selected_clients=[{"pid": 22, "create_time": 10.5}],
    )

    assert reused["job_id"] == first["job_id"]
    assert reused["reused"] is True
    same_target = coordinator.create_job(
        source="native",
        target="routed",
        client_policy="defer",
    )
    assert same_target["job_id"] != first["job_id"]
    assert same_target.get("reused") is not True
    assert same_target["supersedes"] == first["job_id"]
    assert coordinator.job(first["job_id"])["state"] == "superseded"

    replacement = coordinator.create_job(
        source="routed",
        target="native",
        client_policy="defer",
    )
    superseded = coordinator.job(same_target["job_id"])

    assert replacement["job_id"] != same_target["job_id"]
    assert replacement["intent_sequence"] == same_target["intent_sequence"] + 1
    assert replacement["supersedes"] == same_target["job_id"]
    assert replacement["waiting_for_predecessor"] is False
    assert superseded["state"] == "superseded"
    assert superseded["stage"] == "superseded"
    assert superseded["superseded_by"] == replacement["job_id"]
    assert superseded["supersede_reason"] == "newer_user_intent"


def test_non_cancellable_predecessor_defers_replacement_until_safe_boundary(tmp_path) -> None:
    coordinator = _coordinator(tmp_path)
    first = coordinator.create_job(
        source="native", target="routed", client_policy="wait"
    )
    coordinator.update_stage(
        first["job_id"],
        stage="commit_config",
        progress=50,
        cancellable=False,
    )

    replacement = coordinator.create_job(
        source="routed", target="native", client_policy="wait"
    )
    predecessor = coordinator.job(first["job_id"])

    assert predecessor["state"] == "running"
    assert predecessor["superseded_by"] == replacement["job_id"]
    assert predecessor["supersede_reason"] == "newer_user_intent"
    assert replacement["waiting_for_predecessor"] is True
    assert replacement["predecessor_job_id"] == first["job_id"]
    assert coordinator.is_latest(replacement["job_id"]) is True
    assert coordinator.is_latest(first["job_id"]) is False

    stopped = coordinator.acknowledge_supersede(first["job_id"])
    released = coordinator.release_predecessor(replacement["job_id"])

    assert stopped["state"] == "superseded"
    assert released["waiting_for_predecessor"] is False


def test_three_rapid_switches_leave_only_latest_intent_active(tmp_path) -> None:
    coordinator = _coordinator(tmp_path)
    first = coordinator.create_job(
        source="native", target="routed", client_policy="wait"
    )
    second = coordinator.create_job(
        source="routed", target="native", client_policy="wait"
    )
    third = coordinator.create_job(
        source="native", target="routed", client_policy="wait"
    )

    assert coordinator.job(first["job_id"])["state"] == "superseded"
    assert coordinator.job(second["job_id"])["state"] == "superseded"
    assert coordinator.active_job()["job_id"] == third["job_id"]
    assert third["intent_sequence"] > second["intent_sequence"] > first["intent_sequence"]
    assert coordinator.recover()["job_id"] == third["job_id"]


def test_stage_progress_is_monotonic_and_commit_boundary_blocks_cancel(tmp_path) -> None:
    coordinator = _coordinator(tmp_path)
    job = coordinator.create_job(source="native", target="routed", client_policy="wait")

    coordinator.update_stage(
        job["job_id"],
        stage="prepare_target",
        progress=20,
        cancellable=True,
    )
    committed = coordinator.update_stage(
        job["job_id"],
        stage="commit_config",
        progress=50,
        cancellable=False,
    )

    assert committed["state"] == "running"
    with pytest.raises(ControlError, match="不可取消"):
        coordinator.cancel(job["job_id"])
    with pytest.raises(ControlError, match="不能倒退"):
        coordinator.update_stage(
            job["job_id"],
            stage="verify_proxy",
            progress=40,
            cancellable=False,
        )


def test_cancel_allows_new_job_and_terminal_cancel_is_idempotent(tmp_path) -> None:
    coordinator = _coordinator(tmp_path)
    first = coordinator.create_job(source="native", target="routed", client_policy="wait")

    canceled = coordinator.cancel(first["job_id"])
    repeated = coordinator.cancel(first["job_id"])
    second = coordinator.create_job(source="native", target="routed", client_policy="defer")

    assert canceled["state"] == "canceled"
    assert repeated == canceled
    assert second["job_id"] != first["job_id"]


def test_complete_and_action_required_are_terminal(tmp_path) -> None:
    coordinator = _coordinator(tmp_path)
    completed_job = coordinator.create_job(
        source="native", target="routed", client_policy="wait"
    )
    completed = coordinator.complete(completed_job["job_id"], result={"mode": "routed"})
    action_job = coordinator.create_job(
        source="routed", target="native", client_policy="defer"
    )
    action_required = coordinator.complete(
        action_job["job_id"],
        result={"mode": "native", "stale_clients": 2},
        action_required=True,
    )

    assert completed["state"] == "completed"
    assert action_required["state"] == "action_required"
    assert coordinator.active_job() is None


def test_superseded_is_terminal_and_cannot_complete_later(tmp_path) -> None:
    coordinator = _coordinator(tmp_path)
    first = coordinator.create_job(
        source="native", target="routed", client_policy="wait"
    )
    coordinator.create_job(source="routed", target="native", client_policy="wait")

    with pytest.raises(ControlError, match="已经结束"):
        coordinator.complete(first["job_id"], result={"mode": "routed"})


def test_recovers_legacy_v1_job_with_latest_intent_defaults(tmp_path) -> None:
    coordinator = _coordinator(tmp_path)
    job = coordinator.create_job(
        source="native", target="routed", client_policy="wait"
    )
    job_path = coordinator.paths.mode_switch_dir / f"job-{job['job_id']}.json"
    legacy = json.loads(job_path.read_text(encoding="utf-8"))
    legacy["version"] = 1
    for key in (
        "intent_sequence",
        "supersedes",
        "superseded_by",
        "supersede_reason",
        "waiting_for_predecessor",
        "predecessor_job_id",
    ):
        legacy.pop(key, None)
    job_path.write_text(json.dumps(legacy), encoding="utf-8")

    recovered = ModeSwitchCoordinator(coordinator.paths).recover()

    assert recovered is not None
    assert recovered["job_id"] == job["job_id"]
    assert recovered["intent_sequence"] == 1
    assert recovered["supersedes"] is None
    assert recovered["waiting_for_predecessor"] is False


def test_failure_redacts_sensitive_text_and_records_rollback(tmp_path) -> None:
    coordinator = _coordinator(tmp_path)
    job = coordinator.create_job(source="native", target="routed", client_policy="wait")

    failed = coordinator.fail(
        job["job_id"],
        stage="verify_target_provider",
        error="Authorization: Bearer secret-token-value-123456 failed for owner@example.com",
        rollback={"performed": True, "access_token": "sensitive-value"},
    )

    assert failed["state"] == "failed"
    assert "secret-token" not in failed["error"]["message"]
    assert "owner@example.com" not in failed["error"]["message"]
    assert failed["rollback"]["access_token"] == "***REDACTED***"


def test_invalid_job_id_client_selection_and_target_are_rejected(tmp_path) -> None:
    coordinator = _coordinator(tmp_path)

    with pytest.raises(ControlError, match="目标"):
        coordinator.create_job(source="native", target="other", client_policy="wait")
    with pytest.raises(ControlError, match="pid"):
        coordinator.create_job(
            source="native",
            target="routed",
            client_policy="terminate_selected",
            selected_clients=[{"pid": os.getpid()}],
        )
    with pytest.raises(ControlError, match="job_id"):
        coordinator.job("../escape")
