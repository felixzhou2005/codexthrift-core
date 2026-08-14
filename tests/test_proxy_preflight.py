from __future__ import annotations

import json
import subprocess

import httpx

from codexthrift.proxy_preflight import verify_codex_proxy_route


def _completed(*, stdout: str = "test-token\n", returncode: int = 0):
    def run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], returncode, stdout=stdout, stderr="")

    return run


def test_proxy_preflight_verifies_auth_models_and_response() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "codex-auto"}]})
        return httpx.Response(200, json={"id": "resp_test", "output": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = verify_codex_proxy_route(
            host="127.0.0.1",
            port=4000,
            command="/tmp/routerllm-dashboard",
            args=["--print-codex-proxy-token"],
            command_runner=_completed(),
            client=client,
        )

    assert result["verified"] is True
    assert result["auth_command_status"] == "verified"
    assert result["models_endpoint_status"] == "verified"
    assert result["responses_probe_status"] == "verified"
    assert all(request.headers["Authorization"] == "Bearer test-token" for request in requests)
    response_payload = json.loads(requests[-1].content)
    assert response_payload["input"] == [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "Reply with exactly OK."}
            ],
        }
    ]
    assert "test-token" not in repr(result)


def test_proxy_preflight_rejects_missing_auth_token_without_http_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = verify_codex_proxy_route(
            host="127.0.0.1",
            port=4000,
            command="/tmp/routerllm-dashboard",
            args=["--print-codex-proxy-token"],
            command_runner=_completed(stdout="", returncode=2),
            client=client,
        )

    assert result["verified"] is False
    assert result["error_code"] == "auth_command_failed"
    assert requests == []


def test_proxy_preflight_reports_responses_failure_without_secret() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "codex-auto"}]})
        return httpx.Response(500, json={"error": "upstream failed"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = verify_codex_proxy_route(
            host="127.0.0.1",
            port=4000,
            command="/tmp/routerllm-dashboard",
            args=["--print-codex-proxy-token"],
            command_runner=_completed(),
            client=client,
        )

    assert result["verified"] is False
    assert result["error_code"] == "responses_http_500"
    assert "test-token" not in repr(result)
