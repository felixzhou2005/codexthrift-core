from __future__ import annotations

import json
import socket
import uuid
from pathlib import Path
from typing import Any

from ..contracts.protocol import MAX_FRAME_BYTES
from ..errors import ControlError


class IpcClient:
    def __init__(self, socket_path: Path, timeout: float = 10.0) -> None:
        self.socket_path = socket_path
        self.timeout = timeout

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        request_id = uuid.uuid4().hex
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self.timeout)
                connection.connect(str(self.socket_path))
                connection.sendall(json.dumps(payload).encode("utf-8") + b"\n")
                response = connection.makefile("rb").readline(MAX_FRAME_BYTES + 1)
        except OSError as exc:
            raise ControlError(f"codexthriftd IPC unavailable: {exc}") from exc
        value = json.loads(response)
        if value.get("id") != request_id:
            raise ControlError("codexthriftd returned a mismatched response id.")
        if "error" in value:
            raise ControlError(str(value["error"].get("message") or "IPC request failed"))
        return value.get("result")
