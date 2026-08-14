from __future__ import annotations

from typing import Any

from ..quota_adapter import QuotaAdapter


class QuotaService:
    """Application boundary around subscription quota and account actions."""

    def __init__(self, adapter: QuotaAdapter | None = None) -> None:
        self._adapter = adapter or QuotaAdapter()

    def close(self) -> None:
        self._adapter.close()

    def read_snapshot(self, *, profile_id: str | None = None) -> dict[str, Any]:
        return self._adapter.read_snapshot(profile_id=profile_id)

    def read_account_section(self, *, profile_id: str | None = None, observed_at: str | None = None) -> dict[str, Any]:
        return self._adapter.read_account_section(profile_id=profile_id, observed_at=observed_at)

    def read_limits_section(self, *, profile_id: str | None = None, observed_at: str | None = None) -> dict[str, Any]:
        return self._adapter.read_limits_section(profile_id=profile_id, observed_at=observed_at)

    def read_usage_section(self, *, profile_id: str | None = None, observed_at: str | None = None) -> dict[str, Any]:
        return self._adapter.read_usage_section(profile_id=profile_id, observed_at=observed_at)

    def has_account_update(self) -> bool:
        return self._adapter.has_account_update()

    def start_login(self) -> dict[str, Any]:
        return self._adapter.start_login()

    def login_status(self, login_id: str) -> dict[str, Any]:
        return self._adapter.login_status(login_id)

    def cancel_login(self, login_id: str) -> dict[str, Any]:
        return self._adapter.cancel_login(login_id)

    def logout(self) -> dict[str, Any]:
        return self._adapter.logout()

    def consume_reset(
        self,
        *,
        idempotency_key: str,
        credit_id: str | None = None,
    ) -> dict[str, Any]:
        return self._adapter.consume_reset(
            idempotency_key=idempotency_key,
            credit_id=credit_id,
        )
