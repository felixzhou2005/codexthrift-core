from __future__ import annotations

import json
import sqlite3
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from codexthrift.application import storage_maintenance_service as storage_maintenance_module
from codexthrift.content_capture import encrypt_archive_payload, encrypt_payload
from codexthrift.controller import RouterController
from codexthrift.dashboard_api import (
    DashboardService,
    ResetCreditAction,
    create_dashboard_app,
)
from codexthrift.dashboard_store import DashboardStore
from codexthrift.paths import WorkspacePaths
from codexthrift.quota_adapter import QuotaAdapter
from codexthrift.util import ControlError


def _app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    controller = RouterController(WorkspacePaths(tmp_path / "router-home"), port=45454)
    return TestClient(create_dashboard_app(controller=controller, access_token="test-token"))


def test_dashboard_api_requires_bearer_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _app(tmp_path, monkeypatch)

    assert client.get("/healthz").status_code == 401
    response = client.get("/healthz", headers={"Authorization": "Bearer test-token"})
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["api_schema_version"] == 24


def test_mode_switch_job_api_uses_async_contract_and_request_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _app(tmp_path, monkeypatch)
    headers = {
        "Authorization": "Bearer test-token",
        "X-Request-Id": "mode-switch-request-1",
    }
    calls: list[tuple[str, object]] = []
    fake = type("FakeModeSwitch", (), {})()
    fake.preview = lambda *, target: {
        "source": "native",
        "target": target,
        "clients": {"clients": [], "detection_status": "available"},
    }
    fake.current = lambda: {"job": {"job_id": "a" * 32, "state": "running"}}
    fake.create = lambda **params: calls.append(("create", params)) or {
        "job_id": "a" * 32,
        "state": "queued",
        "waiting_for_safe_boundary": False,
        "superseded_job": {
            "job_id": "b" * 32,
            "state": "superseded",
            "target": "native",
        },
    }
    fake.status = lambda job_id: {"job_id": job_id, "state": "running"}
    fake.cancel = lambda job_id, *, confirm: calls.append(
        ("cancel", {"job_id": job_id, "confirm": confirm})
    ) or {"job_id": job_id, "state": "canceled"}
    fake.retry_clients = lambda job_id, *, force, confirm: calls.append(
        ("retry", {"job_id": job_id, "force": force, "confirm": confirm})
    ) or {"job_id": job_id, "state": "running"}
    client.app.state.dashboard_service.mode_switch = fake

    preview = client.get(
        "/v1/codex-mode/switch/preview?target=routed", headers=headers
    )
    current = client.get("/v1/codex-mode/switch/current", headers=headers)
    created = client.post(
        "/v1/codex-mode/switch/jobs",
        headers=headers,
        json={
            "target": "routed",
            "client_policy": "wait",
            "selected_clients": [],
            "confirm": "SWITCH_CODEX_MODE",
        },
    )
    status = client.get(
        f"/v1/codex-mode/switch/jobs/{'a' * 32}", headers=headers
    )
    canceled = client.post(
        f"/v1/codex-mode/switch/jobs/{'a' * 32}/cancel",
        headers=headers,
        json={"confirm": "CANCEL_CODEX_MODE_SWITCH"},
    )
    retried = client.post(
        f"/v1/codex-mode/switch/jobs/{'a' * 32}/retry-clients",
        headers=headers,
        json={"force": False, "confirm": "RETRY_CODEX_CLIENTS"},
    )

    assert preview.status_code == 200
    assert current.json()["job"]["state"] == "running"
    assert created.status_code == 200
    assert created.headers["X-Request-Id"] == "mode-switch-request-1"
    assert status.json()["state"] == "running"
    assert canceled.json()["state"] == "canceled"
    assert retried.json()["state"] == "running"
    assert [item[0] for item in calls] == ["create", "cancel", "retry"]
    assert calls[0][1]["replace_active"] is True
    events = client.get("/v1/events?limit=10", headers=headers).json()["items"]
    supersede_events = [
        item for item in events if item["event_type"] == "codex_mode_switch_superseded"
    ]
    assert len(supersede_events) == 1
    assert supersede_events[0]["details"]["job_id"] == "b" * 32
    assert "session" not in json.dumps(supersede_events).lower()


def test_mode_switch_api_can_explicitly_disable_active_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _app(tmp_path, monkeypatch)
    headers = {"Authorization": "Bearer test-token"}
    captured = {}
    fake = type("FakeModeSwitch", (), {})()
    fake.create = lambda **params: captured.update(params) or {
        "job_id": "a" * 32,
        "state": "queued",
    }
    client.app.state.dashboard_service.mode_switch = fake

    response = client.post(
        "/v1/codex-mode/switch/jobs",
        headers=headers,
        json={
            "target": "routed",
            "client_policy": "wait",
            "selected_clients": [],
            "replace_active": False,
            "confirm": "SWITCH_CODEX_MODE",
        },
    )

    assert response.status_code == 200
    assert captured["replace_active"] is False


def test_storage_overview_is_read_only_and_reports_bounded_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _app(tmp_path, monkeypatch)
    headers = {"Authorization": "Bearer test-token"}

    response = client.get("/v1/storage/overview", headers=headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["database"]["call_count"] == 0
    assert payload["database"]["bytes"] > 0
    assert payload["payload_archive"]["available"] is False
    assert payload["maintenance"]["state"] == "idle"
    assert payload["retention_buckets"]["pending"] in {True, False}


def test_storage_retention_preview_is_read_only_and_persisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit_path = tmp_path / "router-home" / "logs" / "route-audit.jsonl"
    audit_path.parent.mkdir(parents=True)
    audit_path.write_text(
        json.dumps({
            "timestamp": "2026-01-01T10:15:00+00:00",
            "status": "success",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }) + "\n",
        encoding="utf-8",
    )
    client = _app(tmp_path, monkeypatch)
    headers = {"Authorization": "Bearer test-token"}
    client.get("/v1/calls?limit=1", headers=headers)
    store_path = tmp_path / "router-home" / "data" / "routerllm.sqlite3"
    store = DashboardStore(store_path, payload_path=tmp_path / "router-home" / "data" / "call-payloads.jsonl.enc")
    while not store.refresh_storage_buckets(max_records=100)["complete"]:
        pass

    response = client.post(
        "/v1/storage/retention/preview",
        headers=headers,
        json={"limit_gib": 5, "cleanup_ratio": 0.8, "minimum_retention_hours": 24, "delete_mode": "payload_only"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["state"] == "ready"
    assert payload["plan"]["needs_cleanup"] is False
    assert payload["plan"]["estimated_immediate_release_bytes"] == 0
    assert payload["plan"]["estimated_sqlite_reclaimable_bytes"] == 0
    assert payload["plan"]["estimated_after_retention_bytes"] == payload["current"]["total_bytes"]
    assert payload["plan"]["estimated_after_compaction_bytes"] == payload["current"]["total_bytes"]
    assert (tmp_path / "router-home" / "data" / "storage-maintenance" / f"preview-{payload['preview_id']}.json").exists()


def test_payload_retention_execute_rewrites_archive_and_preserves_call_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router_home = tmp_path / "router-home"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    controller = RouterController(WorkspacePaths(router_home), port=45454)
    store = DashboardStore(controller.paths.dashboard_db, payload_path=controller.paths.encrypted_payload_log)
    audit_path = controller.paths.audit_log
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {"timestamp": "2026-01-01T10:00:00+00:00", "status": "success", "payload_ref": "old-ref"},
        {"timestamp": "2026-08-12T10:00:00+00:00", "status": "success", "payload_ref": "new-ref"},
    ]
    audit_path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
    store.import_audit_jsonl(audit_path)
    controller.paths.encrypted_payload_log.parent.mkdir(parents=True, exist_ok=True)
    controller.paths.encrypted_payload_log.write_text(
        json.dumps({"payload_ref": "old-ref", "nonce": "a", "ciphertext": "b"}) + "\n" +
        json.dumps({"payload_ref": "new-ref", "nonce": "c", "ciphertext": "d"}) + "\n",
        encoding="utf-8",
    )
    while not store.index_payload_archive(max_records=100)["complete"]:
        pass
    while not store.refresh_storage_buckets(max_records=100)["complete"]:
        pass
    with TestClient(create_dashboard_app(controller=controller, store=store, access_token="test-token")) as client:
        service = client.app.state.storage_service
        database = store.storage_overview()
        archive = store.payload_archive_status()
        buckets = store.storage_bucket_status()
        preview_id = "a" * 32
        preview = {
            "preview_id": preview_id,
            "state": "ready",
            "expires_at": "2099-01-01T00:00:00+00:00",
            "settings": {"delete_mode": "payload_only"},
            "plan": {"needs_cleanup": True, "cutoff_at": "2026-02-01T00:00:00+00:00"},
            "source": service._source_fingerprint(database, archive, buckets),
        }
        service.paths.storage_maintenance_dir.mkdir(parents=True, exist_ok=True)
        (service.paths.storage_maintenance_dir / f"preview-{preview_id}.json").write_text(json.dumps(preview), encoding="utf-8")
        response = client.post(
            "/v1/storage/retention/execute",
            headers={"Authorization": "Bearer test-token"},
            json={"preview_id": preview_id, "confirm": "DELETE_OLD_CALL_DATA"},
        )
        assert response.status_code == 200
        job_id = response.json()["job_id"]
        for _ in range(200):
            job = client.get(f"/v1/storage/retention/jobs/{job_id}", headers={"Authorization": "Bearer test-token"}).json()
            if job["state"] in {"completed", "failed", "canceled"}:
                break
            threading.Event().wait(0.01)

    assert job["state"] == "completed"
    assert job["result"]["quick_check"] == "ok"
    assert job["result"]["foreign_key_errors"] == 0
    assert job["result"]["affected_call_count"] == 1
    assert job["candidate_call_count"] == 1
    assert job["unique_payload_count"] == 1
    assert job["duplicate_reference_count"] == 0
    assert job["protected_shared_payload_count"] == 0
    assert job["result"]["released_bytes"] > 0
    assert job["result"]["source_archive_bytes"] > job["result"]["final_archive_bytes"]
    assert "old-ref" not in controller.paths.encrypted_payload_log.read_text(encoding="utf-8")
    assert "new-ref" in controller.paths.encrypted_payload_log.read_text(encoding="utf-8")
    assert len(store.recent_calls()) == 2
    old_id = next(item["id"] for item in store.recent_calls() if item["payload_ref"] == "old-ref")
    assert store.call_payload_section(old_id, "visible-output")["state"] == "retention_deleted"


def test_payload_retention_restores_archive_when_database_update_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router_home = tmp_path / "router-home"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    controller = RouterController(WorkspacePaths(router_home), port=45454)
    store = DashboardStore(
        controller.paths.dashboard_db,
        payload_path=controller.paths.encrypted_payload_log,
    )
    audit_path = controller.paths.audit_log
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps({
            "timestamp": "2026-01-01T10:00:00+00:00",
            "status": "success",
            "payload_ref": "rollback-ref",
        }) + "\n",
        encoding="utf-8",
    )
    store.import_audit_jsonl(audit_path)
    controller.paths.encrypted_payload_log.write_text(
        json.dumps({"payload_ref": "rollback-ref", "nonce": "a", "ciphertext": "b"}) + "\n",
        encoding="utf-8",
    )
    while not store.index_payload_archive(max_records=100)["complete"]:
        pass
    while not store.refresh_storage_buckets(max_records=100)["complete"]:
        pass
    original_archive = controller.paths.encrypted_payload_log.read_bytes()

    with TestClient(
        create_dashboard_app(controller=controller, store=store, access_token="test-token")
    ) as client:
        service = client.app.state.storage_service
        preview_id = "9" * 32
        service._write_preview({
            "preview_id": preview_id,
            "state": "ready",
            "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            "settings": {"delete_mode": "payload_only"},
            "plan": {"needs_cleanup": True, "cutoff_at": "2026-02-01T00:00:00+00:00"},
            "source": service._source_fingerprint(
                store.storage_overview(),
                store.payload_archive_status(),
                store.storage_bucket_status(),
            ),
        })

        def fail_database_update(**_: object) -> dict[str, object]:
            raise RuntimeError("simulated database update failure")

        monkeypatch.setattr(store, "apply_payload_retention", fail_database_update)
        response = client.post(
            "/v1/storage/retention/execute",
            headers={"Authorization": "Bearer test-token"},
            json={"preview_id": preview_id, "confirm": "DELETE_OLD_CALL_DATA"},
        )
        assert response.status_code == 200
        job_id = response.json()["job_id"]
        for _ in range(200):
            job = client.get(
                f"/v1/storage/retention/jobs/{job_id}",
                headers={"Authorization": "Bearer test-token"},
            ).json()
            if job["state"] in {"completed", "failed", "canceled"}:
                break
            threading.Event().wait(0.01)

    assert job["state"] == "failed"
    assert job["error_code"] == "RuntimeError"
    assert job["failed_stage"] == "update_database"
    assert controller.paths.encrypted_payload_log.read_bytes() == original_archive
    assert store.retention_payload_refs("2026-02-01T00:00:00+00:00")


def test_payload_retention_execute_rejects_mode_specific_wrong_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _app(tmp_path, monkeypatch)
    service = client.app.state.storage_service
    database = service.store.storage_overview()
    archive = service.store.payload_archive_status()
    buckets = service.store.storage_bucket_status()
    preview_id = "b" * 32
    preview = {
        "preview_id": preview_id,
        "state": "ready",
        "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        "settings": {"delete_mode": "full_call"},
        "plan": {"needs_cleanup": True, "cutoff_at": "2026-02-01T00:00:00+00:00"},
        "source": service._source_fingerprint(database, archive, buckets),
    }
    service._write_preview(preview)
    headers = {"Authorization": "Bearer test-token"}

    invalid_confirm = client.post(
        "/v1/storage/retention/execute",
        headers=headers,
        json={"preview_id": preview_id, "confirm": "DELETE"},
    )
    wrong_full_call_confirmation = client.post(
        "/v1/storage/retention/execute",
        headers=headers,
        json={"preview_id": preview_id, "confirm": "DELETE_OLD_CALL_DATA"},
    )

    assert invalid_confirm.status_code == 400
    assert wrong_full_call_confirmation.status_code == 400


def test_full_call_retention_removes_old_history_and_blocks_audit_reimport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router_home = tmp_path / "router-home"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    controller = RouterController(WorkspacePaths(router_home), port=45454)
    store = DashboardStore(controller.paths.dashboard_db, payload_path=controller.paths.encrypted_payload_log)
    audit_path = controller.paths.audit_log
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {"timestamp": "2026-01-01T10:00:00+00:00", "status": "success", "payload_ref": "old-full-ref", "usage": {"input_tokens": 10, "output_tokens": 2}},
        {"timestamp": "2026-08-12T10:00:00+00:00", "status": "success", "payload_ref": "new-full-ref", "usage": {"input_tokens": 20, "output_tokens": 4}},
    ]
    audit_path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
    store.import_audit_jsonl(audit_path)
    controller.paths.encrypted_payload_log.write_text(
        json.dumps({"payload_ref": "old-full-ref", "nonce": "a", "ciphertext": "b"}) + "\n" +
        json.dumps({"payload_ref": "new-full-ref", "nonce": "c", "ciphertext": "d"}) + "\n",
        encoding="utf-8",
    )
    while not store.index_payload_archive(max_records=100)["complete"]:
        pass
    while not store.refresh_storage_buckets(max_records=100)["complete"]:
        pass

    with TestClient(create_dashboard_app(controller=controller, store=store, access_token="test-token")) as client:
        service = client.app.state.storage_service
        preview_id = "f" * 32
        preview = {
            "preview_id": preview_id,
            "state": "ready",
            "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
            "settings": {"delete_mode": "full_call"},
            "plan": {"needs_cleanup": True, "cutoff_at": "2026-02-01T00:00:00+00:00"},
            "source": service._source_fingerprint(
                store.storage_overview(), store.payload_archive_status(), store.storage_bucket_status()
            ),
        }
        service._write_preview(preview)
        response = client.post(
            "/v1/storage/retention/execute",
            headers={"Authorization": "Bearer test-token"},
            json={"preview_id": preview_id, "confirm": "DELETE_COMPLETE_CALL_HISTORY"},
        )
        assert response.status_code == 200
        job_id = response.json()["job_id"]
        for _ in range(300):
            job = client.get(
                f"/v1/storage/retention/jobs/{job_id}",
                headers={"Authorization": "Bearer test-token"},
            ).json()
            if job["state"] in {"completed", "failed", "canceled"}:
                break
            threading.Event().wait(0.01)

    assert job["state"] == "completed"
    assert job["result"]["call_count"] == 1
    calls = store.recent_calls()
    assert len(calls) == 1
    assert calls[0]["payload_ref"] == "new-full-ref"
    archive_text = controller.paths.encrypted_payload_log.read_text(encoding="utf-8")
    assert "old-full-ref" not in archive_text
    assert "new-full-ref" in archive_text
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("DELETE FROM import_cursors WHERE source_path=?", (str(audit_path),))
    reimport = store.import_audit_jsonl(audit_path)
    assert reimport["imported"] == 0
    assert len(store.recent_calls()) == 1


