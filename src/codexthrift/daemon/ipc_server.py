from __future__ import annotations

import json
import os
import socketserver
import threading
from pathlib import Path
from typing import Any

from ..codex_ingress import CodexIngressServer
from ..contracts.protocol import (
    MAX_FRAME_BYTES,
    RpcProtocolError,
    error_response,
    result_response,
    validate_request,
)
from ..controller import RouterController
from ..paths import WorkspacePaths
from ..storage.atomic_io import atomic_write_text
from .build_identity import build_identity
from .desired_state import DesiredStateStore
from .dispatcher import RpcDispatcher
from .event_bus import EventBus
from .locks import SingleInstanceLock
from .supervisor import DaemonSupervisor


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        line = self.rfile.readline(MAX_FRAME_BYTES + 1)
        if len(line) > MAX_FRAME_BYTES:
            self._write(error_response(None, RpcProtocolError(-32600, "Frame too large")))
            return
        request_id: Any = None
        try:
            value = json.loads(line)
            request_id, method, params = validate_request(value)
            response = result_response(request_id, self.server.dispatcher.dispatch(method, params))
        except json.JSONDecodeError:
            response = error_response(None, RpcProtocolError(-32700, "Parse error"))
        except RpcProtocolError as exc:
            response = error_response(request_id, exc)
        # The IPC boundary must always return a framed response, even when an
        # unexpected application error escapes the Dashboard compatibility layer.
        except Exception:  # noqa: BLE001
            response = error_response(request_id, RpcProtocolError(-32603, "Internal error"))
        self._write(response)

    def _write(self, value: dict[str, Any]) -> None:
        self.wfile.write(json.dumps(value, ensure_ascii=False).encode("utf-8") + b"\n")


class UnixRpcServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, socket_path: Path, dispatcher: RpcDispatcher) -> None:
        self.dispatcher = dispatcher
        super().__init__(str(socket_path), _Handler)


def serve(paths: WorkspacePaths | None = None, *, port: int = 4000) -> None:
    paths = paths or WorkspacePaths.discover()
    paths.ensure_mutable()
    lock = SingleInstanceLock(paths.daemon_lock)
    lock.acquire()
    socket_path = paths.daemon_socket
    if socket_path.exists():
        socket_path.unlink()
    controller = RouterController(paths, port=port, ensure_paths=False)
    events = EventBus()
    desired_state = DesiredStateStore(paths.desired_state_file)
    controller.bind_proxy_desired_state_writer(
        lambda running, allow_unverified: desired_state.set_proxy_desired(
            running=running,
            allow_unverified=allow_unverified,
        )
    )
    supervisor = DaemonSupervisor(controller, desired_state)
    dispatcher = RpcDispatcher(
        controller,
        event_bus=events,
        supervisor=supervisor,
        identity=build_identity(data_root=paths.root),
    )
    ingress = CodexIngressServer(paths, upstream_port=port)
    try:
        ingress.start()
        atomic_write_text(paths.daemon_pid, f"{os.getpid()}\n", mode=0o600)
        with UnixRpcServer(socket_path, dispatcher) as server:
            dispatcher.bind_shutdown(server.shutdown)
            if os.name != "nt":
                socket_path.chmod(0o600)
            threading.Thread(
                target=dispatcher.restore_startup,
                name="codexthrift-startup-restore",
                daemon=True,
            ).start()
            server.serve_forever()
    finally:
        ingress.stop()
        dispatcher.close()
        paths.daemon_pid.unlink(missing_ok=True)
        socket_path.unlink(missing_ok=True)
        lock.release()
