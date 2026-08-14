from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_AUTH_FILE_BYTES = 1024 * 1024
OPENAI_AUTH_CLAIM = "https://api.openai.com/auth"


class CodexIdentityError(RuntimeError):
    """A safe, non-sensitive local Codex identity lookup error."""


def read_local_codex_identity() -> dict[str, Any] | None:
    """Read display-only claims from the current user's protected Codex ID token."""
    auth_path = _codex_auth_path()
    try:
        file_stat = auth_path.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        raise CodexIdentityError("本机 Codex 凭据文件类型不安全。")
    if (hasattr(os, "getuid") and file_stat.st_uid != os.getuid()) or file_stat.st_mode & 0o077:
        raise CodexIdentityError("本机 Codex 凭据文件权限不安全。")
    if file_stat.st_size > MAX_AUTH_FILE_BYTES:
        raise CodexIdentityError("本机 Codex 凭据文件异常。")

    try:
        document = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CodexIdentityError("无法读取本机 Codex 身份状态。") from exc
    tokens = document.get("tokens")
    token = tokens.get("id_token") if isinstance(tokens, dict) else None
    if not isinstance(token, str):
        return None
    claims = _jwt_payload(token)
    email = claims.get("email")
    if not isinstance(email, str) or not email or claims.get("email_verified") is not True:
        return None
    expires_at = _expires_at(claims.get("exp"))
    if expires_at is None or expires_at <= datetime.now(UTC):
        raise CodexIdentityError("本机 Codex 身份已过期。")
    issuer = claims.get("iss")
    if not isinstance(issuer, str) or not issuer.startswith("https://auth.openai.com"):
        raise CodexIdentityError("本机 Codex 身份签发方不受支持。")

    auth_claim = claims.get(OPENAI_AUTH_CLAIM)
    auth_claim = auth_claim if isinstance(auth_claim, dict) else {}
    account_id = auth_claim.get("chatgpt_account_id") or tokens.get("account_id")
    plan_type = auth_claim.get("chatgpt_plan_type")
    return {
        "state": "connected_local_codex",
        "account_type": "chatgpt",
        "email": email,
        "email_masked": _mask_email(email),
        "fingerprint": _fingerprint(email),
        "account_id_fingerprint": _fingerprint(account_id) if isinstance(account_id, str) else None,
        "name": claims.get("name") if isinstance(claims.get("name"), str) else None,
        "plan_type": plan_type if isinstance(plan_type, str) else None,
        "email_verified": True,
        "identity_source": "local_codex_id_token",
        "identity_expires_at": expires_at.isoformat(),
    }


def _codex_auth_path() -> Path:
    home = Path(os.environ["CODEX_HOME"]).expanduser() if os.environ.get("CODEX_HOME") else Path.home() / ".codex"
    return home / "auth.json"


def _jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise CodexIdentityError("本机 Codex 身份格式无效。")
    try:
        payload = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        claims = json.loads(payload)
    except (ValueError, json.JSONDecodeError) as exc:
        raise CodexIdentityError("本机 Codex 身份格式无效。") from exc
    if not isinstance(claims, dict):
        raise CodexIdentityError("本机 Codex 身份格式无效。")
    return claims


def _expires_at(value: Any) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, UTC)
    except (OSError, OverflowError, ValueError):
        return None


def _mask_email(email: str) -> str:
    local, domain = email.split("@", 1)
    visible = local[:2] if len(local) > 2 else local[:1]
    return f"{visible}{'*' * max(2, min(6, len(local) - len(visible)))}@{domain}"


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.lower().encode("utf-8")).hexdigest()[:24]