def test_payload_retention_execute_rejects_stale_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _app(tmp_path, monkeypatch)
    service = client.app.state.storage_service
    database = service.store.storage_overview()
    archive = service.store.payload_archive_status()
    buckets = service.store.storage_bucket_status()
    preview_id = "c" * 32
    preview = {
        "preview_id": preview_id,
        "state": "ready",
        "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        "settings": {"delete_mode": "payload_only"},
        "plan": {"needs_cleanup": True, "cutoff_at": "2026-02-01T00:00:00+00:00"},
        "source": service._source_fingerprint(database, archive, buckets),
    }
    service._write_preview(preview)
    service.paths.encrypted_payload_log.write_text("{}\n", encoding="utf-8")

    response = client.post(
        "/v1/storage/retention/execute",
        headers={"Authorization": "Bearer test-token"},
        json={"preview_id": preview_id, "confirm": "DELETE_OLD_CALL_DATA"},
    )

    assert response.status_code == 400


def test_payload_retention_cancel_rejects_irreversible_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _app(tmp_path, monkeypatch)
    service = client.app.state.storage_service
    job = {
        "job_id": "d" * 32,
        "state": "running",
        "stage": "replace_archive",
        "updated_at": datetime.now(UTC).isoformat(),
    }
    service._write_job(job)

    response = client.post(
        f"/v1/storage/retention/jobs/{job['job_id']}/cancel",
        headers={"Authorization": "Bearer test-token"},
        json={"confirm": "CANCEL_STORAGE_MAINTENANCE"},
    )

    assert response.status_code == 400


def test_payload_retention_cancel_marks_cancellable_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _app(tmp_path, monkeypatch)
    service = client.app.state.storage_service
    job = {
        "job_id": "e" * 32,
        "state": "running",
        "stage": "rewrite_archive",
        "updated_at": datetime.now(UTC).isoformat(),
    }
    service._write_job(job)

    response = client.post(
        f"/v1/storage/retention/jobs/{job['job_id']}/cancel",
        headers={"Authorization": "Bearer test-token"},
        json={"confirm": "CANCEL_STORAGE_MAINTENANCE"},
    )

    assert response.status_code == 200
    assert response.json()["cancel_requested"] is True
    assert service._cancel_requested is True


def test_maintenance_events_are_durable_cursor_readable_and_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _app(tmp_path, monkeypatch)
    service = client.app.state.storage_service
    job_id = "a" * 32
    manifest = service._create_job({
        "job_id": job_id,
        "operation": "sqlite_compaction",
        "state": "queued",
        "stage": "queued",
        "created_at": datetime.now(UTC).isoformat(),
        "updated_at": datetime.now(UTC).isoformat(),
        "private_detail": "/Users/example/secret-token",
    })
    manifest = service._update_job(
        manifest,
        state="running",
        stage="verify_archive_coverage",
        processed_bytes=1234,
        payload={"input": "must-not-appear"},
    )
    service._update_job(
        manifest,
        state="failed",
        stage="failed",
        failed_stage="verify_archive_coverage",
        error_code="RuntimeError",
    )

    headers = {"Authorization": "Bearer test-token"}
    first_page = client.get(
        f"/v1/storage/retention/jobs/{job_id}/events?limit=2",
        headers=headers,
    )
    assert first_page.status_code == 200
    first = first_page.json()
    assert [item["seq"] for item in first["items"]] == [1, 2]
    assert first["has_more"] is True
    assert first["items"][1]["metrics"]["processed_bytes"] == 1234

    second = client.get(
        f"/v1/storage/retention/jobs/{job_id}/events?after_seq={first['next_after_seq']}",
        headers=headers,
    ).json()
    assert [item["seq"] for item in second["items"]] == [3]
    assert second["items"][0]["failed_stage"] == "verify_archive_coverage"
    assert second["items"][0]["error_code"] == "RuntimeError"
    serialized = json.dumps(first) + json.dumps(second)
    assert "secret-token" not in serialized
    assert "must-not-appear" not in serialized
    assert stat.S_IMODE(service._job_events_path(job_id).stat().st_mode) == 0o600
    assert stat.S_IMODE((service.paths.logs_dir / "storage-maintenance.log").stat().st_mode) == 0o600

    restarted = type(service)(service.paths, service.store, service._controller)
    resumed = restarted.maintenance_events(job_id, after_seq=2)
    assert [item["seq"] for item in resumed["items"]] == [3]


def test_maintenance_events_and_global_log_rotate_without_losing_recent_cursor_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage_maintenance_module, "MAINTENANCE_EVENT_MAX_BYTES", 350)
    monkeypatch.setattr(storage_maintenance_module, "MAINTENANCE_EVENT_BACKUPS", 10)
    monkeypatch.setattr(storage_maintenance_module, "MAINTENANCE_LOG_MAX_BYTES", 350)
    monkeypatch.setattr(storage_maintenance_module, "MAINTENANCE_LOG_BACKUPS", 10)
    client = _app(tmp_path, monkeypatch)
    service = client.app.state.storage_service
    job_id = "b" * 32
    manifest = service._create_job({
        "job_id": job_id,
        "operation": "sqlite_compaction",
        "state": "queued",
        "stage": "queued",
        "created_at": datetime.now(UTC).isoformat(),
        "updated_at": datetime.now(UTC).isoformat(),
    })
    for index in range(1, 8):
        manifest = service._update_job(
            manifest,
            state="running",
            stage="verify_archive_coverage",
            processed_bytes=index * 1024,
        )

    assert service._job_events_path(job_id).with_name(
        f"{service._job_events_path(job_id).name}.1"
    ).exists()
    global_log = service.paths.logs_dir / "storage-maintenance.log"
    assert global_log.with_name(f"{global_log.name}.1").exists()
    events = service.maintenance_events(job_id, after_seq=4, limit=10)
    assert [item["seq"] for item in events["items"]] == [5, 6, 7, 8]


