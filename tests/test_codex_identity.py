from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from codexthrift.codex_identity import CodexIdentityError, read_local_codex_identity


def _token(payload: dict[str, object]) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


def _write_auth(path: Path, payload: dict[str, object], *, mode: int = 0o600) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"tokens": {"id_token": _token(payload), "account_id": "private-account-id"}}), encoding="utf-8")
    path.chmod(mode)


def _claims() -> dict[str, object]:
    return {
        "email": "owner@example.com",
        "email_verified": True,
        "name": "Owner Name",
        "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        "iss": "https://auth.openai.com",
        "https://api.openai.com/auth": {"chatgpt_account_id": "private-account-id", "chatgpt_plan_type": "pro"},
    }


def test_local_identity_reads_only_display_claims(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    _write_auth(tmp_path / "codex" / "auth.json", _claims())

    identity = read_local_codex_identity()

    assert identity and identity["email"] == "owner@example.com"
    assert identity["name"] == "Owner Name"
    assert identity["plan_type"] == "pro"
    assert identity["identity_source"] == "local_codex_id_token"
    assert "private-account-id" not in str(identity)
    assert "header." not in str(identity)


def test_local_identity_rejects_expired_or_insecure_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    auth_path = tmp_path / "codex" / "auth.json"
    expired = _claims() | {"exp": int((datetime.now(UTC) - timedelta(hours=1)).timestamp())}
    _write_auth(auth_path, expired)

    with pytest.raises(CodexIdentityError, match="过期"):
        read_local_codex_identity()

    _write_auth(auth_path, _claims(), mode=0o644)
    with pytest.raises(CodexIdentityError, match="权限"):
        read_local_codex_identity()
