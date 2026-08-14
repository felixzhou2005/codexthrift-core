from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

from .codex_ingress import CODEX_INGRESS_HOST, CODEX_INGRESS_PORT
from .paths import WorkspacePaths
from .util import atomic_write_json, read_json, utc_now_iso

STATE_VERSION = 1
MANAGED_PROVIDER_ID = "openai"
NATIVE_PROVIDER_ID = "openai"
LEGACY_MANAGED_PROVIDER_ID = "litellm"
MANAGED_MODEL = "codex-auto"
ROUTERLLM_KEY_READER_ARGS = frozenset({
    ("--print-codex-proxy-token",),
    ("-m", "codexthrift.key_reader"),
    ("-m", "codex_router_app.key_reader"),
})
MANAGED_KEYS = (
    "model",
    "model_provider",
    "model_reasoning_effort",
    "openai_base_url",
)


class CodexClientMode(StrEnum):
    SMART_ROUTER = "smart_router"
    NATIVE_OPENAI = "native_openai"
    CONFIG_DRIFT = "config_drift"
    UNKNOWN = "unknown"


def router_base_urls(host: str, port: int) -> set[str]:
    return {
        f"http://{host}:{port}/v1",
        f"http://127.0.0.1:{port}/v1",
        f"http://localhost:{port}/v1",
        f"http://{CODEX_INGRESS_HOST}:{CODEX_INGRESS_PORT}/v1",
        f"http://localhost:{CODEX_INGRESS_PORT}/v1",
    }


def is_routerllm_key_reader_args(args: Any) -> bool:
    if not isinstance(args, (list, tuple)):
        return False
    return tuple(str(value) for value in args) in ROUTERLLM_KEY_READER_ARGS


def is_routerllm_key_reader(command: Any, args: Any) -> bool:
    normalized_command = str(command or "")
    normalized_args = [str(value) for value in args] if isinstance(args, (list, tuple)) else []
    if is_routerllm_key_reader_args(normalized_args):
        return bool(normalized_command)
    if normalized_command != "/bin/cat" or len(normalized_args) != 1:
        return False
    key_path = Path(normalized_args[0])
    return key_path.name == "litellm-master-key" and key_path.parent.name == "secrets"


def is_routerllm_provider(provider: Any, *, host: str, port: int) -> bool:
    if not hasattr(provider, "get"):
        return False
    auth = provider.get("auth")
    command = auth.get("command") if hasattr(auth, "get") else None
    args = list(auth.get("args") or []) if hasattr(auth, "get") else []
    return (
        provider.get("name") == "LiteLLM Personal Subscription Router"
        and provider.get("base_url") in router_base_urls(host, port)
        and provider.get("wire_api") == "responses"
        and is_routerllm_key_reader(command, args)
    )


def classify_codex_document(document: Any, *, host: str, port: int) -> dict[str, Any]:
    provider_id = str(document.get("model_provider") or NATIVE_PROVIDER_ID)
    model = document.get("model")
    providers = document.get("model_providers")
    legacy_provider = (
        providers.get(LEGACY_MANAGED_PROVIDER_ID)
        if hasattr(providers, "get")
        else None
    )
    local_openai_override = document.get("openai_base_url") in router_base_urls(host, port)

    if provider_id == NATIVE_PROVIDER_ID and model == MANAGED_MODEL:
        mode = CodexClientMode.SMART_ROUTER if local_openai_override else CodexClientMode.CONFIG_DRIFT
    elif provider_id == NATIVE_PROVIDER_ID:
        mode = CodexClientMode.CONFIG_DRIFT if local_openai_override else CodexClientMode.NATIVE_OPENAI
    elif provider_id == LEGACY_MANAGED_PROVIDER_ID:
        mode = CodexClientMode.CONFIG_DRIFT
    else:
        mode = CodexClientMode.UNKNOWN

    return {
        "client_mode": mode.value,
        "effective_provider": provider_id,
        "effective_model": model,
        "openai_base_url": document.get("openai_base_url"),
        "provider_managed_by_routerllm": local_openai_override,
        "legacy_provider_available": is_routerllm_provider(
            legacy_provider,
            host=host,
            port=port,
        ),
    }


def capture_native_snapshot(paths: WorkspacePaths, document: Any) -> dict[str, Any]:
    managed_keys = {
        key: {
            "present": key in document,
            "value": document.get(key),
        }
        for key in MANAGED_KEYS
    }
    snapshot = {
        "version": STATE_VERSION,
        "captured_at": utc_now_iso(),
        "codex_home": str(paths.codex_home),
        "config_path": str(paths.codex_config),
        "managed_keys": managed_keys,
    }
    atomic_write_json(paths.native_codex_snapshot, snapshot, mode=0o600)
    return snapshot


def read_native_snapshot(paths: WorkspacePaths) -> dict[str, Any] | None:
    value = read_json(paths.native_codex_snapshot)
    if not isinstance(value, dict) or value.get("version") != STATE_VERSION:
        return None
    if value.get("codex_home") != str(paths.codex_home):
        return None
    return value


def record_mode_switch(
    paths: WorkspacePaths,
    mode: CodexClientMode,
    *,
    observed_clients: list[dict[str, Any]] | None = None,
    verification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current = read_mode_state(paths) or {}
    generation = int(current.get("generation") or 0) + 1
    state = {
        "version": STATE_VERSION,
        "generation": generation,
        "mode": mode.value,
        "switched_at": utc_now_iso(),
        "codex_home": str(paths.codex_home),
        "observed_clients": observed_clients or [],
        "verification": verification or {},
    }
    atomic_write_json(paths.codex_mode_state, state, mode=0o600)
    return state


def read_mode_state(paths: WorkspacePaths) -> dict[str, Any] | None:
    value = read_json(paths.codex_mode_state)
    if not isinstance(value, dict) or value.get("version") != STATE_VERSION:
        return None
    if value.get("codex_home") != str(paths.codex_home):
        return None
    return value
