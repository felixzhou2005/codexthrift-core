from __future__ import annotations

from dataclasses import dataclass
from typing import Any

PROTOCOL_VERSION = "1.0"
CORE_FEATURES = [
    "codex.native_switch",
    "quota.reset_credits",
    "routing.phase3a",
    "audit.logical_tasks",
    "compat.dashboard_ipc",
    "runtime.desired_state",
    "codex.mode_switch_jobs",
    "runtime.identity",
    "runtime.graceful_shutdown",
    "subscription.overview",
    "subscription.sections",
    "storage.archive_only_payload_projection",
    "storage.archive_compression.v2",
]
MAX_FRAME_BYTES = 1_048_576


@dataclass(frozen=True)
class RpcProtocolError(Exception):
    code: int
    message: str
    data: Any = None


def validate_request(value: Any) -> tuple[Any, str, dict[str, Any]]:
    if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
        raise RpcProtocolError(-32600, "Invalid Request")
    method = value.get("method")
    if not isinstance(method, str) or not method:
        raise RpcProtocolError(-32600, "Invalid Request")
    params = value.get("params", {})
    if not isinstance(params, dict):
        raise RpcProtocolError(-32602, "Invalid params")
    return value.get("id"), method, params


def result_response(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error_response(request_id: Any, error: RpcProtocolError) -> dict[str, Any]:
    payload: dict[str, Any] = {"code": error.code, "message": error.message}
    if error.data is not None:
        payload["data"] = error.data
    return {"jsonrpc": "2.0", "id": request_id, "error": payload}