def test_interrupted_maintenance_recovery_persists_safe_terminal_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _app(tmp_path, monkeypatch)
    service = client.app.state.storage_service
    job_id = "c" * 32
    manifest = service._create_job({
        "job_id": job_id,
        "operation": "sqlite_compaction",
        "state": "running",
        "stage": "vacuum_into",
        "created_at": datetime.now(UTC).isoformat(),
        "updated_at": datetime.now(UTC).isoformat(),
    })
    service.paths.storage_maintenance_dir.mkdir(parents=True, exist_ok=True)
    service._maintenance_lock_path.write_text(
        json.dumps({"job_id": job_id, "operation": "sqlite_compaction", "pid": 2_147_483_647}),
        encoding="utf-8",
    )

    restarted = storage_maintenance_module.StorageMaintenanceService(
        service.paths,
        service.store,
        service._controller,
    )
    recovered = restarted.maintenance_status(job_id)
    events = restarted.maintenance_events(job_id)

    assert recovered["state"] == "failed"
    assert recovered["error_code"] == "maintenance_interrupted"
    assert events["items"][-1]["state"] == "failed"
    assert events["items"][-1]["error_code"] == "maintenance_interrupted"
    assert not restarted._maintenance_lock_path.exists()
    assert manifest["event_seq"] == 1


def test_sqlite_compaction_reclaims_space_and_preserves_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router_home = tmp_path / "router-home"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    controller = RouterController(WorkspacePaths(router_home), port=45454)
    controller.codex_mode_status = lambda: {"client_mode": "native_openai"}
    store = DashboardStore(controller.paths.dashboard_db, payload_path=controller.paths.encrypted_payload_log)
    store.initialize()
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("CREATE TABLE compaction_probe(id INTEGER PRIMARY KEY, body BLOB NOT NULL)")
        connection.executemany(
            "INSERT INTO compaction_probe(body) VALUES (?)",
            [(b"x" * 4096,) for _ in range(600)],
        )
        connection.commit()
        connection.execute("DELETE FROM compaction_probe WHERE id <= 500")
        connection.commit()
    before_storage = store.storage_overview()
    before_bytes = int(before_storage["bytes"]) + int(before_storage["wal_bytes"])

    with TestClient(create_dashboard_app(controller=controller, store=store, access_token="test-token")) as client:
        headers = {"Authorization": "Bearer test-token"}
        preview_response = client.get("/v1/storage/compaction/preview", headers=headers)
        assert preview_response.status_code == 200
        preview = preview_response.json()
        execute_response = client.post(
            "/v1/storage/compaction/execute",
            headers=headers,
            json={"preview_id": preview["preview_id"], "confirm": "COMPACT_SQLITE_DATABASE"},
        )
        assert execute_response.status_code == 200
        job_id = execute_response.json()["job_id"]
        for _ in range(300):
            job = client.get(f"/v1/storage/retention/jobs/{job_id}", headers=headers).json()
            if job["state"] in {"completed", "failed", "canceled"}:
                break
            threading.Event().wait(0.01)

    assert job["state"] == "completed"
    assert job["result"]["quick_check"] == "ok"
    assert job["result"]["foreign_key_errors"] == 0
    after_storage = store.storage_overview()
    assert int(after_storage["bytes"]) + int(after_storage["wal_bytes"]) < before_bytes
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM compaction_probe").fetchone()[0] == 100
    assert not (controller.paths.storage_maintenance_dir / "maintenance.lock").exists()


def test_sqlite_compaction_keeps_dashboard_reads_available_during_long_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _app(tmp_path, monkeypatch)
    headers = {"Authorization": "Bearer test-token"}
    service = client.app.state.storage_service
    service._controller.codex_mode_status = lambda: {"client_mode": "native_openai"}
    reached_long_build = threading.Event()
    release_long_build = threading.Event()
    original_verify = service._verify_embedded_payload_archive_copies

    def hold_long_build(path: Path, manifest: dict[str, object]) -> dict[str, object]:
        reached_long_build.set()
        release_long_build.wait(timeout=2)
        return original_verify(path, manifest)

    monkeypatch.setattr(service, "_verify_embedded_payload_archive_copies", hold_long_build)
    preview = client.get("/v1/storage/compaction/preview", headers=headers).json()
    response = client.post(
        "/v1/storage/compaction/execute",
        headers=headers,
        json={"preview_id": preview["preview_id"], "confirmed": True},
    )
    assert response.status_code == 200
    assert reached_long_build.wait(timeout=2)

    read_completed = threading.Event()

    def dashboard_read() -> None:
        service.store.storage_overview()
        read_completed.set()

    reader = threading.Thread(target=dashboard_read)
    reader.start()
    assert read_completed.wait(timeout=0.5)

    job_id = response.json()["job_id"]
    read_paths = [
        "/v1/overview",
        "/v1/calls?limit=5",
        "/v1/costs/summary",
        "/v1/routing/stats?limit=5",
        "/v1/analytics/daily?days=7",
        f"/v1/storage/retention/jobs/{job_id}/events?after_seq=0",
    ]
    started_at = time.monotonic()
    with ThreadPoolExecutor(max_workers=len(read_paths)) as executor:
        futures = [executor.submit(client.get, path, headers=headers) for path in read_paths]
        results = [future.result(timeout=1) for future in futures]
    assert time.monotonic() - started_at < 1
    assert [result.status_code for result in results] == [200] * len(read_paths)

    release_long_build.set()
    reader.join(timeout=1)
    for _ in range(300):
        job = client.get(f"/v1/storage/retention/jobs/{job_id}", headers=headers).json()
        if job["state"] in {"completed", "failed", "canceled"}:
            break
        threading.Event().wait(0.01)
    assert job["state"] == "completed"


def test_sqlite_compaction_restores_source_inside_exclusive_maintenance_on_final_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router_home = tmp_path / "router-home"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    controller = RouterController(WorkspacePaths(router_home), port=45454)
    controller.codex_mode_status = lambda: {"client_mode": "native_openai"}
    store = DashboardStore(controller.paths.dashboard_db, payload_path=controller.paths.encrypted_payload_log)
    store.initialize()
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("CREATE TABLE recovery_probe(id INTEGER PRIMARY KEY, body BLOB NOT NULL)")
        connection.executemany(
            "INSERT INTO recovery_probe(body) VALUES (?)",
            [(b"x" * 4096,) for _ in range(100)],
        )
        connection.commit()
        connection.execute("DELETE FROM recovery_probe WHERE id <= 90")
        connection.commit()
    source_inode = store.database_path.stat().st_ino

    with TestClient(create_dashboard_app(controller=controller, store=store, access_token="test-token")) as client:
        service = client.app.state.storage_service
        original_health = service._database_health
        live_health_checks = 0

        def fail_final_live_health(path: Path) -> dict[str, object]:
            nonlocal live_health_checks
            result = original_health(path)
            if path == store.database_path:
                live_health_checks += 1
                if live_health_checks == 2:
                    return {"quick_check": "forced_failure", "foreign_key_errors": []}
            return result

        monkeypatch.setattr(service, "_database_health", fail_final_live_health)
        headers = {"Authorization": "Bearer test-token"}
        preview = client.get("/v1/storage/compaction/preview", headers=headers).json()
        response = client.post(
            "/v1/storage/compaction/execute",
            headers=headers,
            json={"preview_id": preview["preview_id"], "confirmed": True},
        )
        assert response.status_code == 200
        job_id = response.json()["job_id"]
        for _ in range(300):
            job = client.get(f"/v1/storage/retention/jobs/{job_id}", headers=headers).json()
            if job["state"] in {"completed", "failed", "canceled"}:
                break
            threading.Event().wait(0.01)

    assert job["state"] == "failed"
    assert job["error_code"] == "RuntimeError"
    assert store.database_path.stat().st_ino == source_inode
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM recovery_probe").fetchone()[0] == 10
    assert not list(controller.paths.storage_maintenance_dir.glob("database-*.bak"))
    assert not list(controller.paths.storage_maintenance_dir.glob("database-*.tmp"))


def test_sqlite_compaction_removes_verified_embedded_payload_copy_and_keeps_archive_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router_home = tmp_path / "router-home"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    controller = RouterController(WorkspacePaths(router_home), port=45454)
    controller.codex_mode_status = lambda: {"client_mode": "native_openai"}
    key = b"v" * 32
    payload_ref = "payload-verified-compaction"
    audit_path = tmp_path / "route-audit.jsonl"
    audit_path.write_text(
        json.dumps({
            "timestamp": "2026-08-10T10:00:00+00:00",
            "status": "success",
            "payload_ref": payload_ref,
        }) + "\n",
        encoding="utf-8",
    )
    payload = {"payload_ref": payload_ref, "request": {"input": "x" * (1024 * 1024)}}
    envelope = {"payload_ref": payload_ref, **encrypt_payload(key, json.dumps(payload).encode("utf-8"))}
    controller.paths.encrypted_payload_log.parent.mkdir(parents=True, exist_ok=True)
    controller.paths.encrypted_payload_log.write_text(json.dumps(envelope) + "\n", encoding="utf-8")
    store = DashboardStore(
        controller.paths.dashboard_db,
        payload_path=controller.paths.encrypted_payload_log,
        payload_key=key,
    )
    store.import_audit_jsonl(audit_path)
    store.import_encrypted_payloads()
    with sqlite3.connect(store.database_path) as connection:
        call_id = connection.execute("SELECT id FROM api_calls WHERE payload_ref=?", (payload_ref,)).fetchone()[0]
        connection.execute(
            """INSERT INTO call_payloads(payload_ref, api_call_id, nonce, ciphertext, captured_at)
               VALUES (?, ?, ?, ?, ?)""",
            (payload_ref, call_id, envelope["nonce"], envelope["ciphertext"], "2026-08-10T10:00:00+00:00"),
        )
    source_storage = store.storage_overview()
    source_bytes = int(source_storage["bytes"]) + int(source_storage["wal_bytes"])

    with TestClient(create_dashboard_app(controller=controller, store=store, access_token="test-token")) as client:
        headers = {"Authorization": "Bearer test-token"}
        preview = client.get("/v1/storage/compaction/preview", headers=headers).json()
        assert preview["state"] == "ready"
        assert preview["projection"]["archive_covered_payload_count"] == 1
        assert preview["projection"]["missing_archive_payload_count"] == 0
        response = client.post(
            "/v1/storage/compaction/execute",
            headers=headers,
            json={"preview_id": preview["preview_id"], "confirmed": True},
        )
        assert response.status_code == 200
        job_id = response.json()["job_id"]
        for _ in range(500):
            job = client.get(f"/v1/storage/retention/jobs/{job_id}", headers=headers).json()
            if job["state"] in {"completed", "failed", "canceled"}:
                break
            threading.Event().wait(0.01)

    assert job["state"] == "completed"
    assert job["result"]["embedded_payloads_removed"] == 1
    assert job["result"]["embedded_payloads_retained"] == 0
    assert job["result"]["archive_verified_payloads"] == 1
    assert job["result"]["archive_canary_available"] == 1
    assert job["result"]["released_bytes"] > 0
    compacted_storage = store.storage_overview()
    assert int(compacted_storage["bytes"]) + int(compacted_storage["wal_bytes"]) < source_bytes
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM call_payloads").fetchone()[0] == 0
    detail = store.call_payload_section(call_id, "input")
    assert detail is not None
    assert detail["state"] == "available"
    assert detail["source"] == "encrypted_log"
    assert detail["request"]["input"] == "x" * (1024 * 1024)


