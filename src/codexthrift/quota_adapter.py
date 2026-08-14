from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any, Protocol

from .app_server_session import AppServerError, CodexAppServerSession
from .codex_identity import CodexIdentityError, read_local_codex_identity


class AppServerTransport(Protocol):
    def request(self, method: str, params: Any = None) -> dict[str, Any]: ...


class QuotaAdapter:
    """Normalize current App Server account data without retaining credentials."""

    def __init__(self, transport: AppServerTransport | None = None, identity_reader: Any = read_local_codex_identity):
        self.transport = transport or CodexAppServerSession()
        self.identity_reader = identity_reader

    def close(self) -> None:
        close = getattr(self.transport, "close", None)
        if callable(close):
            close()

    def read_snapshot(self, *, profile_id: str | None = None) -> dict[str, Any]:
        observed_at = datetime.now(UTC).isoformat()
        account_section = self.read_account_section(profile_id=profile_id, observed_at=observed_at)
        limits_section = self.read_limits_section(profile_id=profile_id, observed_at=observed_at)
        usage_section = self.read_usage_section(profile_id=profile_id, observed_at=observed_at)

        safe_account = account_section["account"]
        summary = {
            **account_section["summary"],
            **limits_section["summary"],
            "usage": usage_section["usage"],
        }
        status = account_section["summary"].get("account_state") or "identity_unavailable"
        section_errors = {
            section: result["error_message"]
            for section, result in (
                ("account", account_section),
                ("limits", limits_section),
                ("usage", usage_section),
            )
            if result["status"] != "available" and result.get("error_message")
        }
        account_available = status in {"connected", "connected_local_codex", "api_key_only"}
        any_section_available = any(
            result["status"] == "available"
            for result in (account_section, limits_section, usage_section)
        )
        snapshot = {
            "profile_id": profile_id or safe_account.get("fingerprint"),
            "observed_at": observed_at,
            "status": "available" if account_available or any_section_available else "unavailable",
            "account": safe_account,
            "summary": summary,
            "limits": limits_section["limits"],
            "windows": limits_section["windows"],
            "usage": usage_section["usage"],
            "reset_credits": limits_section["reset_credits"],
        }
        if section_errors:
            snapshot["section_errors"] = section_errors
            snapshot["error_code"] = "partial_quota_read"
            snapshot["error_message"] = "; ".join(f"{section}: {message}" for section, message in section_errors.items())
        return snapshot

    def read_account_section(self, *, profile_id: str | None = None, observed_at: str | None = None) -> dict[str, Any]:
        observed_at = observed_at or datetime.now(UTC).isoformat()
        result = self.read_account()
        response = result["response"]
        account = response.get("account")
        requires_auth = response.get("requiresOpenaiAuth") is True
        safe_account = _safe_account(account, requires_auth=requires_auth)
        if safe_account.get("state") == "identity_unavailable":
            safe_account = self._local_identity_or_unavailable(safe_account)
        else:
            safe_account = self._enrich_account_from_local_identity(safe_account)
        account_identifier = _mask_account_identifier(safe_account.get("name"))
        if account_identifier:
            safe_account = {**safe_account, "account_identifier": account_identifier}
        status = safe_account.get("state") or _account_status(account, requires_auth=requires_auth, has_limits=False)
        return {
            "profile_id": profile_id or safe_account.get("fingerprint"),
            "observed_at": observed_at,
            "status": "available" if result["ok"] else "unavailable",
            "error_message": result["error"],
            "account": safe_account,
            "summary": {
                "plan_type": safe_account.get("plan_type"),
                "auth_mode": safe_account.get("account_type"),
                "account_label": safe_account.get("name") or safe_account.get("email_masked") or safe_account.get("email"),
                "account_identifier": account_identifier,
                "account_state": status,
                "requires_openai_auth": requires_auth,
            },
        }

    def read_limits_section(self, *, profile_id: str | None = None, observed_at: str | None = None) -> dict[str, Any]:
        observed_at = observed_at or datetime.now(UTC).isoformat()
        result = self.read_limits()
        response = result["response"]
        limits = _normalize_limits(response)
        return {
            "profile_id": profile_id,
            "observed_at": observed_at,
            "status": "available" if result["ok"] else "unavailable",
            "error_message": result["error"],
            "summary": {
                "plan_type": _plan_type(response),
                "available_reset_credits": _normalize_reset_credits(response, observed_at=observed_at)["available_count"],
                "credits": _safe_credits(response),
            },
            "limits": limits,
            "windows": _compatibility_windows(limits),
            "reset_credits": _normalize_reset_credits(response, observed_at=observed_at),
        }

    def read_usage_section(self, *, profile_id: str | None = None, observed_at: str | None = None) -> dict[str, Any]:
        observed_at = observed_at or datetime.now(UTC).isoformat()
        result = self.read_usage()
        return {
            "profile_id": profile_id,
            "observed_at": observed_at,
            "status": "available" if result["ok"] else "unavailable",
            "error_message": result["error"],
            "usage": _safe_usage(result["response"]),
        }

    def read_account(self) -> dict[str, Any]:
        """Read only identity/account fields for an independently loading UI section."""
        try:
            response = self.transport.request("account/read", {"refreshToken": True})
        except AppServerError as exc:
            response = {"account": None, "requiresOpenaiAuth": False}
            return {"ok": False, "response": response, "error": str(exc)}
        return {"ok": True, "response": response, "error": None}

    def read_limits(self) -> dict[str, Any]:
        """Read only rate-limit and reset-credit fields for the quota section."""
        try:
            response = self.transport.request("account/rateLimits/read", {})
        except AppServerError as exc:
            return {"ok": False, "response": {}, "error": str(exc)}
        return {"ok": True, "response": response, "error": None}

    def read_usage(self) -> dict[str, Any]:
        """Read only historical token usage for the activity section."""
        try:
            response = self.transport.request("account/usage/read", {})
        except AppServerError as exc:
            return {"ok": False, "response": {}, "error": str(exc)}
        return {"ok": True, "response": response, "error": None}

    def has_account_update(self) -> bool:
        wait = getattr(self.transport, "wait_for_notification", None)
        if not callable(wait):
            return False
        return wait({"account/rateLimits/updated", "account/updated"}, timeout_seconds=0.0) is not None

    def _local_identity_or_unavailable(self, unavailable: dict[str, Any]) -> dict[str, Any]:
        try:
            identity = self.identity_reader()
        except CodexIdentityError as exc:
            return {**unavailable, "identity_error_code": "local_identity_unavailable", "identity_error_message": str(exc)}
        return identity if isinstance(identity, dict) else unavailable

    def _enrich_account_from_local_identity(self, account: dict[str, Any]) -> dict[str, Any]:
        if account.get("name") and account.get("plan_type"):
            return account
        try:
            identity = self.identity_reader()
        except CodexIdentityError:
            return account
        if not isinstance(identity, dict) or identity.get("state") not in {
            "connected",
            "connected_local_codex",
        }:
            return account
        account_email = account.get("email")
        identity_email = identity.get("email")
        if (
            isinstance(account_email, str)
            and isinstance(identity_email, str)
            and account_email.casefold() != identity_email.casefold()
        ):
            return account
        enriched = dict(account)
        for key in ("name", "plan_type", "email_verified", "identity_expires_at", "account_id_fingerprint"):
            if not enriched.get(key) and identity.get(key):
                enriched[key] = identity[key]
        return enriched

    def start_login(self) -> dict[str, Any]:
        try:
            result = self.transport.request(
                "account/login/start",
                {"type": "chatgpt", "useHostedLoginSuccessPage": True, "appBrand": "chatgpt"},
            )
        except AppServerError as exc:
            return {"status": "error", "error_code": "app_server_unavailable", "error_message": str(exc)}
        login_id = result.get("loginId")
        auth_url = result.get("authUrl")
        if not isinstance(login_id, str) or not isinstance(auth_url, str):
            return {"status": "error", "error_code": "invalid_login_response", "error_message": "Codex 未返回登录链接。"}
        return {"status": "pending", "login_id": login_id, "auth_url": auth_url}

    def login_status(self, login_id: str) -> dict[str, Any]:
        wait = getattr(self.transport, "wait_for_notification", None)
        if not callable(wait):
            return {"status": "pending", "login_id": login_id}

        def matches(message: dict[str, Any]) -> bool:
            params = message.get("params")
            return not isinstance(params, dict) or params.get("loginId") in (None, login_id)

        message = wait({"account/login/completed"}, timeout_seconds=0.0, predicate=matches)
        if not message:
            return {"status": "pending", "login_id": login_id}
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        success = params.get("success")
        if success is False or params.get("error"):
            return {"status": "error", "login_id": login_id, "error_code": "login_failed", "error_message": "ChatGPT 登录未完成。"}
        return {"status": "completed", "login_id": login_id}

    def cancel_login(self, login_id: str) -> dict[str, Any]:
        try:
            self.transport.request("account/login/cancel", {"loginId": login_id})
        except AppServerError as exc:
            return {"status": "error", "error_code": "app_server_unavailable", "error_message": str(exc)}
        return {"status": "cancelled", "login_id": login_id}

    def logout(self) -> dict[str, Any]:
        try:
            self.transport.request("account/logout", {})
        except AppServerError as exc:
            return {"status": "error", "error_code": "app_server_unavailable", "error_message": str(exc)}
        return {"status": "logged_out"}

    def consume_reset(self, *, idempotency_key: str, credit_id: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"idempotencyKey": idempotency_key}
        if credit_id:
            params["creditId"] = credit_id
        try:
            result = self.transport.request("account/rateLimitResetCredit/consume", params)
        except AppServerError as exc:
            return {"outcome": "error", "error_code": "app_server_unavailable", "error_message": str(exc)}
        return {"outcome": str(result.get("outcome") or "unknown")}


