from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any


def redact_secret(text: str, secrets: Iterable[str]) -> str:
    result = text
    for secret in secrets:
        if secret:
            result = result.replace(secret, "***REDACTED***")
    return result


def redact_sensitive_text(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Best-effort masking for common credential shapes in human-readable logs."""
    result = redact_secret(text, extra_secrets)
    patterns = [
        (
            re.compile(
                r'(?i)(["\']?(?:access_token|refresh_token|id_token|client_secret|api_key)'
                r'["\']?\s*[:=]\s*["\']?)([^"\'\s,}]+)'
            ),
            r'\1***REDACTED***',
        ),
        (re.compile(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}'), 'Bearer ***REDACTED***'),
        (re.compile(r'\bsk-[A-Za-z0-9_-]{16,}'), 'sk-***REDACTED***'),
        (re.compile(r'(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])'), '***REDACTED_EMAIL***'),
        (re.compile(r'(?<!\w)(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}(?!\w)'), '***REDACTED_PHONE***'),
    ]
    for pattern, replacement in patterns:
        result = pattern.sub(replacement, result)
    return result


def redact_sensitive_value(value: Any, extra_secrets: Iterable[str] = ()) -> Any:
    """Recursively mask credentials before structured status leaves the controller."""
    secrets = tuple(extra_secrets)
    if isinstance(value, str):
        return redact_sensitive_text(value, secrets)
    if isinstance(value, dict):
        sensitive_keys = {
            "access_token",
            "refresh_token",
            "id_token",
            "client_secret",
            "api_key",
            "master_key",
            "authorization",
            "password",
            "cookie",
            "set_cookie",
            "email",
            "phone",
            "telephone",
            "address",
            "street_address",
            "postal_address",
        }
        return {
            key: (
                "***REDACTED***"
                if str(key).lower() in sensitive_keys and item not in (None, "")
                else redact_sensitive_value(item, secrets)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive_value(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive_value(item, secrets) for item in value)
    return value
