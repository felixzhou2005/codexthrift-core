from __future__ import annotations

import secrets
from typing import Any

from fastapi.testclient import TestClient

from ..controller import RouterController
from ..dashboard_api import create_dashboard_app
from ..errors import ControlError


class DashboardCompatService:
    """Expose the legacy Dashboard contract inside the UDS daemon process."""

    def __init__(self, controller: RouterController) -> None:
        self._token = secrets.token_urlsafe(32)
        self._client = TestClient(
            create_dashboard_app(controller=controller, access_token=self._token)
        )
        self._client.__enter__()

    def close(self) -> None:
        self._client.__exit__(None, None, None)

    def request(
        self,
        *,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> Any:
        if method not in {"GET", "POST"}:
            raise ControlError("Dashboard compatibility method must be GET or POST.")
        if not (path == "/healthz" or path.startswith("/v1/")):
            raise ControlError("Dashboard compatibility path is not allowed.")
        response = self._client.request(
            method,
            path,
            json=body if method == "POST" else None,
            headers={"Authorization": f"Bearer {self._token}"},
        )
        value = response.json()
        if response.is_error:
            detail = value.get("detail") if isinstance(value, dict) else None
            raise ControlError(str(detail or "Dashboard compatibility request failed."))
        return value