def _unavailable_snapshot(profile_id: str | None, observed_at: str, message: str) -> dict[str, Any]:
    return {
        "profile_id": profile_id,
        "observed_at": observed_at,
        "status": "unavailable",
        "error_code": "app_server_unavailable",
        "error_message": message,
        "account": {"state": "temporarily_unavailable"},
        "summary": {"account_state": "temporarily_unavailable"},
        "limits": [],
        "windows": [],
        "usage": {"summary": {}, "daily_usage_buckets": []},
        "reset_credits": {
            "available_count": None,
            "count_source": "unavailable",
            "detail_lower_bound": 0,
            "details_state": "unavailable",
            "returned_detail_count": 0,
            "missing_detail_count": None,
            "details": [],
            "observed_at": observed_at,
            "source": "unavailable",
            "contract_warning": None,
        },
    }


def _safe_account(account: Any, *, requires_auth: bool) -> dict[str, Any]:
    if not isinstance(account, dict):
        return {"state": "login_required" if requires_auth else "identity_unavailable"}
    email = account.get("email") if isinstance(account.get("email"), str) else None
    account_type = account.get("type") if isinstance(account.get("type"), str) else None
    account_id = _first_string(account, "accountId", "account_id", "userId", "user_id", "id")
    result = {
        "state": "api_key_only" if account_type == "apiKey" else "connected",
        "account_type": account_type,
        "email": email,
        "email_masked": _mask_email(email),
        "fingerprint": hashlib.sha256(email.lower().encode()).hexdigest()[:24] if email else None,
        "account_id_fingerprint": _fingerprint(account_id) if account_id else None,
        "name": _account_name(account),
        "plan_type": account.get("planType") if isinstance(account.get("planType"), str) else None,
        "email_verified": None,
        "identity_source": "app_server",
    }
    return result


