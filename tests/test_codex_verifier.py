from __future__ import annotations

from codexthrift.codex_verifier import CodexModeVerifier
from codexthrift.paths import WorkspacePaths


class _Transport:
    pid = 99

    def __init__(self, responses):
        self.responses = responses
        self.closed = False

    def request(self, method, params=None):
        return self.responses[method]

    def close(self):
        self.closed = True


def test_verifier_reports_native_auth_required(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    paths = WorkspacePaths(tmp_path / "router-home")
    transport = _Transport(
        {
            "config/read": {"config": {"modelProvider": "openai"}},
            "account/read": {"account": None, "requiresOpenaiAuth": True},
        }
    )
    verifier = CodexModeVerifier(paths, transport_factory=lambda env: transport)

    result = verifier.verify("openai")

    assert result["verified"] is True
    assert result["status"] == "auth_required"
    assert transport.closed is True


def test_verifier_reports_provider_drift(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    paths = WorkspacePaths(tmp_path / "router-home")
    transport = _Transport(
        {
            "config/read": {"config": {"model_provider": "litellm", "model": "codex-auto"}},
            "account/read": {"account": {"type": "chatgpt", "planType": "pro"}},
        }
    )

    result = CodexModeVerifier(paths, transport_factory=lambda env: transport).verify("openai")

    assert result["verified"] is False
    assert result["status"] == "config_drift"