def test_sqlite_compaction_retains_mismatched_archive_copy_and_completes_safely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router_home = tmp_path / "router-home"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    controller = RouterController(WorkspacePaths(router_home), port=45454)
    controller.codex_mode_status = lambda: {"client_mode": "native_openai"}
    key = b"m" * 32
    payload_ref = "payload-mismatch"
    audit_path = tmp_path / "route-audit.jsonl"
    audit_path.write_text(
        json.dumps({"timestamp": "2026-08-10T10:00:00+00:00", "status": "success", "payload_ref": payload_ref}) + "\n",
        encoding="utf-8",
    )
    envelope = {
        "payload_ref": payload_ref,
        **encrypt_payload(key, json.dumps({"payload_ref": payload_ref}).encode("utf-8")),
    }
    controller.paths.encrypted_payload_log.parent.mkdir(parents=True, exist_ok=True)
    controller.paths.encrypted_payload_log.write_text(json.dumps(envelope) + "\n", encoding="utf-8")
    store = DashboardStore(
        controller.paths.dashboard_db,
        payload_path=controller.paths.encrypted_payload_log,
        payload_key=key,
    )
    store.import_audit_jsonl(audit_path)
    store.import_encrypted_payloads()
    with sqlite3.connect(store.database_path) as connection:
        call_id = connection.execute("SELECT id FROM api_calls WHERE payload_ref=?", (payload_ref,)).fetchone()[0]
        connection.execute(
            """INSERT INTO call_payloads(payload_ref, api_call_id, nonce, ciphertext, captured_at)
               VALUES (?, ?, ?, ?, ?)""",
            (payload_ref, call_id, envelope["nonce"], "different-ciphertext", "2026-08-10T10:00:00+00:00"),
        )
    source_inode = store.database_path.stat().st_ino

    with TestClient(create_dashboard_app(controller=controller, store=store, access_token="test-token")) as client:
        headers = {"Authorization": "Bearer test-token"}
        preview = client.get("/v1/storage/compaction/preview", headers=headers).json()
        assert preview["state"] == "ready"
        response = client.post(
            "/v1/storage/compaction/execute",
            headers=headers,
            json={"preview_id": preview["preview_id"], "confirmed": True},
        )
        assert response.status_code == 200
        job_id = response.json()["job_id"]
        for _ in range(300):
            job = client.get(f"/v1/storage/retention/jobs/{job_id}", headers=headers).json()
            if job["state"] in {"completed", "failed", "canceled"}:
                break
            threading.Event().wait(0.01)

    assert job["state"] == "completed"
    assert job["result"]["embedded_payloads_removed"] == 0
    assert job["result"]["embedded_payloads_retained"] == 1
    assert job["result"]["archive_canary_tested"] == 0
    assert store.database_path.stat().st_ino != source_inode
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT ciphertext FROM call_payloads WHERE payload_ref=?", (payload_ref,)).fetchone()[0] == "different-ciphertext"
    assert not list(controller.paths.storage_maintenance_dir.glob("database-*.bak"))
    assert not list(controller.paths.storage_maintenance_dir.glob("database-*.tmp"))


def test_sqlite_compaction_retains_missing_archive_copy_without_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router_home = tmp_path / "router-home"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    controller = RouterController(WorkspacePaths(router_home), port=45454)
    controller.codex_mode_status = lambda: {"client_mode": "native_openai"}
    payload_ref = "payload-without-archive"
    audit_path = tmp_path / "route-audit.jsonl"
    audit_path.write_text(
        json.dumps({"timestamp": "2026-08-10T10:00:00+00:00", "status": "success", "payload_ref": payload_ref}) + "\n",
        encoding="utf-8",
    )
    controller.paths.encrypted_payload_log.parent.mkdir(parents=True, exist_ok=True)
    controller.paths.encrypted_payload_log.write_text("", encoding="utf-8")
    store = DashboardStore(
        controller.paths.dashboard_db,
        payload_path=controller.paths.encrypted_payload_log,
        payload_key=b"a" * 32,
    )
    store.import_audit_jsonl(audit_path)
    with sqlite3.connect(store.database_path) as connection:
        call_id = connection.execute("SELECT id FROM api_calls WHERE payload_ref=?", (payload_ref,)).fetchone()[0]
        connection.execute(
            """INSERT INTO call_payloads(payload_ref, api_call_id, nonce, ciphertext, captured_at)
               VALUES (?, ?, 'nonce', 'ciphertext', ?)""",
            (payload_ref, call_id, "2026-08-10T10:00:00+00:00"),
        )

    with TestClient(create_dashboard_app(controller=controller, store=store, access_token="test-token")) as client:
        headers = {"Authorization": "Bearer test-token"}
        preview = client.get("/v1/storage/compaction/preview", headers=headers).json()
        response = client.post(
            "/v1/storage/compaction/execute",
            headers=headers,
            json={"preview_id": preview["preview_id"], "confirmed": True},
        )
        assert response.status_code == 200
        job_id = response.json()["job_id"]
        for _ in range(300):
            job = client.get(f"/v1/storage/retention/jobs/{job_id}", headers=headers).json()
            if job["state"] in {"completed", "failed", "canceled"}:
                break
            threading.Event().wait(0.01)

    assert preview["state"] == "ready_with_retained_items"
    assert preview["projection"]["missing_archive_payload_count"] == 1
    assert job["state"] == "completed"
    assert job["result"]["embedded_payloads_removed"] == 0
    assert job["result"]["embedded_payloads_retained"] == 1
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute(
            "SELECT ciphertext FROM call_payloads WHERE payload_ref=?",
            (payload_ref,),
        ).fetchone()[0] == "ciphertext"


def test_archive_compression_rewrites_only_archive_only_legacy_payloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router_home = tmp_path / "router-home"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    controller = RouterController(WorkspacePaths(router_home), port=45454)
    controller.codex_mode_status = lambda: {"client_mode": "native_openai"}
    key = b"z" * 32
    refs = ("archive-legacy", "archive-v2", "archive-embedded")
    audit_path = tmp_path / "route-audit.jsonl"
    audit_path.write_text(
        "".join(
            json.dumps({
                "timestamp": f"2026-08-10T10:0{index}:00+00:00",
                "status": "success",
                "payload_ref": payload_ref,
            }) + "\n"
            for index, payload_ref in enumerate(refs)
        ),
        encoding="utf-8",
    )
    payloads = {
        payload_ref: json.dumps({
            "payload_ref": payload_ref,
            "request": {"input": payload_ref + ("x" * (512 * 1024))},
        }).encode("utf-8")
        for payload_ref in refs
    }
    envelopes = {
        "archive-legacy": {"payload_ref": "archive-legacy", **encrypt_payload(key, payloads["archive-legacy"])},
        "archive-v2": {"payload_ref": "archive-v2", **encrypt_archive_payload(key, payloads["archive-v2"])},
        "archive-embedded": {"payload_ref": "archive-embedded", **encrypt_payload(key, payloads["archive-embedded"])},
    }
    controller.paths.encrypted_payload_log.parent.mkdir(parents=True, exist_ok=True)
    archive_order = ("archive-embedded", *refs)
    controller.paths.encrypted_payload_log.write_text(
        "".join(json.dumps(envelopes[payload_ref]) + "\n" for payload_ref in archive_order),
        encoding="utf-8",
    )
    store = DashboardStore(
        controller.paths.dashboard_db,
        payload_path=controller.paths.encrypted_payload_log,
        payload_key=key,
    )
    store.import_audit_jsonl(audit_path)
    store.import_encrypted_payloads()
    with sqlite3.connect(store.database_path) as connection:
        call_id = connection.execute(
            "SELECT id FROM api_calls WHERE payload_ref='archive-embedded'"
        ).fetchone()[0]
        embedded = envelopes["archive-embedded"]
        connection.execute(
            """INSERT INTO call_payloads(payload_ref, api_call_id, nonce, ciphertext, captured_at)
               VALUES ('archive-embedded', ?, ?, ?, '2026-08-10T10:02:00+00:00')""",
            (call_id, embedded["nonce"], embedded["ciphertext"]),
        )
    source_bytes = controller.paths.encrypted_payload_log.stat().st_size
    source_inode = controller.paths.encrypted_payload_log.stat().st_ino

    with TestClient(create_dashboard_app(controller=controller, store=store, access_token="test-token")) as client:
        headers = {"Authorization": "Bearer test-token"}
        preview = client.get("/v1/storage/archive-compression/preview", headers=headers).json()
        assert preview["state"] == "ready"
        assert preview["archive"]["eligible_payload_count"] == 2
        assert preview["archive"]["embedded_payload_count"] == 1
        with sqlite3.connect(store.database_path) as connection:
            connection.execute("CREATE TABLE unrelated_archive_preview_write(id INTEGER PRIMARY KEY)")
            connection.execute("INSERT INTO unrelated_archive_preview_write DEFAULT VALUES")
        response = client.post(
            "/v1/storage/archive-compression/execute",
            headers=headers,
            json={"preview_id": preview["preview_id"], "confirmed": True},
        )
        assert response.status_code == 200
        job_id = response.json()["job_id"]
        for _ in range(500):
            job = client.get(f"/v1/storage/retention/jobs/{job_id}", headers=headers).json()
            if job["state"] in {"completed", "failed", "canceled"}:
                break
            threading.Event().wait(0.01)
        second_preview = client.get("/v1/storage/archive-compression/preview", headers=headers).json()

    assert job["state"] == "completed"
    assert job["result"]["compressed_payload_count"] == 1
    assert job["result"]["already_compressed_payload_count"] == 1
    assert job["result"]["embedded_payload_count"] == 1
    assert job["result"]["unreadable_payload_count"] == 0
    assert job["result"]["unindexed_payload_count"] == 1
    assert job["result"]["released_bytes"] > 0
    assert controller.paths.encrypted_payload_log.stat().st_size < source_bytes
    assert controller.paths.encrypted_payload_log.stat().st_ino != source_inode
    assert second_preview["state"] == "no_eligible_payloads"
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM call_payloads").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM payload_archive_index").fetchone()[0] == 3
        assert connection.execute(
            "SELECT ciphertext FROM call_payloads WHERE payload_ref='archive-embedded'"
        ).fetchone()[0] == envelopes["archive-embedded"]["ciphertext"]
    for call in store.recent_calls(limit=10):
        section = store.call_payload_section(int(call["id"]), "input")
        assert section is not None
        assert section["state"] == "available"


