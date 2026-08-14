from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any, Protocol

from .app_server_session import AppServerError, CodexAppServerSession
from .codex_mode import NATIVE_PROVIDER_ID
from .paths import WorkspacePaths


class AppServerTransport(Protocol):
    @property
    def pid(self) -> int | None: ...

    def request(self, method: str, params: Any = None) -> dict[str, Any]: ...

    def close(self) -> None: ...


class CodexModeVerifier:
    def __init__(
        self,
        paths: WorkspacePaths,
        *,
        transport_factory: Callable[[dict[str, str]], AppServerTransport] | None = None,
    ):
        self.paths = paths
        self.transport_factory = transport_factory or (
            lambda env: CodexAppServerSession(env=env, timeout_seconds=20.0)
        )

    def verify(self, expected_provider: str) -> dict[str, Any]:
        env = dict(os.environ)
        env["CODEX_HOME"] = str(self.paths.codex_home)
        transport = self.transport_factory(env)
        try:
            config_response = transport.request("config/read", {})
            account_response = transport.request("account/read", {"refreshToken": True})
        except AppServerError as exc:
            return {
                "verified": False,
                "status": "unavailable",
                "error": str(exc),
                "app_server_pid": transport.pid,
            }
        finally:
            transport.close()

        config = config_response.get("config") if isinstance(config_response.get("config"), dict) else config_response
        provider = config.get("model_provider") or config.get("modelProvider") or NATIVE_PROVIDER_ID
        model = config.get("model")
        account = account_response.get("account") if isinstance(account_response, dict) else None
        account = account if isinstance(account, dict) else {}
        account_type = account.get("type") or account.get("accountType")
        requires_auth = account_response.get("requiresOpenaiAuth") is True
        provider_matches = provider == expected_provider
        auth_required = expected_provider == NATIVE_PROVIDER_ID and (requires_auth or not account_type)
        status = "verified"
        if not provider_matches:
            status = "config_drift"
        elif auth_required:
            status = "auth_required"
        return {
            "verified": provider_matches,
            "status": status,
            "effective_provider": provider,
            "effective_model": model,
            "account_type": account_type,
            "plan_type": account.get("planType") or account.get("plan_type"),
            "native_auth_state": "auth_required" if auth_required else "connected",
            "app_server_pid": transport.pid,
        }
