from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from typing import Any

import httpx

from .util import normalized_child_env

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def verify_codex_proxy_route(
    *,
    host: str,
    port: int,
    command: str,
    args: list[str],
    command_runner: CommandRunner = subprocess.run,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    result: dict[str, Any] = {
        "verified": False,
        "status": "failed",
        "auth_command_status": "pending",
        "models_endpoint_status": "pending",
        "responses_probe_status": "pending",
        "error_code": None,
    }
    try:
        completed = command_runner(
            [command, *args],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
            env=normalized_child_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return _failed(result, "auth_command_unavailable", started)

    token_lines = completed.stdout.strip().splitlines()
    if completed.returncode != 0:
        return _failed(result, "auth_command_failed", started)
    if len(token_lines) != 1 or not token_lines[0].strip():
        return _failed(result, "auth_token_invalid", started)
    token = token_lines[0].strip()
    result["auth_command_status"] = "verified"

    owned_client = client is None
    active_client = client or httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0))
    headers = {"Authorization": f"Bearer {token}"}
    base_url = f"http://{host}:{port}/v1"
    try:
        models = active_client.get(
            f"{base_url}/models",
            params={"client_version": "routerllm-preflight"},
            headers=headers,
        )
        if models.status_code != 200:
            return _failed(result, f"models_http_{models.status_code}", started)
        try:
            model_items = models.json().get("data") or []
        except (ValueError, AttributeError):
            return _failed(result, "models_response_invalid", started)
        model_ids = {
            str(item.get("id"))
            for item in model_items
            if isinstance(item, dict) and item.get("id")
        }
        if "codex-auto" not in model_ids:
            return _failed(result, "codex_auto_missing", started)
        result["models_endpoint_status"] = "verified"

        response = active_client.post(
            f"{base_url}/responses",
            headers=headers,
            json={
                "model": "codex-auto",
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "Reply with exactly OK.",
                            }
                        ],
                    }
                ],
                "max_output_tokens": 16,
                "stream": False,
            },
        )
        if response.status_code != 200:
            return _failed(result, f"responses_http_{response.status_code}", started)
        result["responses_probe_status"] = "verified"
        result["verified"] = True
        result["status"] = "verified"
        result["duration_ms"] = round((time.monotonic() - started) * 1000)
        return result
    except httpx.HTTPError:
        return _failed(result, "proxy_connection_failed", started)
    finally:
        if owned_client:
            active_client.close()


def _failed(result: dict[str, Any], error_code: str, started: float) -> dict[str, Any]:
    result["error_code"] = error_code
    result["duration_ms"] = round((time.monotonic() - started) * 1000)
    return result
