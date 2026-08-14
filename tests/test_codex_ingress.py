from __future__ import annotations

import json

import httpx
import pytest
import zstandard
from fastapi.testclient import TestClient

from codexthrift.codex_ingress import (
    CodexIngressServer,
    create_codex_ingress_app,
    is_loopback_address,
)
from codexthrift.paths import WorkspacePaths


class _SseStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'data: {"type":"response.completed"}\n\n'


def test_ingress_replaces_client_credentials_and_preserves_stream(tmp_path, monkeypatch, caplog) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    paths = WorkspacePaths(tmp_path / "router-home")
    paths.ensure()
    paths.master_key_file.write_text("router-internal-key\n", encoding="utf-8")
    observed: list[dict[str, object]] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        observed.append({
            "path": request.url.path,
            "authorization": request.headers.get("authorization"),
            "cookie": request.headers.get("cookie"),
            "body": body,
        })
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "codex-auto"}]})
        return httpx.Response(
            200,
            stream=_SseStream(),
            headers={"content-type": "text/event-stream"},
        )

    app = create_codex_ingress_app(
        paths=paths,
        transport=httpx.MockTransport(upstream),
        enforce_loopback=False,
    )
    client = TestClient(app)
    models = client.get(
        "/v1/models?client_version=0.147.0",
        headers={"authorization": "Bearer chatgpt-secret", "cookie": "session=secret"},
    )
    response = client.post(
        "/v1/responses",
        headers={"authorization": "Bearer chatgpt-secret", "cookie": "session=secret"},
        json={"model": "codex-auto", "input": [{"role": "user", "content": []}]},
    )

    assert models.status_code == 200
    assert models.json() == {"models": []}
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.content == b'data: {"type":"response.completed"}\n\n'
    assert [item["authorization"] for item in observed] == [
        "Bearer router-internal-key",
        "Bearer router-internal-key",
    ]
    assert all(item["cookie"] is None for item in observed)
    assert json.loads(observed[1]["body"])["model"] == "codex-auto"
    assert "chatgpt-secret" not in caplog.text


@pytest.mark.parametrize("model", [None, "", "   "])
def test_ingress_supplies_default_model_when_codex_omits_it(
    tmp_path,
    monkeypatch,
    model,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    paths = WorkspacePaths(tmp_path / "router-home")
    paths.ensure()
    paths.master_key_file.write_text("router-internal-key\n", encoding="utf-8")
    observed: list[dict[str, object]] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        observed.append(json.loads(await request.aread()))
        return httpx.Response(200, stream=_SseStream())

    app = create_codex_ingress_app(
        paths=paths,
        transport=httpx.MockTransport(upstream),
        enforce_loopback=False,
    )
    response = TestClient(app).post(
        "/v1/responses",
        json={"model": model, "input": "do-not-log-this-input"},
    )

    assert response.status_code == 200
    assert observed == [{"model": "codex-auto", "input": "do-not-log-this-input"}]


def test_ingress_preserves_explicit_model(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    paths = WorkspacePaths(tmp_path / "router-home")
    paths.ensure()
    paths.master_key_file.write_text("router-internal-key\n", encoding="utf-8")
    observed: list[dict[str, object]] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        observed.append(json.loads(await request.aread()))
        return httpx.Response(200, stream=_SseStream())

    app = create_codex_ingress_app(
        paths=paths,
        transport=httpx.MockTransport(upstream),
        enforce_loopback=False,
    )
    response = TestClient(app).post(
        "/v1/responses",
        json={"model": "explicit-model", "input": "test"},
    )

    assert response.status_code == 200
    assert observed == [{"model": "explicit-model", "input": "test"}]


def test_ingress_decodes_zstd_request_and_supplies_default_model(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    paths = WorkspacePaths(tmp_path / "router-home")
    paths.ensure()
    paths.master_key_file.write_text("router-internal-key\n", encoding="utf-8")
    observed: list[dict[str, object]] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        observed.append({
            "encoding": request.headers.get("content-encoding"),
            "body": json.loads(await request.aread()),
        })
        return httpx.Response(200, stream=_SseStream())

    app = create_codex_ingress_app(
        paths=paths,
        transport=httpx.MockTransport(upstream),
        enforce_loopback=False,
    )
    raw_body = json.dumps({"input": "compressed-test"}).encode("utf-8")
    response = TestClient(app).post(
        "/v1/responses",
        content=zstandard.ZstdCompressor().compress(raw_body),
        headers={"content-type": "application/json", "content-encoding": "zstd"},
    )

    assert response.status_code == 200
    assert observed == [{
        "encoding": None,
        "body": {"input": "compressed-test", "model": "codex-auto"},
    }]


def test_ingress_requires_loopback_bind_and_upstream(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    paths = WorkspacePaths(tmp_path / "router-home")
    assert is_loopback_address("127.0.0.1") is True
    assert is_loopback_address("::1") is True
    assert is_loopback_address("0.0.0.0") is False
    with pytest.raises(ValueError, match="upstream"):
        create_codex_ingress_app(paths=paths, upstream_host="0.0.0.0")
    with pytest.raises(ValueError, match="bind"):
        CodexIngressServer(paths, host="0.0.0.0")
    protected = TestClient(create_codex_ingress_app(paths=paths))
    assert protected.get("/healthz").status_code == 403
