from __future__ import annotations

from typing import Any

from codexthrift.app_server_session import AppServerError
from codexthrift.quota_adapter import QuotaAdapter


class FakeTransport:
    def __init__(
        self,
        *,
        account: dict[str, Any] | None = None,
        reset_summary: dict[str, Any] | None = None,
        usage_error: bool = False,
    ) -> None:
        self.methods: list[str] = []
        self.account = account
        self.reset_summary = reset_summary if reset_summary is not None else {
            "availableCount": 3,
            "credits": [
                {
                    "id": "must-not-return-credit-id",
                    "resetType": "codexRateLimits",
                    "status": "available",
                    "grantedAt": 1786384800,
                    "expiresAt": 1786989600,
                    "title": "Rate-limit reset",
                    "description": "Reset an eligible Codex rate-limit window.",
                }
            ],
        }
        self.usage_error = usage_error
        self.notifications: list[dict[str, Any]] = []

    def request(self, method: str, params: Any = None) -> dict[str, Any]:
        self.methods.append(method)
        if method == "account/read":
            return {
                "account": self.account if self.account is not None else {"type": "chatgpt", "email": "owner@example.com", "planType": "plus", "accessToken": "must-not-return"},
                "requiresOpenaiAuth": False,
            }
        if method == "account/rateLimits/read":
            return {
                "rateLimits": {
                    "limitId": "codex",
                    "planType": "plus",
                    "credits": {"hasCredits": True, "unlimited": False, "balance": "12.5"},
                    "primary": {"usedPercent": 0, "windowDurationMins": 300, "resetsAt": 1786384800},
                    "secondary": {"usedPercent": 35, "windowDurationMins": 10080, "resetsAt": 1786989600},
                },
                "rateLimitsByLimitId": {
                    "codex": {
                        "limitId": "codex",
                        "limitName": "Codex",
                        "planType": "plus",
                        "primary": {"usedPercent": 0, "windowDurationMins": 300, "resetsAt": 1786384800},
                        "secondary": {"usedPercent": 35, "windowDurationMins": 10080, "resetsAt": 1786989600},
                    },
                    "codex_bengalfox": {
                        "limitId": "codex_bengalfox",
                        "primary": {"usedPercent": 80, "windowDurationMins": 300, "resetsAt": 1786384800},
                    },
                },
                "rateLimitResetCredits": self.reset_summary,
            }
        if method == "account/usage/read":
            if self.usage_error:
                raise AppServerError("usage unavailable")
            return {
                "summary": {"lifetimeTokens": 1200, "peakDailyTokens": 400, "currentStreakDays": 2},
                "dailyUsageBuckets": [{"startDate": "2026-08-10", "tokens": 12}],
                "refreshToken": "must-not-return",
            }
        if method == "account/login/start":
            return {"loginId": "login-1", "authUrl": "https://auth.openai.com/example"}
        if method in {"account/login/cancel", "account/logout"}:
            return {}
        if method == "account/rateLimitResetCredit/consume":
            assert params["idempotencyKey"] == "reset-0001"
            return {"outcome": "reset"}
        raise AssertionError(method)

    def wait_for_notification(self, methods: set[str], **_: Any) -> dict[str, Any] | None:
        return self.notifications.pop(0) if self.notifications else None


def test_quota_adapter_normalizes_current_schema_without_tokens() -> None:
    transport = FakeTransport()
    snapshot = QuotaAdapter(transport).read_snapshot(profile_id="profile-1")

    assert snapshot["status"] == "available"
    assert snapshot["summary"]["plan_type"] == "plus"
    assert snapshot["summary"]["available_reset_credits"] == 3
    assert snapshot["reset_credits"]["details_state"] == "partial"
    assert snapshot["reset_credits"]["returned_detail_count"] == 1
    assert snapshot["reset_credits"]["missing_detail_count"] == 2
    assert snapshot["reset_credits"]["details"][0]["expires_at"].endswith("+00:00")
    assert len(snapshot["limits"]) == 2
    assert snapshot["limits"][0]["windows"][0]["used_percent"] == 0.0
    assert snapshot["limits"][0]["windows"][0]["remaining_percent"] == 100.0
    assert snapshot["limits"][0]["windows"][0]["resets_at"].endswith("+00:00")
    assert snapshot["usage"]["daily_usage_buckets"][0] == {"start_date": "2026-08-10", "tokens": 12}
    assert "must-not-return" not in str(snapshot)


