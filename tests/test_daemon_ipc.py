from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import codexthrift.daemon.dispatcher as dispatcher_module
from codexthrift.contracts.protocol import (
    CORE_FEATURES,
    RpcProtocolError,
    validate_request,
)
from codexthrift.daemon.dispatcher import RpcDispatcher
from codexthrift.daemon.event_bus import EventBus
from codexthrift.daemon.ipc_client import IpcClient
from codexthrift.daemon.ipc_server import UnixRpcServer
from codexthrift.daemon.locks import SingleInstanceLock
from codexthrift.daemon.main import DEFAULT_PROXY_PORT, configured_proxy_port
from codexthrift.errors import ControlError


def test_protocol_rejects_invalid_request_and_params() -> None:
    with pytest.raises(RpcProtocolError) as invalid:
        validate_request({"jsonrpc": "1.0", "method": "system.status"})
    assert invalid.value.code == -32600

    with pytest.raises(RpcProtocolError) as invalid_params:
        validate_request({"jsonrpc": "2.0", "method": "system.status", "params": []})
    assert invalid_params.value.code == -32602


def test_configured_proxy_port_preserves_default_without_override() -> None:
    assert configured_proxy_port({}) == DEFAULT_PROXY_PORT
    assert configured_proxy_port({"CODEXTHRIFT_PROXY_PORT": "  "}) == DEFAULT_PROXY_PORT


@pytest.mark.parametrize("value", ["0", "65536", "not-a-port"])
def test_configured_proxy_port_rejects_invalid_overrides(value: str) -> None:
    assert configured_proxy_port({"CODEXTHRIFT_PROXY_PORT": value}) == DEFAULT_PROXY_PORT


def test_configured_proxy_port_accepts_valid_override() -> None:
    assert configured_proxy_port({"CODEXTHRIFT_PROXY_PORT": "44000"}) == 44000


def test_event_bus_returns_events_after_sequence() -> None:
    bus = EventBus()
    first = bus.publish("event.service.changed", {"method": "routing.enable"})

    assert bus.wait(after=0)["events"] == [first]
    assert bus.wait(after=first["sequence"])["events"] == []


def test_dispatcher_handshake_unknown_and_unavailable_methods() -> None:
    identity = {
        "coreVersion": "0.1.1",
        "sourceCommit": "abc123",
        "runtimeManifestSha256": "manifest-sha",
        "coreBuildId": "codexthrift-0.1.1-test",
        "instanceId": "instance-test",
        "pid": 123,
        "startedAt": "2026-08-12T00:00:00+00:00",
        "dataRoot": "/tmp/codexthrift-test",
        "runtimeRoot": "/tmp/codexthrift-runtime",
        "executablePath": "/tmp/codexthriftd",
    }
    dispatcher = RpcDispatcher(SimpleNamespace(), identity=identity)

    handshake = dispatcher.dispatch(
        "core.handshake",
        {"protocolMin": "1.0", "protocolMax": "1.0"},
    )
    assert handshake["protocolVersion"] == "1.0"
    assert "codex.mode_switch_jobs" in CORE_FEATURES
    assert "subscription.overview" in CORE_FEATURES
    assert "subscription.sections" in CORE_FEATURES
    assert "storage.archive_only_payload_projection" in CORE_FEATURES
    assert "storage.archive_compression.v2" in CORE_FEATURES
    assert handshake["coreBuildId"] == "codexthrift-0.1.1-test"
    assert handshake["instanceId"] == "instance-test"
    assert "runtime.identity" in handshake["features"]
    assert "subscription.overview" in handshake["features"]
    assert "subscription.sections" in handshake["features"]
    assert "storage.archive_only_payload_projection" in handshake["features"]
    assert "storage.archive_compression.v2" in handshake["features"]

    with pytest.raises(RpcProtocolError) as unavailable:
        dispatcher.dispatch("context.compact", {})
    assert unavailable.value.code == -32004

    with pytest.raises(RpcProtocolError) as unknown:
        dispatcher.dispatch("example.unknown", {})
    assert unknown.value.code == -32601


