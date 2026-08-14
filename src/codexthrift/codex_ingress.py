from __future__ import annotations

import ipaddress
import json
import logging
import re
import threading
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import uvicorn
import zstandard
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .paths import WorkspacePaths

CODEX_INGRESS_HOST = "127.0.0.1"
CODEX_INGRESS_PORT = 4877
_REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_SAFE_REQUEST_HEADERS = frozenset({"accept", "content-type", "openai-beta"})
_SAFE_RESPONSE_HEADERS = frozenset({"content-type", "openai-processing-ms"})
LOGGER = logging.getLogger(__name__)
_MAX_DECOMPRESSED_REQUEST_BYTES = 64 * 1024 * 1024


def is_loopback_address(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return value.casefold() == "localhost"


def codex_ingress_base_url() -> str:
    return f"http://{CODEX_INGRESS_HOST}:{CODEX_INGRESS_PORT}/v1"


def _request_id(request: Request) -> str:
    value = request.headers.get("x-request-id", "")
    return value if _REQUEST_ID_PATTERN.fullmatch(value) else uuid.uuid4().hex


def _master_key(paths: WorkspacePaths) -> str:
    try:
        value = paths.master_key_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise HTTPException(status_code=503, detail="CodexThrift upstream credential is unavailable.") from exc
    if not value:
        raise HTTPException(status_code=503, detail="CodexThrift upstream credential is unavailable.")
    return value


def _upstream_headers(request: Request, *, master_key: str, request_id: str) -> dict[str, str]:
    headers = {
        name: value
        for name, value in request.headers.items()
        if name.casefold() in _SAFE_REQUEST_HEADERS
    }
    headers["authorization"] = f"Bearer {master_key}"
    headers["x-request-id"] = request_id
    return headers


def _response_headers(response: httpx.Response, request_id: str) -> dict[str, str]:
    headers = {
        name: value
        for name, value in response.headers.items()
        if name.casefold() in _SAFE_RESPONSE_HEADERS
    }
    headers["x-request-id"] = request_id
    return headers


def _codex_models_envelope(value: Any) -> dict[str, list[dict[str, Any]]]:
    if isinstance(value, dict) and isinstance(value.get("models"), list):
        return {"models": value["models"]}
    return {"models": []}


def _normalized_responses_body(raw_body: bytes) -> bytes:
    try:
        value = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return raw_body
    if not isinstance(value, dict):
        return raw_body
    model = value.get("model")
    if isinstance(model, str) and model.strip():
        return raw_body
    value["model"] = "codex-auto"
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _decoded_request_body(request: Request, raw_body: bytes) -> bytes:
    encoding = request.headers.get("content-encoding", "").strip().casefold()
    if encoding in {"", "identity"}:
        return raw_body
    if encoding != "zstd":
        raise HTTPException(status_code=415, detail="Unsupported request content encoding.")
    try:
        return zstandard.ZstdDecompressor().decompress(
            raw_body,
            max_output_size=_MAX_DECOMPRESSED_REQUEST_BYTES,
        )
    except zstandard.ZstdError as exc:
        raise HTTPException(status_code=400, detail="Invalid compressed request body.") from exc


def verify_codex_ingress_route(
    *,
    ingress_host: str = CODEX_INGRESS_HOST,
    ingress_port: int = CODEX_INGRESS_PORT,
    host: str | None = None,
    port: int | None = None,
    command: str | None = None,
    args: list[str] | None = None,
    timeout_seconds: float = 90.0,
) -> dict[str, Any]:
    del host, port, command, args
    started = time.monotonic()
    headers = {"authorization": "Bearer codexthrift-client-credential-probe"}
    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            models = client.get(
                f"http://{ingress_host}:{ingress_port}/v1/models",
                headers=headers,
            )
            if models.status_code != 200:
                return {
                    "verified": False,
                    "status": "failed",
                    "error_code": "ingress_models_failed",
                    "status_code": models.status_code,
                }
            response = client.post(
                f"http://{ingress_host}:{ingress_port}/v1/responses",
                headers=headers,
                json={
                    "model": "codex-auto",
                    "input": [{
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Return exactly OK."}],
                    }],
                    "stream": False,
                    "max_output_tokens": 16,
                },
            )
        return {
            "verified": response.status_code == 200,
            "status": "verified" if response.status_code == 200 else "failed",
            "error_code": None if response.status_code == 200 else "ingress_responses_failed",
            "status_code": response.status_code,
            "duration_ms": round((time.monotonic() - started) * 1000),
        }
    except (httpx.HTTPError, OSError) as exc:
        return {
            "verified": False,
            "status": "unavailable",
            "error_code": "ingress_unavailable",
            "error": f"{type(exc).__name__}: {exc}",
            "duration_ms": round((time.monotonic() - started) * 1000),
        }


def create_codex_ingress_app(
    *,
    paths: WorkspacePaths | None = None,
    upstream_host: str = "127.0.0.1",
    upstream_port: int = 4000,
    transport: httpx.AsyncBaseTransport | None = None,
    enforce_loopback: bool = True,
) -> FastAPI:
    if not is_loopback_address(upstream_host):
        raise ValueError("Codex ingress upstream must be loopback-only.")
    resolved_paths = paths or WorkspacePaths.default()
    upstream_base_url = f"http://{upstream_host}:{upstream_port}"
    app = FastAPI(title="CodexThrift Codex Ingress", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def loopback_boundary(request: Request, call_next: Any) -> Response:
        client_host = request.client.host if request.client else ""
        if enforce_loopback and not is_loopback_address(client_host):
            return JSONResponse(status_code=403, content={"error": "loopback_required"})
        request.state.request_id = _request_id(request)
        started = time.monotonic()
        response = await call_next(request)
        response.headers["x-request-id"] = request.state.request_id
        LOGGER.info(
            "codex_ingress request_id=%s method=%s path=%s status=%s duration_ms=%s",
            request.state.request_id,
            request.method,
            request.url.path,
            response.status_code,
            round((time.monotonic() - started) * 1000),
        )
        return response

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "bind": f"{CODEX_INGRESS_HOST}:{CODEX_INGRESS_PORT}"}

    @app.get("/v1/models")
    async def models(request: Request) -> Response:
        request_id = request.state.request_id
        headers = _upstream_headers(
            request,
            master_key=_master_key(resolved_paths),
            request_id=request_id,
        )
        async with httpx.AsyncClient(transport=transport, timeout=20.0) as client:
            upstream = await client.get(f"{upstream_base_url}/v1/models", headers=headers)
        response_headers = _response_headers(upstream, request_id)
        if upstream.status_code >= 400:
            return Response(
                content=upstream.content,
                status_code=upstream.status_code,
                headers=response_headers,
            )
        if "client_version" in request.query_params:
            try:
                value = upstream.json()
            except ValueError:
                value = None
            return JSONResponse(
                content=_codex_models_envelope(value),
                status_code=200,
                headers=response_headers,
            )
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=response_headers,
        )

    @app.post("/v1/responses")
    async def responses(request: Request) -> StreamingResponse:
        request_id = request.state.request_id
        headers = _upstream_headers(
            request,
            master_key=_master_key(resolved_paths),
            request_id=request_id,
        )

        raw_request_body = await request.body()
        request_body = _normalized_responses_body(
            _decoded_request_body(request, raw_request_body)
        )

        client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(7_200.0, connect=20.0),
        )
        upstream_request = client.build_request(
            "POST",
            f"{upstream_base_url}/v1/responses",
            headers=headers,
            content=request_body,
        )
        try:
            upstream = await client.send(upstream_request, stream=True)
        except Exception:
            await client.aclose()
            raise

        async def response_body() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(
            response_body(),
            status_code=upstream.status_code,
            headers=_response_headers(upstream, request_id),
        )

    @app.websocket("/v1/responses")
    async def reject_websocket(websocket: WebSocket) -> None:
        client_host = websocket.client.host if websocket.client else ""
        await websocket.accept()
        await websocket.close(code=1008 if not is_loopback_address(client_host) else 1003)

    return app


class CodexIngressServer:
    def __init__(
        self,
        paths: WorkspacePaths,
        *,
        upstream_host: str = "127.0.0.1",
        upstream_port: int = 4000,
        host: str = CODEX_INGRESS_HOST,
        port: int = CODEX_INGRESS_PORT,
    ) -> None:
        if not is_loopback_address(host):
            raise ValueError("Codex ingress must bind to loopback.")
        self.host = host
        self.port = port
        app = create_codex_ingress_app(
            paths=paths,
            upstream_host=upstream_host,
            upstream_port=upstream_port,
        )
        self._server = uvicorn.Server(
            uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
        )
        self._thread: threading.Thread | None = None

    def start(self, *, timeout: float = 5.0) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._server.run,
            name="codexthrift-codex-ingress",
            daemon=True,
        )
        self._thread.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._server.started:
                return
            if not self._thread.is_alive():
                break
            time.sleep(0.02)
        raise RuntimeError(f"Codex ingress failed to bind {self.host}:{self.port}.")

    def stop(self, *, timeout: float = 5.0) -> None:
        thread = self._thread
        if thread is None:
            return
        self._server.should_exit = True
        thread.join(timeout=timeout)
        self._thread = None