def test_account_name_is_preferred_over_email_for_display() -> None:
    transport = FakeTransport(account={
        "type": "chatgpt",
        "email": "chris@example.com",
        "name": "QA RIFO",
        "planType": "pro",
    })

    snapshot = QuotaAdapter(transport).read_account_section()

    assert snapshot["account"]["name"] == "QA RIFO"
    assert snapshot["account"]["email_masked"] == "ch***@example.com"
    assert snapshot["account"]["account_identifier"] == "qa****"
    assert snapshot["summary"]["account_label"] == "QA RIFO"
    assert snapshot["summary"]["account_identifier"] == "qa****"


def test_account_identifier_is_not_invented_from_email_only() -> None:
    snapshot = QuotaAdapter(FakeTransport(account={
        "type": "chatgpt",
        "email": "chris@example.com",
        "planType": "pro",
    })).read_account_section()

    assert snapshot["account"]["name"] is None
    assert snapshot["account"].get("account_identifier") is None
    assert snapshot["summary"].get("account_identifier") is None


def test_subscription_sections_read_only_their_own_app_server_resource() -> None:
    account_transport = FakeTransport()
    account = QuotaAdapter(account_transport).read_account_section()
    assert account["status"] == "available"
    assert account_transport.methods == ["account/read"]
    assert "account/usage/read" not in str(account)

    limits_transport = FakeTransport()
    limits = QuotaAdapter(limits_transport).read_limits_section()
    assert limits["status"] == "available"
    assert limits_transport.methods == ["account/rateLimits/read"]
    assert limits["windows"]

    usage_transport = FakeTransport()
    usage = QuotaAdapter(usage_transport).read_usage_section()
    assert usage["status"] == "available"
    assert usage_transport.methods == ["account/usage/read"]
    assert usage["usage"]["daily_usage_buckets"]


def test_reset_credit_contract_states() -> None:
    complete = QuotaAdapter(
        FakeTransport(reset_summary={
            "availableCount": 1,
            "credits": [{"id": "private", "status": "available", "expiresAt": 1786989600}],
        })
    ).read_snapshot()["reset_credits"]
    assert complete["details_state"] == "complete"
    assert complete["returned_detail_count"] == 1

    count_only = QuotaAdapter(
        FakeTransport(reset_summary={"availableCount": 2, "credits": None})
    ).read_snapshot()["reset_credits"]
    assert count_only["details_state"] == "count_only"
    assert count_only["missing_detail_count"] == 2

    empty = QuotaAdapter(
        FakeTransport(reset_summary={"availableCount": 0, "credits": []})
    ).read_snapshot()["reset_credits"]
    assert empty["details_state"] == "complete"
    assert empty["returned_detail_count"] == 0

    lower_bound = QuotaAdapter(
        FakeTransport(reset_summary={"credits": [{"status": "available"}]})
    ).read_snapshot()["reset_credits"]
    assert lower_bound["available_count"] is None
    assert lower_bound["count_source"] == "detail_lower_bound"
    assert lower_bound["detail_lower_bound"] == 1
    assert lower_bound["details_state"] == "partial"


def test_usage_failure_does_not_hide_reset_credit_details() -> None:
    snapshot = QuotaAdapter(FakeTransport(usage_error=True)).read_snapshot()

    assert snapshot["status"] == "available"
    assert snapshot["reset_credits"]["available_count"] == 3
    assert snapshot["reset_credits"]["returned_detail_count"] == 1
    assert snapshot["usage"] == {"summary": {}, "daily_usage_buckets": []}