def test_shutdown_requires_exact_instance_and_confirmation() -> None:
    dispatcher = RpcDispatcher(
        SimpleNamespace(),
        identity={"instanceId": "instance-test", "coreBuildId": "build-test"},
    )
    with pytest.raises(RpcProtocolError) as mismatch:
        dispatcher.dispatch(
            "system.shutdown",
            {"confirm": "SHUTDOWN_CORE", "instanceId": "other-instance"},
        )
    assert mismatch.value.code == -32012

    with pytest.raises(RpcProtocolError) as confirmation:
        dispatcher.dispatch(
            "system.shutdown",
            {"confirm": "WRONG", "instanceId": "instance-test"},
        )
    assert confirmation.value.code == -32010


def test_shutdown_round_trip_returns_before_server_stops() -> None:
    socket_dir = Path(tempfile.mkdtemp(prefix="ct-", dir="/tmp"))
    socket_path = socket_dir / "codexthrift.sock"
    dispatcher = RpcDispatcher(
        SimpleNamespace(),
        identity={"instanceId": "instance-test", "coreBuildId": "build-test"},
    )
    server = UnixRpcServer(socket_path, dispatcher)
    dispatcher.bind_shutdown(server.shutdown)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 2
        while not socket_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        result = IpcClient(socket_path).call(
            "system.shutdown",
            {"confirm": "SHUTDOWN_CORE", "instanceId": "instance-test"},
        )
        assert result == {
            "accepted": True,
            "instanceId": "instance-test",
            "coreBuildId": "build-test",
        }
        thread.join(timeout=2)
        assert not thread.is_alive()
    finally:
        if thread.is_alive():
            server.shutdown()
            thread.join(timeout=2)
        server.server_close()
        shutil.rmtree(socket_dir)


def test_dispatcher_serializes_write_and_publishes_event() -> None:
    calls: list[str] = []
    controller = SimpleNamespace(stop_proxy=lambda **_params: calls.append("stop") or {"ok": True})
    dispatcher = RpcDispatcher(controller)

    assert dispatcher.dispatch("proxy.stop", {"confirm": "APPLY"}) == {"ok": True}
    events = dispatcher.dispatch("events.subscribe", {"after": 0})

    assert calls == ["stop"]
    assert events["events"][0]["type"] == "event.proxy.health"


def test_dispatcher_initializes_one_dashboard_for_concurrent_first_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialization_count = 0
    initialization_lock = threading.Lock()
    start = threading.Barrier(4)

    class FakeDashboardCompatService:
        def __init__(self, _controller: object) -> None:
            nonlocal initialization_count
            with initialization_lock:
                initialization_count += 1
            time.sleep(0.05)

        def request(self, *, method: str, path: str, body: object = None) -> dict[str, str]:
            return {"method": method, "path": path}

        def close(self) -> None:
            return None

    monkeypatch.setattr(dispatcher_module, "DashboardCompatService", FakeDashboardCompatService)
    dispatcher = RpcDispatcher(SimpleNamespace())
    results: list[dict[str, str]] = []

    def request(section: str) -> None:
        start.wait()
        results.append(
            dispatcher.dispatch(
                "compat.dashboardRequest",
                {"method": "POST", "path": f"/v1/subscription/{section}", "body": None},
            )
        )

    threads = [threading.Thread(target=request, args=(section,)) for section in ("account", "limits", "usage")]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert initialization_count == 1
    assert {result["path"] for result in results} == {
        "/v1/subscription/account",
        "/v1/subscription/limits",
        "/v1/subscription/usage",
    }


def test_unix_rpc_server_round_trip_and_socket_permissions() -> None:
    socket_dir = Path(tempfile.mkdtemp(prefix="ct-ipc-", dir="/tmp"))
    socket_path = socket_dir / "codexthrift.sock"
    dispatcher = RpcDispatcher(SimpleNamespace())
    server = UnixRpcServer(socket_path, dispatcher)
    if os.name != "nt":
        socket_path.chmod(0o600)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = IpcClient(socket_path).call(
            "core.handshake",
            {"protocolMin": "1.0", "protocolMax": "1.0"},
        )
        assert result["protocolVersion"] == "1.0"
        if os.name != "nt":
            assert socket_path.stat().st_mode & 0o777 == 0o600
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        shutil.rmtree(socket_dir)


def test_single_instance_lock_rejects_second_owner(tmp_path: Path) -> None:
    first = SingleInstanceLock(tmp_path / "daemon.lock")
    second = SingleInstanceLock(tmp_path / "daemon.lock")
    first.acquire()
    try:
        with pytest.raises(ControlError):
            second.acquire()
    finally:
        first.release()