def _account_status(account: Any, *, requires_auth: bool, has_limits: bool) -> str:
    if isinstance(account, dict):
        return "api_key_only" if account.get("type") == "apiKey" else "connected"
    if requires_auth and not has_limits:
        return "login_required"
    return "identity_unavailable"


def _mask_email(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    local, domain = email.split("@", 1)
    visible = local[:2] if len(local) > 2 else local[:1]
    return f"{visible}{'*' * max(2, min(6, len(local) - len(visible)))}@{domain}"


def _account_name(account: dict[str, Any]) -> str | None:
    for key in ("name", "displayName", "display_name"):
        value = account.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _mask_account_identifier(name: Any) -> str | None:
    if not isinstance(name, str) or not name.strip():
        return None
    value = name.strip()
    letters = "".join(character for character in value if character.isalnum())
    if not letters:
        return None
    return f"{letters[:2].lower()}****"


def _first_string(value: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.lower().encode("utf-8")).hexdigest()[:24]


def _plan_type(rate_limits: dict[str, Any]) -> str | None:
    root = rate_limits.get("rateLimits")
    return root.get("planType") if isinstance(root, dict) and isinstance(root.get("planType"), str) else None


def _safe_usage(usage: dict[str, Any]) -> dict[str, Any]:
    summary = usage.get("summary") if isinstance(usage.get("summary"), dict) else {}
    allowed_summary = {
        key: summary[key]
        for key in ("lifetimeTokens", "peakDailyTokens", "currentStreakDays", "longestStreakDays", "longestRunningTurnSec")
        if _number(summary.get(key)) is not None
    }
    buckets = []
    raw_buckets = usage.get("dailyUsageBuckets")
    if isinstance(raw_buckets, list):
        for bucket in raw_buckets:
            if not isinstance(bucket, dict) or not isinstance(bucket.get("startDate"), str):
                continue
            tokens = _number(bucket.get("tokens"))
            buckets.append({"start_date": bucket["startDate"], "tokens": int(tokens) if tokens is not None else 0})
    return {"summary": allowed_summary, "daily_usage_buckets": buckets}


def _normalize_limits(response: dict[str, Any]) -> list[dict[str, Any]]:
    by_id = response.get("rateLimitsByLimitId")
    roots: list[tuple[str, dict[str, Any]]] = []
    if isinstance(by_id, dict):
        roots.extend((str(limit_id), value) for limit_id, value in by_id.items() if isinstance(value, dict))
    if not roots and isinstance(response.get("rateLimits"), dict):
        root = response["rateLimits"]
        roots.append((str(root.get("limitId") or "codex"), root))

    limits = []
    for limit_id, root in roots:
        windows = []
        for kind in ("primary", "secondary", "individualLimit"):
            window = root.get(kind)
            if not isinstance(window, dict):
                continue
            used = _number(_first(window, "usedPercent", "used_percent"))
            remaining = _number(_first(window, "remainingPercent", "remaining_percent"))
            if remaining is None and used is not None:
                remaining = max(0.0, min(100.0, 100.0 - used))
            windows.append(
                {
                    "kind": "individual" if kind == "individualLimit" else kind,
                    "used_percent": used,
                    "remaining_percent": remaining,
                    "window_duration_mins": _integer(_first(window, "windowDurationMins", "window_duration_mins")),
                    "resets_at": _iso_timestamp(_first(window, "resetsAt", "resetAt", "reset_at")),
                }
            )
        limits.append(
            {
                "limit_id": limit_id,
                "limit_name": root.get("limitName") if isinstance(root.get("limitName"), str) else None,
                "plan_type": root.get("planType") if isinstance(root.get("planType"), str) else None,
                "rate_limit_reached_type": root.get("rateLimitReachedType") if isinstance(root.get("rateLimitReachedType"), str) else None,
                "credits": _credits(root.get("credits")),
                "windows": windows,
            }
        )
    return limits


def _safe_credits(response: dict[str, Any]) -> dict[str, Any] | None:
    root = response.get("rateLimits")
    return _credits(root.get("credits")) if isinstance(root, dict) else None


def _credits(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    balance = value.get("balance")
    return {
        "has_credits": value.get("hasCredits") if isinstance(value.get("hasCredits"), bool) else None,
        "unlimited": value.get("unlimited") if isinstance(value.get("unlimited"), bool) else None,
        "balance": str(balance) if isinstance(balance, (str, int, float)) and not isinstance(balance, bool) else None,
    }


def _compatibility_windows(limits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    windows = []
    for limit in limits:
        for window in limit["windows"]:
            kind = window["kind"]
            windows.append(
                {
                    "key": f"{limit['limit_id']}:{kind}",
                    "label": f"{limit.get('limit_name') or limit['limit_id']} · {kind}",
                    "limit_id": limit["limit_id"],
                    "limit_name": limit.get("limit_name"),
                    "window_kind": kind,
                    "window_duration_mins": window.get("window_duration_mins"),
                    "remaining_percent": window.get("remaining_percent"),
                    "used_percent": window.get("used_percent"),
                    "reset_at": window.get("resets_at"),
                }
            )
    return windows


def _available_reset_credits(rate_limits: dict[str, Any]) -> int | None:
    credits = rate_limits.get("rateLimitResetCredits")
    if not isinstance(credits, dict):
        return None
    count = credits.get("availableCount")
    return count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else None


def _normalize_reset_credits(rate_limits: dict[str, Any], *, observed_at: str) -> dict[str, Any]:
    root = rate_limits.get("rateLimitResetCredits")
    if not isinstance(root, dict):
        return {
            "available_count": None,
            "count_source": "unavailable",
            "detail_lower_bound": 0,
            "details_state": "unavailable",
            "returned_detail_count": 0,
            "missing_detail_count": None,
            "details": [],
            "observed_at": observed_at,
            "source": "account/rateLimits/read",
            "contract_warning": None,
        }

    available_count = _available_reset_credits(rate_limits)
    raw_details = root.get("credits")
    details: list[dict[str, Any]] = []
    warnings: list[str] = []

    if isinstance(raw_details, list):
        for item in raw_details:
            if not isinstance(item, dict):
                warnings.append("ignored_non_object_detail")
                continue
            details.append(
                {
                    "status": _text(item.get("status"), 80),
                    "reset_type": _text(item.get("resetType"), 120),
                    "granted_at": _iso_timestamp(item.get("grantedAt")),
                    "expires_at": _iso_timestamp(item.get("expiresAt")),
                    "title": _text(item.get("title"), 300),
                    "description": _text(item.get("description"), 1000),
                }
            )
    elif raw_details is not None:
        warnings.append("invalid_details_type")

    returned_count = len(details)
    if available_count is None:
        count_source = "detail_lower_bound" if isinstance(raw_details, list) else "unavailable"
        details_state = "partial" if isinstance(raw_details, list) else "unavailable"
        missing_count = None
        if isinstance(raw_details, list):
            warnings.append("available_count_missing")
    else:
        count_source = "official_available_count"
        missing_count = max(0, available_count - returned_count)
        if raw_details is None:
            details_state = "count_only"
        elif not isinstance(raw_details, list):
            details_state = "count_only"
        elif returned_count == available_count:
            details_state = "complete"
        else:
            details_state = "partial"
            if returned_count > available_count:
                warnings.append("detail_count_exceeds_available_count")

    return {
        "available_count": available_count,
        "count_source": count_source,
        "detail_lower_bound": returned_count,
        "details_state": details_state,
        "returned_detail_count": returned_count,
        "missing_detail_count": missing_count,
        "details": details,
        "observed_at": observed_at,
        "source": "account/rateLimits/read",
        "contract_warning": ",".join(dict.fromkeys(warnings)) or None,
    }


def _iso_timestamp(value: Any) -> str | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, UTC).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return value if isinstance(value, str) and value else None


def _text(value: Any, max_length: int) -> str | None:
    return value[:max_length] if isinstance(value, str) and value else None


def _first(value: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in value:
            return value[key]
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None