def test_limits_failure_preserves_account_identity() -> None:
    class LimitsUnavailableTransport(FakeTransport):
        def request(self, method: str, params: Any = None) -> dict[str, Any]:
            if method == "account/rateLimits/read":
                raise AppServerError("limits unavailable")
            return super().request(method, params)

    snapshot = QuotaAdapter(LimitsUnavailableTransport()).read_snapshot()

    assert snapshot["status"] == "available"
    assert snapshot["account"]["email"] == "owner@example.com"
    assert snapshot["summary"]["account_state"] == "connected"
    assert snapshot["section_errors"] == {"limits": "limits unavailable"}


def test_account_failure_with_local_identity_preserves_connected_state() -> None:
    class AccountUnavailableTransport(FakeTransport):
        def request(self, method: str, params: Any = None) -> dict[str, Any]:
            if method == "account/read":
                raise AppServerError("account unavailable")
            return super().request(method, params)

    identity = {
        "state": "connected_local_codex",
        "account_type": "chatgpt",
        "email": "local@example.com",
        "email_masked": "lo***@example.com",
        "fingerprint": "local-safe",
        "name": "QA RIFO",
        "plan_type": "pro",
        "identity_source": "local_codex_id_token",
    }
    snapshot = QuotaAdapter(AccountUnavailableTransport(), identity_reader=lambda: identity).read_snapshot()

    assert snapshot["status"] == "available"
    assert snapshot["account"]["state"] == "connected_local_codex"
    assert snapshot["account"]["name"] == "QA RIFO"
    assert snapshot["summary"]["account_state"] == "connected_local_codex"
    assert snapshot["section_errors"] == {"account": "account unavailable"}


def test_identity_can_be_unavailable_while_limits_are_present() -> None:
    snapshot = QuotaAdapter(FakeTransport(account={})).read_snapshot()
    assert snapshot["summary"]["account_state"] == "connected"

    snapshot = QuotaAdapter(FakeTransport(account=None)).read_snapshot()
    assert snapshot["summary"]["account_state"] == "connected"


class IdentityUnavailableTransport(FakeTransport):
    def request(self, method: str, params: Any = None) -> dict[str, Any]:
        if method == "account/read":
            return {"account": None, "requiresOpenaiAuth": False}
        return super().request(method, params)


def test_null_account_with_limits_is_identity_unavailable() -> None:
    snapshot = QuotaAdapter(IdentityUnavailableTransport(), identity_reader=lambda: None).read_snapshot()
    assert snapshot["summary"]["account_state"] == "identity_unavailable"
    assert snapshot["windows"]


def test_local_codex_identity_fills_account_when_app_server_has_no_identity() -> None:
    identity = {
        "state": "connected_local_codex",
        "account_type": "chatgpt",
        "email": "owner@example.com",
        "email_masked": "ow***@example.com",
        "fingerprint": "safe",
        "name": "Owner",
        "plan_type": "pro",
        "email_verified": True,
        "identity_source": "local_codex_id_token",
        "identity_expires_at": "2026-08-11T00:00:00+00:00",
    }
    snapshot = QuotaAdapter(IdentityUnavailableTransport(), identity_reader=lambda: identity).read_snapshot()

    assert snapshot["account"]["email"] == "owner@example.com"
    assert snapshot["account"]["name"] == "Owner"
    assert snapshot["summary"]["account_state"] == "connected_local_codex"
    assert snapshot["reset_credits"]["available_count"] == 3


def test_login_flow_uses_persistent_notification_session() -> None:
    transport = FakeTransport()
    adapter = QuotaAdapter(transport)
    started = adapter.start_login()
    assert started == {"status": "pending", "login_id": "login-1", "auth_url": "https://auth.openai.com/example"}
    assert adapter.login_status("login-1")["status"] == "pending"
    transport.notifications.append({"method": "account/login/completed", "params": {"loginId": "login-1", "success": True}})
    assert adapter.login_status("login-1")["status"] == "completed"


def test_quota_adapter_consumes_only_explicit_reset_request() -> None:
    adapter = QuotaAdapter(FakeTransport())
    assert adapter.consume_reset(idempotency_key="reset-0001")["outcome"] == "reset"
