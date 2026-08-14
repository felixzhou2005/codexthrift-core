from __future__ import annotations

from pathlib import Path

from codexthrift.dashboard import _token_from_file


def test_token_file_argument_takes_precedence_over_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    environment_file = tmp_path / "environment-token"
    explicit_file = tmp_path / "explicit-token"
    environment_file.write_text("environment-secret\n", encoding="utf-8")
    explicit_file.write_text("explicit-secret\n", encoding="utf-8")
    monkeypatch.setenv("ROUTERLLM_DASHBOARD_TOKEN_FILE", str(environment_file))

    assert _token_from_file(explicit_file) == "explicit-secret"


def test_token_file_returns_none_for_missing_or_empty_file(tmp_path: Path) -> None:
    empty_file = tmp_path / "empty-token"
    empty_file.write_text("\n", encoding="utf-8")

    assert _token_from_file(empty_file) is None
    assert _token_from_file(tmp_path / "missing-token") is None