def test_archive_compression_marks_preview_stale_when_eligibility_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router_home = tmp_path / "router-home"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    controller = RouterController(WorkspacePaths(router_home), port=45454)
    controller.codex_mode_status = lambda: {"client_mode": "native_openai"}
    key = b"e" * 32
    payload_ref = "archive-eligibility-change"
    audit_path = tmp_path / "route-audit.jsonl"
    audit_path.write_text(
        json.dumps({"timestamp": "2026-08-10T10:00:00+00:00", "status": "success", "payload_ref": payload_ref}) + "\n",
        encoding="utf-8",
    )
    plaintext = json.dumps({"payload_ref": payload_ref, "request": {"input": "z" * (256 * 1024)}}).encode()
    envelope = {"payload_ref": payload_ref, **encrypt_payload(key, plaintext)}
    controller.paths.encrypted_payload_log.parent.mkdir(parents=True, exist_ok=True)
    controller.paths.encrypted_payload_log.write_text(json.dumps(envelope) + "\n", encoding="utf-8")
    store = DashboardStore(
        controller.paths.dashboard_db,
        payload_path=controller.paths.encrypted_payload_log,
        payload_key=key,
    )
    store.import_audit_jsonl(audit_path)
    store.import_encrypted_payloads()

    with TestClient(create_dashboard_app(controller=controller, store=store, access_token="test-token")) as client:
        headers = {"Authorization": "Bearer test-token"}
        preview = client.get("/v1/storage/archive-compression/preview", headers=headers).json()
        assert preview["state"] == "ready"
        with sqlite3.connect(store.database_path) as connection:
            call_id = connection.execute(
                "SELECT id FROM api_calls WHERE payload_ref=?",
                (payload_ref,),
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO call_payloads(payload_ref, api_call_id, nonce, ciphertext, captured_at)
                   VALUES (?, ?, ?, ?, '2026-08-10T10:00:00+00:00')""",
                (payload_ref, call_id, envelope["nonce"], envelope["ciphertext"]),
            )
        response = client.post(
            "/v1/storage/archive-compression/execute",
            headers=headers,
            json={"preview_id": preview["preview_id"], "confirmed": True},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "preview_stale"
    assert not list(controller.paths.storage_maintenance_dir.glob("job-*.json"))


def test_archive_compression_rolls_back_file_and_index_when_final_canary_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router_home = tmp_path / "router-home"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    controller = RouterController(WorkspacePaths(router_home), port=45454)
    controller.codex_mode_status = lambda: {"client_mode": "native_openai"}
    key = b"r" * 32
    payload_ref = "archive-rollback"
    audit_path = tmp_path / "route-audit.jsonl"
    audit_path.write_text(
        json.dumps({"timestamp": "2026-08-10T10:00:00+00:00", "status": "success", "payload_ref": payload_ref}) + "\n",
        encoding="utf-8",
    )
    plaintext = json.dumps({"payload_ref": payload_ref, "request": {"input": "y" * (256 * 1024)}}).encode()
    envelope = {"payload_ref": payload_ref, **encrypt_payload(key, plaintext)}
    controller.paths.encrypted_payload_log.parent.mkdir(parents=True, exist_ok=True)
    controller.paths.encrypted_payload_log.write_text(json.dumps(envelope) + "\n", encoding="utf-8")
    store = DashboardStore(
        controller.paths.dashboard_db,
        payload_path=controller.paths.encrypted_payload_log,
        payload_key=key,
    )
    store.import_audit_jsonl(audit_path)
    store.import_encrypted_payloads()
    source_bytes = controller.paths.encrypted_payload_log.read_bytes()
    source_inode = controller.paths.encrypted_payload_log.stat().st_ino
    monkeypatch.setattr(store, "payload_archive_canary_status", lambda *, limit=3: {"tested": 1, "available": 0, "passed": False})

    with TestClient(create_dashboard_app(controller=controller, store=store, access_token="test-token")) as client:
        headers = {"Authorization": "Bearer test-token"}
        preview = client.get("/v1/storage/archive-compression/preview", headers=headers).json()
        response = client.post(
            "/v1/storage/archive-compression/execute",
            headers=headers,
            json={"preview_id": preview["preview_id"], "confirmed": True},
        )
        job_id = response.json()["job_id"]
        for _ in range(500):
            job = client.get(f"/v1/storage/retention/jobs/{job_id}", headers=headers).json()
            if job["state"] in {"completed", "failed", "canceled"}:
                break
            threading.Event().wait(0.01)

    assert job["state"] == "failed"
    assert controller.paths.encrypted_payload_log.read_bytes() == source_bytes
    assert controller.paths.encrypted_payload_log.stat().st_ino == source_inode
    with sqlite3.connect(store.database_path) as connection:
        indexed_inode = connection.execute(
            "SELECT source_inode FROM payload_archive_index WHERE payload_ref=?", (payload_ref,)
        ).fetchone()[0]
    assert indexed_inode == source_inode


def test_sqlite_compaction_marks_preview_stale_after_normal_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _app(tmp_path, monkeypatch)
    headers = {"Authorization": "Bearer test-token"}
    client.app.state.storage_service._controller.codex_mode_status = lambda: {"client_mode": "native_openai"}
    preview = client.get("/v1/storage/compaction/preview", headers=headers).json()

    wrong_confirmation = client.post(
        "/v1/storage/compaction/execute",
        headers=headers,
        json={"preview_id": preview["preview_id"], "confirm": "COMPACT"},
    )
    with sqlite3.connect(client.app.state.storage_service.store.database_path) as connection:
        connection.execute("CREATE TABLE stale_compaction_probe(id INTEGER)")
        connection.execute("INSERT INTO stale_compaction_probe(id) VALUES (1)")
    stale_execute = client.post(
        "/v1/storage/compaction/execute",
        headers=headers,
        json={"preview_id": preview["preview_id"], "confirmed": True},
    )

    assert wrong_confirmation.status_code == 400
    assert stale_execute.status_code == 400
    assert stale_execute.json()["detail"] == "preview_stale"
    assert client.app.state.storage_service.maintenance_status()["state"] == "idle"
    assert not list(client.app.state.storage_service.paths.storage_maintenance_dir.glob("job-*.json"))
    assert not (client.app.state.storage_service.paths.storage_maintenance_dir / "maintenance.lock").exists()
    refreshed_preview = client.get("/v1/storage/compaction/preview", headers=headers).json()
    assert refreshed_preview["preview_id"] != preview["preview_id"]
    with sqlite3.connect(client.app.state.storage_service.store.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM stale_compaction_probe").fetchone()[0] == 1


def test_sqlite_compaction_ignores_empty_wal_metadata_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _app(tmp_path, monkeypatch)
    headers = {"Authorization": "Bearer test-token"}
    service = client.app.state.storage_service
    service._controller.codex_mode_status = lambda: {"client_mode": "native_openai"}
    preview = client.get("/v1/storage/compaction/preview", headers=headers).json()

    with sqlite3.connect(service.store.database_path) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    response = client.post(
        "/v1/storage/compaction/execute",
        headers=headers,
        json={"preview_id": preview["preview_id"], "confirmed": True},
    )

    assert response.status_code == 200
    job_id = response.json()["job_id"]
    for _ in range(300):
        job = client.get(f"/v1/storage/retention/jobs/{job_id}", headers=headers).json()
        if job["state"] in {"completed", "failed", "canceled"}:
            break
        threading.Event().wait(0.01)
    assert job["state"] == "completed"


def test_sqlite_compaction_rejects_non_native_mode_without_creating_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _app(tmp_path, monkeypatch)
    headers = {"Authorization": "Bearer test-token"}
    service = client.app.state.storage_service
    service._controller.codex_mode_status = lambda: {"client_mode": "smart_router"}
    preview = client.get("/v1/storage/compaction/preview", headers=headers).json()

    response = client.post(
        "/v1/storage/compaction/execute",
        headers=headers,
        json={"preview_id": preview["preview_id"], "confirmed": True},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "codex_mode_required"
    assert service.maintenance_status()["state"] == "idle"
    assert not list(service.paths.storage_maintenance_dir.glob("job-*.json"))
    assert not (service.paths.storage_maintenance_dir / "maintenance.lock").exists()


def test_storage_maintenance_settings_persist_without_automatic_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _app(tmp_path, monkeypatch)
    headers = {"Authorization": "Bearer test-token"}

    response = client.post(
        "/v1/storage/maintenance/settings",
        headers=headers,
        json={
            "enabled": True,
            "limit_gib": 5,
            "cleanup_ratio": 0.8,
            "minimum_retention_hours": 168,
            "delete_mode": "payload_only",
            "check_interval_hours": 24,
        },
    )
    assert response.status_code == 200
    for _ in range(200):
        status = client.get("/v1/storage/overview", headers=headers).json()
        if status["automatic_check"].get("checked_at"):
            break
        threading.Event().wait(0.01)

    stored = client.get("/v1/storage/maintenance/settings", headers=headers)
    assert stored.status_code == 200
    assert stored.json()["enabled"] is True
    assert status["automatic_check"]["needs_maintenance"] is False
    assert status["maintenance"]["state"] == "idle"
    assert not list((tmp_path / "router-home" / "data" / "storage-maintenance").glob("job-*.json"))


def test_task_and_routing_endpoints_return_new_semantics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audit_path = tmp_path / "route-audit.jsonl"
    audit_path.write_text(
        "\n".join([
            '{"timestamp":"2026-08-10T10:00:00+00:00","status":"success","logical_turn_id":"turn-api","selected_alias":"sol-medium","selected_tier":"COMPLEX","routing_cause":"initial_route","usage":{"input_tokens":10,"output_tokens":2}}',
            '{"timestamp":"2026-08-10T10:01:00+00:00","status":"failure","logical_turn_id":"turn-api","selected_alias":"sol-medium","selected_tier":"COMPLEX","routing_cause":"turn_inheritance","usage":{"input_tokens":20,"output_tokens":1}}',
        ]) + "\n",
        encoding="utf-8",
    )
    store = DashboardStore(tmp_path / "routerllm.sqlite3")
    store.import_audit_jsonl(audit_path)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    controller = RouterController(WorkspacePaths(tmp_path / "router-home"), port=45454)

    with TestClient(create_dashboard_app(controller=controller, store=store, access_token="test-token")) as client:
        headers = {"Authorization": "Bearer test-token"}
        tasks = client.get("/v1/tasks?limit=1", headers=headers)
        assert tasks.status_code == 200
        assert tasks.json()["total"] == 1
        assert tasks.json()["items"][0]["call_count"] == 2
        assert tasks.json()["items"][0]["status"] == "partial"

        task_key = tasks.json()["items"][0]["task_key"]
        detail = client.get(f"/v1/tasks/{quote(task_key, safe='')}", headers=headers)
        assert detail.status_code == 200
        assert len(detail.json()["calls"]) == 2

        routing = client.get("/v1/routing/stats", headers=headers)
        assert routing.status_code == 200
        assert routing.json()["items"][0]["task_count"] == 1
        assert routing.json()["items"][0]["call_count"] == 2


def test_call_detail_payload_sections_are_deferred_and_paginated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audit_path = tmp_path / "route-audit.jsonl"
    payload_path = tmp_path / "call-payloads.jsonl.enc"
    key = b"d" * 32
    audit_path.write_text(
        json.dumps({
            "timestamp": "2026-08-11T03:48:45+00:00",
            "status": "success",
            "payload_ref": "payload-detail",
            "usage": {"input_tokens": 20, "output_tokens": 3},
        }) + "\n",
        encoding="utf-8",
    )
    payload = {
        "payload_schema_version": 3,
        "payload_ref": "payload-detail",
        "request": {
            "model": "gpt-test",
            "api_key": "test-api-key-redaction-fixture",
            "authorization": "Bearer abcdefghijklmnopqrstuvwxyz",
            "email": "private@example.com",
            "phone": "416-555-0199",
            "address": "100 Private Street",
            "input": [
                {"role": "developer", "content": "private policy"},
                {"role": "user", "content": "private input"},
                {"role": "user", "content": "second private input"},
                {"role": "user", "content": "<environment_context>private runtime</environment_context>"},
                {"role": "user", "content": "Another language model started to solve this problem and produced a summary."},
                {"role": "user", "content": "CONTEXT CHECKPOINT COMPACTION private checkpoint"},
                {"role": "assistant", "content": "prior assistant response"},
                {"type": "reasoning", "id": "reasoning-1", "summary": []},
                {"type": "function_call", "call_id": "call-1", "name": "lookup", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "call-1", "output": "private result"},
            ],
        },
        "terminal_response": {"status": "completed", "output": []},
        "normalized_output_items": [
            {"type": "message", "content": [{"type": "output_text", "text": "private output"}]},
        ],
        "output_text": "private output",
        "tool_calls": [],
        "aggregation_source": "stream_deltas",
        "stream_events": [
            {"_routerllm_sequence": 0, "type": "response.output_text.delta", "delta": "private "},
            {"_routerllm_sequence": 1, "type": "response.output_text.delta", "delta": "output"},
            {"_routerllm_sequence": 2, "type": "response.reasoning_summary_text.delta", "delta": "hidden"},
            {"_routerllm_sequence": 3, "type": "response.output_item.done", "item": {"type": "message"}},
            {"_routerllm_sequence": 4, "type": "response.function_call_arguments.delta", "delta": "{}"},
            {"_routerllm_sequence": 5, "type": "error", "error": {"message": "captured warning"}},
            {"_routerllm_sequence": 6, "type": "response.completed", "response": {"status": "completed", "output": []}},
        ],
    }
    payload_path.write_text(
        json.dumps({"payload_ref": "payload-detail", **encrypt_payload(key, json.dumps(payload).encode("utf-8"))}) + "\n",
        encoding="utf-8",
    )
    store = DashboardStore(tmp_path / "routerllm.sqlite3", payload_path=payload_path, payload_key=key)
    store.import_audit_jsonl(audit_path)
    store.import_encrypted_payloads()
    call_id = store.recent_calls()[0]["id"]
    controller = RouterController(WorkspacePaths(tmp_path / "router-home"), port=45454)
    original_decrypt = store._decrypt_detail_payload

    with TestClient(create_dashboard_app(controller=controller, store=store, access_token="test-token")) as client:
        headers = {"Authorization": "Bearer test-token"}
        monkeypatch.setattr(store, "_decrypt_detail_payload", lambda _row: (_ for _ in ()).throw(AssertionError("summary must not decrypt")))
        summary = client.get(f"/v1/calls/{call_id}", headers=headers)
        summary_export = client.get(f"/v1/calls/{call_id}/export?level=summary", headers=headers)
        assert summary.status_code == 200
        assert summary_export.status_code == 200
        assert "private input" not in summary.text
        assert "private output" not in summary.text
        assert "private input" not in summary_export.text
        assert "private output" not in summary_export.text
        assert "debug" not in summary_export.json()
        assert summary.json()["payload"]["state"] == "available"
        assert summary.json()["advanced_available"] == {
            "raw_request": True,
            "terminal_response": True,
            "normalized_output": True,
            "stream_events": True,
        }
        assert set(summary.json()["counts"]) == {
            "response_item_count", "visible_message_count", "reasoning_item_count",
            "refusal_count", "tool_call_count", "other_item_count", "terminal_output_item_count",
        }

        monkeypatch.setattr(store, "_decrypt_detail_payload", original_decrypt)
        user_input_response = client.get(f"/v1/calls/{call_id}/user-input", headers=headers)
        context_response = client.get(f"/v1/calls/{call_id}/input-context", headers=headers)
        input_response = client.get(f"/v1/calls/{call_id}/input", headers=headers)
        first_human = client.get(f"/v1/calls/{call_id}/input?origin=human&limit=1", headers=headers)
        second_human = client.get(f"/v1/calls/{call_id}/input?origin=human&cursor=1&limit=1", headers=headers)
        runtime_input = client.get(f"/v1/calls/{call_id}/input?origin=runtime", headers=headers)
        developer_input = client.get(f"/v1/calls/{call_id}/input?origin=developer", headers=headers)
        tool_results = client.get(f"/v1/calls/{call_id}/tool-results", headers=headers)
        invalid_origin = client.get(f"/v1/calls/{call_id}/input?origin=invalid", headers=headers)
        output_response = client.get(f"/v1/calls/{call_id}/output-items", headers=headers)
        visible_output = client.get(f"/v1/calls/{call_id}/visible-output", headers=headers)
        rejected_normalized_output = client.get(f"/v1/calls/{call_id}/normalized-output", headers=headers)
        rejected_raw_request = client.get(f"/v1/calls/{call_id}/raw-request", headers=headers)
        rejected_terminal_response = client.get(f"/v1/calls/{call_id}/terminal-response", headers=headers)
        rejected_stream_events = client.get(f"/v1/calls/{call_id}/stream-events", headers=headers)
        normalized_output = client.get(f"/v1/calls/{call_id}/normalized-output?confirmed=true", headers=headers)
        raw_request = client.get(f"/v1/calls/{call_id}/raw-request?confirmed=true", headers=headers)
        terminal_response = client.get(f"/v1/calls/{call_id}/terminal-response?confirmed=true", headers=headers)
        first_events = client.get(f"/v1/calls/{call_id}/stream-events?limit=2&confirmed=true", headers=headers)
        second_events = client.get(f"/v1/calls/{call_id}/stream-events?cursor=2&limit=2&confirmed=true", headers=headers)
        text_events = client.get(f"/v1/calls/{call_id}/stream-events?type=text_delta&limit=1&confirmed=true", headers=headers)
        second_text_events = client.get(f"/v1/calls/{call_id}/stream-events?type=text_delta&cursor=1&limit=1&confirmed=true", headers=headers)
        function_events = client.get(f"/v1/calls/{call_id}/stream-events?type=function_call&confirmed=true", headers=headers)
        invalid_events = client.get(f"/v1/calls/{call_id}/stream-events?type=invalid&confirmed=true", headers=headers)
        rejected_debug_export = client.get(f"/v1/calls/{call_id}/export?level=debug", headers=headers)
        debug_export = client.get(f"/v1/calls/{call_id}/export?level=debug&confirmed=true", headers=headers)
        audit_events = client.get("/v1/events?limit=30", headers=headers)

    assert user_input_response.status_code == 200
    assert user_input_response.json()["user_messages"] == [
        {"role": "user", "content": "private input"},
        {"role": "user", "content": "second private input"},
    ]
    assert user_input_response.json()["request_metadata"]["model"] == "gpt-test"
    assert context_response.status_code == 200
    assert context_response.json()["context_messages"] == [
        {"role": "developer", "content": "private policy"},
        {"role": "user", "content": "<environment_context>private runtime</environment_context>"},
        {"role": "user", "content": "Another language model started to solve this problem and produced a summary."},
        {"role": "user", "content": "CONTEXT CHECKPOINT COMPACTION private checkpoint"},
        {"role": "assistant", "content": "prior assistant response"},
        {"type": "reasoning", "id": "reasoning-1", "summary": []},
        {"type": "function_call", "call_id": "call-1", "name": "lookup", "arguments": "{}"},
    ]
    assert context_response.json()["tool_results"] == [
        {"type": "function_call_output", "call_id": "call-1", "output": "private result"},
    ]

    assert input_response.json()["request"]["input"][1]["content"] == "private input"
    assert first_human.json()["items"][0]["origin"] == "human"
    assert first_human.json()["items"][0]["content"] == "second private input"
    assert first_human.json()["next_cursor"] == 1
    assert second_human.json()["items"][0]["content"] == "private input"
    assert second_human.json()["next_cursor"] is None
    assert [item["origin"] for item in runtime_input.json()["items"]] == [
        "runtime_environment", "handoff", "compaction",
    ]
    assert developer_input.json()["items"][0]["content"] == "private policy"
    assert tool_results.json()["items"][0]["call_id"] == "call-1"
    assert tool_results.json()["items"][0]["origin"] == "function_call_output"
    assert invalid_origin.status_code == 400
    assert output_response.json()["output_text"] == "private output"
    assert visible_output.json()["visible_output_text"] == "private output"
    assert "normalized_output_items" not in visible_output.json()
    assert rejected_normalized_output.status_code == 400
    assert rejected_raw_request.status_code == 400
    assert rejected_terminal_response.status_code == 400
    assert rejected_stream_events.status_code == 400
    assert normalized_output.json()["normalized_output_items"] == output_response.json()["normalized_output_items"]
    assert raw_request.json()["request"]["input"][1]["content"] == "private input"
    assert terminal_response.json()["terminal_response"] == {"status": "completed", "output": []}
    assert "response" not in terminal_response.json()
    assert len(first_events.json()["events"]) == 2
    assert first_events.json()["next_cursor"] == 2
    assert len(second_events.json()["events"]) == 2
    assert second_events.json()["next_cursor"] == 4
    assert text_events.json()["filtered_event_count"] == 2
    assert text_events.json()["total_event_count"] == 7
    assert text_events.json()["next_cursor"] == 1
    assert second_text_events.json()["next_cursor"] is None
    assert function_events.json()["events"][0]["type"] == "response.function_call_arguments.delta"
    assert invalid_events.status_code == 400
    assert rejected_debug_export.status_code == 400
    assert debug_export.status_code == 200
    assert debug_export.json()["debug"]["raw_request"]["api_key"] == "***REDACTED***"
    assert debug_export.json()["debug"]["raw_request"]["authorization"] == "***REDACTED***"
    assert debug_export.json()["debug"]["raw_request"]["email"] == "***REDACTED***"
    assert debug_export.json()["debug"]["raw_request"]["phone"] == "***REDACTED***"
    assert debug_export.json()["debug"]["raw_request"]["address"] == "***REDACTED***"
    assert "private output" in debug_export.text
    export_events = [
        item for item in audit_events.json()["items"]
        if item["component"] == "dashboard_export"
    ]
    assert {item["event_type"] for item in export_events} == {
        "call_debug_export_rejected", "call_debug_exported",
    }
    assert "private input" not in json.dumps(export_events)
    assert "private output" not in json.dumps(export_events)
    assert "test-api-key-redaction-fixture" not in json.dumps(export_events)
    debug_events = [
        item for item in audit_events.json()["items"]
        if item["component"] == "dashboard_debug"
    ]
    assert {item["event_type"] for item in debug_events} == {
        "call_debug_read_rejected", "call_debug_read_succeeded", "call_debug_read_failed",
    }
    assert {item["details"]["read_type"] for item in debug_events} == {
        "raw-request", "normalized-output", "terminal-response", "stream-events",
    }
    assert "private input" not in json.dumps(debug_events)
    assert "private output" not in json.dumps(debug_events)
    assert "test-api-key-redaction-fixture" not in json.dumps(debug_events)


def test_dashboard_refresh_schedules_only_one_payload_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("CODEXTHRIFT_PAYLOAD_IMPORT_ENABLED", "1")
    controller = RouterController(WorkspacePaths(tmp_path / "router-home"), port=45454)
    store = DashboardStore(tmp_path / "routerllm.sqlite3")
    service = DashboardService(controller, store, QuotaAdapter())
    started = threading.Event()
    release = threading.Event()
    import_count = 0

    def slow_payload_import(**_: object) -> dict[str, int | bool]:
        nonlocal import_count
        import_count += 1
        started.set()
        release.wait(timeout=2)
        return {"imported": 3, "skipped": 1, "complete": True}

    monkeypatch.setattr(store, "import_encrypted_payloads", slow_payload_import)

    first = service.refresh_audit()
    assert started.wait(timeout=1)
    second = service.refresh_audit()

    assert first["payload_import_pending"] is True
    assert second["payload_import_pending"] is True
    assert import_count == 1

    release.set()
    thread = service._payload_import_thread
    assert thread is not None
    thread.join(timeout=1)
    assert service._payload_import_result == {"imported": 3, "skipped": 1}


def test_audit_refresh_is_single_flight_and_skips_unchanged_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    controller = RouterController(WorkspacePaths(tmp_path / "router-home"), port=45454)
    service = DashboardService(controller, DashboardStore(tmp_path / "routerllm.sqlite3"), QuotaAdapter())
    controller.paths.audit_log.parent.mkdir(parents=True, exist_ok=True)
    controller.paths.audit_log.write_text("", encoding="utf-8")
    started = threading.Event()
    release = threading.Event()
    refresh_count = 0

    def refresh() -> dict[str, int | bool]:
        nonlocal refresh_count
        refresh_count += 1
        started.set()
        release.wait(timeout=2)
        return {"imported": 1, "skipped": 0, "offset": controller.paths.audit_log.stat().st_size}

    monkeypatch.setattr(service, "refresh_audit", refresh)

    assert service.schedule_audit_refresh()["ingestion_pending"] is True
    assert started.wait(timeout=1)
    assert service.schedule_audit_refresh()["ingestion_pending"] is True
    assert refresh_count == 1
    release.set()
    assert service.wait_for_audit_refresh(timeout=1)
    assert service.audit_read_model_status()["data_generation"] == 1

    assert service.schedule_audit_refresh()["ingestion_pending"] is False
    assert refresh_count == 1

    controller.paths.audit_log.write_text("new audit bytes\n", encoding="utf-8")
    assert service.schedule_audit_refresh()["ingestion_pending"] is True
    assert service.wait_for_audit_refresh(timeout=1)
    assert refresh_count == 2
    assert service.audit_read_model_status()["data_generation"] == 2


def test_database_write_freeze_drains_audit_writer_and_blocks_new_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = RouterController(WorkspacePaths(tmp_path / "router-home"), port=45454)
    service = DashboardService(controller, DashboardStore(tmp_path / "routerllm.sqlite3"), QuotaAdapter())
    controller.paths.audit_log.parent.mkdir(parents=True, exist_ok=True)
    controller.paths.audit_log.write_text("", encoding="utf-8")
    started = threading.Event()
    release = threading.Event()

    def refresh() -> dict[str, int | bool]:
        started.set()
        release.wait(timeout=2)
        return {"imported": 0, "skipped": 0, "offset": 0}

    monkeypatch.setattr(service, "refresh_audit", refresh)
    assert service.schedule_audit_refresh()["ingestion_pending"] is True
    assert started.wait(timeout=1)
    threading.Timer(0.05, release.set).start()

    with service.freeze_database_writes(timeout=1):
        assert service.wait_for_audit_refresh(timeout=0) is True
        status = service.schedule_audit_refresh()
        assert status["ingestion_pending"] is False
        assert service._database_write_freeze.is_set()

    assert not service._database_write_freeze.is_set()


def test_cost_endpoints_share_one_audit_refresh_per_screen_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    controller = RouterController(WorkspacePaths(tmp_path / "router-home"), port=45454)
    headers = {"Authorization": "Bearer test-token"}
    query = "start_at=2026-08-11T09:00:00Z&end_at=2026-08-11T12:00:00Z"

    with TestClient(create_dashboard_app(controller=controller, access_token="test-token")) as client:
        service = client.app.state.dashboard_service
        refresh_count = 0
        started = threading.Event()
        release = threading.Event()

        def refresh() -> dict[str, int | bool]:
            nonlocal refresh_count
            refresh_count += 1
            started.set()
            release.wait(timeout=2)
            return {"imported": 1, "skipped": 0, "offset": 0}

        monkeypatch.setattr(service, "refresh_audit", refresh)
        assert client.get(f"/v1/costs/summary?{query}", headers=headers).status_code == 200
        assert started.wait(timeout=1)
        assert client.get(f"/v1/costs/timeline?{query}&timezone=America/Toronto", headers=headers).status_code == 200
        assert client.get(f"/v1/costs/models?{query}", headers=headers).status_code == 200
        assert client.get(f"/v1/costs/calls?{query}&limit=25", headers=headers).status_code == 200
        status = client.get("/v1/data/read-model", headers=headers)
        assert status.status_code == 200
        assert status.json()["ingestion_pending"] is True
        assert "last_error_code" in status.json()
        release.set()
        assert service.wait_for_audit_refresh(timeout=1)

    assert refresh_count == 1


def test_dashboard_read_cache_is_parameterized_single_flight_and_generation_aware(
    tmp_path: Path,
) -> None:
    controller = RouterController(WorkspacePaths(tmp_path / "router-home"), port=45454)
    service = DashboardService(
        controller,
        DashboardStore(tmp_path / "routerllm.sqlite3"),
        QuotaAdapter(),
    )
    started = threading.Event()
    release = threading.Event()
    load_count = 0

    def slow_load() -> dict[str, int]:
        nonlocal load_count
        load_count += 1
        started.set()
        release.wait(timeout=2)
        return {"value": load_count}

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(service.cached_dashboard_read, ("costs", "same"), slow_load)
            for _ in range(4)
        ]
        assert started.wait(timeout=1)
        release.set()
        assert [future.result(timeout=1) for future in futures] == [{"value": 1}] * 4

    assert load_count == 1
    with service._audit_refresh_lock:
        service._audit_data_generation += 1
    assert service.cached_dashboard_read(("costs", "same"), slow_load) == {"value": 2}
    assert load_count == 2


def test_dashboard_api_requires_explicit_confirmation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _app(tmp_path, monkeypatch)

    response = client.post(
        "/v1/actions/proxy/start",
        headers={"Authorization": "Bearer test-token"},
        json={"confirmed": False},
    )

    assert response.status_code == 400


class _GuardedController:
    def __init__(self, paths: WorkspacePaths):
        self.paths = paths

    def full_status(self):
        return {"codex_config": {"configured": True}}

    def stop_proxy(self, *, confirm: str):
        raise AssertionError("must not stop a proxy while Codex still targets it")


def test_stop_proxy_refuses_when_codex_still_uses_router(tmp_path: Path) -> None:
    paths = WorkspacePaths(tmp_path / "router-home")
    service = DashboardService(_GuardedController(paths), DashboardStore(tmp_path / "routerllm.sqlite3"), QuotaAdapter())

    with pytest.raises(ControlError, match="切回原生"):
        service.stop_proxy(request_id="request-1")


class _FakeSubscriptionQuota:
    def __init__(self) -> None:
        self.read_count = 0
        self.consume_count = 0

    def read_snapshot(self, *, profile_id=None):
        self.read_count += 1
        return {
            "profile_id": profile_id or "profile-safe",
            "observed_at": "2026-08-10T12:00:00+00:00",
            "status": "available",
            "account": {"state": "connected", "email": "live@example.com", "email_masked": "li**@example.com", "fingerprint": "safe", "account_type": "chatgpt", "name": "Live User", "identity_source": "local_codex_id_token"},
            "summary": {"account_state": "connected", "account_label": "live@example.com", "plan_type": "plus", "auth_mode": "chatgpt", "available_reset_credits": 2},
            "windows": [{"key": "codex:primary", "label": "Codex primary", "limit_id": "codex", "window_kind": "primary", "remaining_percent": 75.0}],
            "usage": {"summary": {"lifetimeTokens": 10}, "daily_usage_buckets": []},
            "reset_credits": {
                "available_count": 2,
                "count_source": "official_available_count",
                "detail_lower_bound": 1,
                "details_state": "partial",
                "returned_detail_count": 1,
                "missing_detail_count": 1,
                "details": [{"status": "available", "expires_at": "2026-08-31T12:00:00+00:00"}],
                "observed_at": "2026-08-10T12:00:00+00:00",
                "source": "account/rateLimits/read",
                "contract_warning": None,
            },
        }

    def start_login(self):
        return {"status": "pending", "login_id": "login-safe", "auth_url": "https://auth.openai.com/example"}

    def login_status(self, login_id):
        return {"status": "pending", "login_id": login_id}

    def cancel_login(self, login_id):
        return {"status": "cancelled", "login_id": login_id}

    def logout(self):
        return {"status": "logged_out"}

    def consume_reset(self, *, idempotency_key, credit_id=None):
        self.consume_count += 1
        assert credit_id is None
        return {"outcome": "reset"}

    def close(self):
        return None


def test_subscription_api_refreshes_empty_cache_and_requires_auth(tmp_path: Path) -> None:
    controller = RouterController(WorkspacePaths(tmp_path / "router-home"), port=45454)
    quota = _FakeSubscriptionQuota()
    with TestClient(create_dashboard_app(controller=controller, quota=quota, access_token="test-token")) as client:
        assert client.get("/v1/subscription/overview").status_code == 401
        response = client.get("/v1/subscription/overview", headers={"Authorization": "Bearer test-token"})
        assert response.status_code == 200
        assert response.json()["snapshot"]["account"]["email"] == "live@example.com"
        assert response.json()["snapshot"]["account"]["name"] == "Live User"
        assert response.json()["snapshot"]["account"]["identity_source"] == "local_codex_id_token"
        assert response.json()["snapshot"]["reset_credits"]["available_count"] == 2
        assert response.json()["snapshot"]["reset_credits"]["details"][0]["expires_at"] == "2026-08-31T12:00:00+00:00"
        assert response.json()["cache_state"] == "refreshed"
        assert quota.read_count == 1

        cached = client.get("/v1/subscription/overview", headers={"Authorization": "Bearer test-token"})
        assert cached.status_code == 200
        assert cached.json()["cache_state"] == "cached"
        assert quota.read_count == 1

        refreshed = client.post("/v1/subscription/refresh", headers={"Authorization": "Bearer test-token"}, json={})
        assert refreshed.status_code == 200
        assert refreshed.json()["cache_state"] == "refreshed"
        assert quota.read_count == 2

        login = client.post("/v1/subscription/login/start", headers={"Authorization": "Bearer test-token"}, json={})
        assert login.json()["login_id"] == "login-safe"

        logout = client.post("/v1/subscription/logout", headers={"Authorization": "Bearer test-token"}, json={"confirmed": False})
        assert logout.status_code == 400

    assert b"live@example.com" not in controller.paths.dashboard_db.read_bytes()


class _FakeSubscriptionSections:
    def __init__(self, *, usage_available: bool = True) -> None:
        self.usage_available = usage_available
        self.calls: list[str] = []

    def read_account_section(self, *, profile_id=None, observed_at=None):
        self.calls.append("account")
        return {
            "profile_id": profile_id or "profile-safe",
            "observed_at": observed_at or "2026-08-10T12:00:00+00:00",
            "status": "available",
            "error_message": None,
            "account": {"state": "connected", "email": "section@example.com"},
            "summary": {"account_state": "connected", "account_label": "section@example.com"},
        }

    def read_limits_section(self, *, profile_id=None, observed_at=None):
        self.calls.append("limits")
        return {
            "profile_id": profile_id or "profile-safe",
            "observed_at": observed_at or "2026-08-10T12:00:00+00:00",
            "status": "available",
            "error_message": None,
            "summary": {"available_reset_credits": 2},
            "limits": [],
            "windows": [
                {"key": "primary", "label": "Primary", "remaining_percent": 80},
                {"key": "secondary", "label": "Secondary", "remaining_percent": 60},
            ],
            "reset_credits": {"available_count": 2, "details_state": "count_only"},
        }

    def read_usage_section(self, *, profile_id=None, observed_at=None):
        self.calls.append("usage")
        return {
            "profile_id": profile_id or "profile-safe",
            "observed_at": observed_at or "2026-08-10T12:00:00+00:00",
            "status": "available" if self.usage_available else "unavailable",
            "error_message": None if self.usage_available else "usage unavailable",
            "usage": {"summary": {"lifetimeTokens": 22}, "daily_usage_buckets": []},
        }

    def close(self):
        return None


def test_subscription_section_endpoints_are_independent(tmp_path: Path) -> None:
    controller = RouterController(WorkspacePaths(tmp_path / "router-home"), port=45455)
    quota = _FakeSubscriptionSections()
    with TestClient(create_dashboard_app(controller=controller, quota=quota, access_token="test-token")) as client:
        headers = {"Authorization": "Bearer test-token"}
        account = client.post("/v1/subscription/account", headers=headers, json={})
        limits = client.post("/v1/subscription/limits", headers=headers, json={})
        usage = client.post("/v1/subscription/usage", headers=headers, json={})

    assert account.status_code == 200
    assert account.json()["snapshot"]["account"]["email"] == "section@example.com"
    assert "usage" not in account.json()["snapshot"]
    assert "windows" not in account.json()["snapshot"]
    assert limits.status_code == 200
    assert limits.json()["snapshot"]["windows"]
    assert "account" not in limits.json()["snapshot"]
    assert "usage" not in limits.json()["snapshot"]
    assert usage.status_code == 200
    assert usage.json()["snapshot"]["usage"]["summary"]["lifetimeTokens"] == 22
    assert "account" not in usage.json()["snapshot"]
    assert "windows" not in usage.json()["snapshot"]
    assert quota.calls == ["account", "limits", "usage"]


def test_subscription_section_failure_preserves_other_cached_sections(tmp_path: Path) -> None:
    controller = RouterController(WorkspacePaths(tmp_path / "router-home"), port=45456)
    store = DashboardStore(tmp_path / "routerllm.sqlite3")
    store.initialize()
    store.record_quota_snapshot(
        {
            "profile_id": "profile-safe",
            "observed_at": "2026-08-10T12:00:00+00:00",
            "status": "available",
            "account": {"state": "connected", "email_masked": "ca******@example.com"},
            "summary": {"account_state": "connected"},
            "limits": [],
            "windows": [{"key": "primary", "label": "Primary", "remaining_percent": 70}],
            "usage": {"summary": {"lifetimeTokens": 10}, "daily_usage_buckets": []},
            "reset_credits": {"available_count": 1, "details_state": "count_only", "details": []},
        }
    )
    service = DashboardService(controller, store, _FakeSubscriptionSections(usage_available=False))
    result = service.subscription_section("usage", request_id="request-usage", force=True)

    assert result["snapshot"]["status"] == "stale"
    assert "account" not in result["snapshot"]
    assert "windows" not in result["snapshot"]
    assert result["snapshot"]["section_errors"]["usage"] == "usage unavailable"
    assert store.latest_quota()["account"]["state"] == "connected"
    assert store.latest_quota()["windows"][0]["remaining_percent"] == 70


def test_subscription_account_failure_preserves_cached_identity(tmp_path: Path) -> None:
    controller = RouterController(WorkspacePaths(tmp_path / "router-home"), port=45457)
    store = DashboardStore(tmp_path / "routerllm.sqlite3")
    store.initialize()
    store.record_quota_snapshot(
        {
            "profile_id": "profile-safe",
            "observed_at": "2026-08-10T12:00:00+00:00",
            "status": "available",
            "account": {"state": "connected", "email_masked": "ca******@example.com"},
            "summary": {"account_state": "connected"},
            "limits": [],
            "windows": [],
            "usage": {"summary": {}, "daily_usage_buckets": []},
            "reset_credits": {"available_count": 0, "details_state": "complete", "details": []},
        }
    )

    class AccountUnavailableSections(_FakeSubscriptionSections):
        def read_account_section(self, *, profile_id=None, observed_at=None):
            self.calls.append("account")
            return {
                "profile_id": profile_id,
                "observed_at": observed_at,
                "status": "unavailable",
                "error_message": "account unavailable",
                "account": {"state": "identity_unavailable"},
                "summary": {"account_state": "identity_unavailable"},
            }

    service = DashboardService(controller, store, AccountUnavailableSections())
    result = service.subscription_section("account", request_id="request-account", force=True)

    assert result["snapshot"]["status"] == "stale"
    assert result["snapshot"]["account"]["email_masked"] == "ca******@example.com"
    assert result["snapshot"]["summary"]["account_state"] == "connected"
    assert "windows" not in result["snapshot"]
    assert result["snapshot"]["section_errors"]["account"] == "account unavailable"


def test_late_account_refresh_cannot_restore_stale_limits(tmp_path: Path) -> None:
    controller = RouterController(WorkspacePaths(tmp_path / "router-home"), port=45458)
    store = DashboardStore(tmp_path / "routerllm.sqlite3")
    store.initialize()
    store.record_quota_snapshot(
        {
            "profile_id": "profile-safe",
            "observed_at": "2026-08-10T12:00:00+00:00",
            "status": "available",
            "account": {"state": "connected", "email_masked": "ca******@example.com"},
            "summary": {"account_state": "connected"},
            "windows": [{"key": "primary", "label": "Primary", "remaining_percent": 87}],
            "usage": {"summary": {"lifetimeTokens": 10}, "daily_usage_buckets": []},
            "reset_credits": {"available_count": 1, "details_state": "count_only", "details": []},
        }
    )
    service = DashboardService(controller, store, _FakeSubscriptionSections())

    limits = service.subscription_section("limits", request_id="request-limits", force=True)
    account = service.subscription_section("account", request_id="request-account", force=True)

    assert limits["snapshot"]["windows"][0]["remaining_percent"] == 80
    assert "account" not in limits["snapshot"]
    assert "windows" not in account["snapshot"]
    assert store.latest_quota()["windows"][0]["remaining_percent"] == 80
    assert len(store.latest_quota()["windows"]) == 2


def test_parallel_subscription_sections_persist_distinct_windows(tmp_path: Path) -> None:
    controller = RouterController(WorkspacePaths(tmp_path / "router-home"), port=45459)
    store = DashboardStore(tmp_path / "routerllm.sqlite3")
    service = DashboardService(controller, store, _FakeSubscriptionSections())

    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(
            pool.map(
                lambda section: service.subscription_section(
                    section,
                    request_id=f"request-{section}",
                    force=True,
                ),
                ("account", "limits", "usage"),
            )
        )

    assert all(response["snapshot"]["status"] == "available" for response in responses)
    latest = store.latest_quota()
    assert latest is not None
    assert {window["window_key"] for window in latest["windows"]} == {"primary", "secondary"}


def test_reset_consumption_rejects_raw_credit_id_and_refreshes_snapshot(tmp_path: Path) -> None:
    controller = RouterController(WorkspacePaths(tmp_path / "router-home"), port=45454)
    quota = _FakeSubscriptionQuota()
    service = DashboardService(controller, DashboardStore(tmp_path / "routerllm.sqlite3"), quota)

    result = service.consume_reset(
        ResetCreditAction(confirmed=True, idempotency_key="reset-refresh-0001"),
        request_id="request-reset-1",
    )

    assert result["outcome"] == "reset"
    assert quota.consume_count == 1
    assert quota.read_count == 1

    with TestClient(create_dashboard_app(controller=controller, quota=quota, access_token="test-token")) as client:
        response = client.post(
            "/v1/actions/quota/consume-reset",
            headers={"Authorization": "Bearer test-token"},
            json={
                "confirmed": True,
                "idempotency_key": "reset-refresh-0002",
                "credit_id": "must-be-rejected",
            },
        )
        assert response.status_code == 422


def test_cost_endpoints_share_cache_aware_totals_and_detail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audit_path = tmp_path / "route-audit.jsonl"
    audit_path.write_text("".join(json.dumps(record) + "\n" for record in [
        {
            "audit_schema_version": 5,
            "timestamp": "2026-08-11T10:00:00+00:00",
            "status": "success",
            "response_model": "gpt-5.6-sol",
            "billing_mode": "chatgpt_subscription_included",
            "response_cost": 0,
            "usage": {
                "input_tokens": 120_477,
                "cache_read_input_tokens": 3_584,
                "cache_write_tokens": 0,
                "output_tokens": 81,
            },
        },
        {
            "audit_schema_version": 5,
            "timestamp": "2026-08-11T11:00:00+00:00",
            "status": "success",
            "response_model": "gpt-5.6-terra",
            "billing_mode": "unknown",
            "usage": {
                "input_tokens": 100_000,
                "cache_read_input_tokens": 0,
                "cache_write_tokens": 0,
                "output_tokens": 10_000,
            },
        },
    ]), encoding="utf-8")
    store = DashboardStore(tmp_path / "routerllm.sqlite3")
    store.import_audit_jsonl(audit_path)
    controller = RouterController(WorkspacePaths(tmp_path / "router-home"), port=45454)
    headers = {"Authorization": "Bearer test-token"}
    query = "start_at=2026-08-11T09:00:00Z&end_at=2026-08-11T12:00:00Z"

    with TestClient(create_dashboard_app(controller=controller, store=store, access_token="test-token")) as client:
        summary = client.get(f"/v1/costs/summary?{query}", headers=headers)
        timeline = client.get(f"/v1/costs/timeline?{query}&timezone=America/Toronto&bucket=hour", headers=headers)
        models = client.get(f"/v1/costs/models?{query}", headers=headers)
        calls = client.get(f"/v1/costs/calls?{query}&sort_by=api_equivalent_cost", headers=headers)
        detail = client.get(f"/v1/costs/calls/{calls.json()['items'][0]['id']}", headers=headers)
        catalog = client.get("/v1/pricing/catalog", headers=headers)

    assert summary.status_code == 200
    assert summary.json()["call_count"] == 2
    assert summary.json()["api_equivalent_cost_microusd"] == 908_687
    assert summary.json()["display_api_equivalent_cost_microusd"] == 908_687
    assert summary.json()["known_estimate_coverage"] == 1.0
    assert summary.json()["estimate_display_status"] == "complete"
    assert summary.json()["actual_incremental_cost_microusd"] == 0
    assert summary.json()["actual_known_call_count"] == 1
    assert sum(item["api_equivalent_cost_microusd"] for item in timeline.json()["items"]) == 908_687
    assert {item["model"] for item in models.json()["items"]} == {"gpt-5.6-sol", "gpt-5.6-terra"}
    assert all(item["display_api_equivalent_cost_microusd"] for item in models.json()["items"])
    assert calls.json()["total"] == 2
    assert "safe_metadata" not in calls.json()["items"][0]
    assert "applied_rates_json" not in calls.json()["items"][0]
    assert "safe_metadata" in detail.json()
    assert detail.json()["pricing_basis"] == "openai_api_standard"
    assert len(detail.json()["cost_items"]) == 4
    assert detail.json()["estimate_disclosure"] == "complete_estimate"
    assert detail.json()["billing_assumptions"] == []
    assert all(item["quantity_status"] == "observed" for item in detail.json()["cost_items"])
    assert len(catalog.json()["items"]) == 8
