from __future__ import annotations

import tomlkit

import codexthrift.codex_config as codex_config_module
from codexthrift.codex_config import configure_codex, switch_to_native_codex
from codexthrift.paths import WorkspacePaths


def _paths(tmp_path, monkeypatch) -> WorkspacePaths:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    paths = WorkspacePaths(tmp_path / "router-home")
    paths.ensure()
    paths.codex_config.parent.mkdir(parents=True, exist_ok=True)
    return paths


def test_round_trip_restores_native_fields_and_preserves_unrelated_config(tmp_path, monkeypatch) -> None:
    paths = _paths(tmp_path, monkeypatch)
    monkeypatch.setattr(codex_config_module.sys, "frozen", True, raising=False)
    paths.codex_config.write_text(
        '''
model = "gpt-5.6-terra"
model_reasoning_effort = "medium"
sandbox_mode = "workspace-write"

[mcp_servers.example]
command = "example"
''',
        encoding="utf-8",
    )

    configure_codex(paths, "phase3a", "127.0.0.1", 4000)
    smart = tomlkit.parse(paths.codex_config.read_text(encoding="utf-8"))
    assert smart["model"] == "codex-auto"
    assert smart["model_provider"] == "openai"
    assert smart["openai_base_url"] == "http://127.0.0.1:4877/v1"
    assert "model_providers" not in smart

    switch_to_native_codex(paths, "127.0.0.1", 4000)
    native = tomlkit.parse(paths.codex_config.read_text(encoding="utf-8"))

    assert native["model"] == "gpt-5.6-terra"
    assert native["model_provider"] == "openai"
    assert native["model_reasoning_effort"] == "medium"
    assert native["sandbox_mode"] == "workspace-write"
    assert native["mcp_servers"]["example"]["command"] == "example"
    assert "openai_base_url" not in native
    assert "model_providers" not in native


def test_native_recovery_is_idempotent_and_keeps_post_switch_unrelated_changes(
    tmp_path,
    monkeypatch,
) -> None:
    paths = _paths(tmp_path, monkeypatch)
    paths.codex_config.write_text(
        'model = "gpt-5.6-terra"\nmodel_provider = "openai"\nsandbox_mode = "workspace-write"\n',
        encoding="utf-8",
    )
    configure_codex(paths, "phase3a", "127.0.0.1", 4000)
    smart = tomlkit.parse(paths.codex_config.read_text(encoding="utf-8"))
    smart["approval_policy"] = "on-request"
    paths.codex_config.write_text(tomlkit.dumps(smart), encoding="utf-8")

    first = switch_to_native_codex(paths, "127.0.0.1", 4000)
    second = switch_to_native_codex(paths, "127.0.0.1", 4000)
    recovered = tomlkit.parse(paths.codex_config.read_text(encoding="utf-8"))

    assert first["snapshot_restored"] is True
    assert second["snapshot_restored"] is True
    assert recovered["model"] == "gpt-5.6-terra"
    assert recovered["model_provider"] == "openai"
    assert recovered["approval_policy"] == "on-request"
    assert "openai_base_url" not in recovered


def test_native_switch_without_snapshot_uses_codex_default_model(tmp_path, monkeypatch) -> None:
    paths = _paths(tmp_path, monkeypatch)
    paths.codex_config.write_text(
        '''
model = "codex-auto"
model_provider = "litellm"

[model_providers.litellm]
name = "LiteLLM Personal Subscription Router"
base_url = "http://127.0.0.1:4000/v1"
wire_api = "responses"

[model_providers.litellm.auth]
command = "/tmp/python"
args = ["-m", "codexthrift.key_reader"]
''',
        encoding="utf-8",
    )

    switch_to_native_codex(paths, "127.0.0.1", 4000)
    native = tomlkit.parse(paths.codex_config.read_text(encoding="utf-8"))

    assert native["model_provider"] == "openai"
    assert "model" not in native
    assert "model_reasoning_effort" not in native
    assert native["model_providers"]["litellm"]["base_url"] == "http://127.0.0.1:4000/v1"
    assert "openai_base_url" not in native


def test_native_switch_does_not_create_legacy_managed_provider(tmp_path, monkeypatch) -> None:
    paths = _paths(tmp_path, monkeypatch)
    paths.codex_config.write_text('model_provider = "openai"\n', encoding="utf-8")

    result = switch_to_native_codex(paths, "127.0.0.1", 4000)
    native = tomlkit.parse(paths.codex_config.read_text(encoding="utf-8"))

    assert native["model_provider"] == "openai"
    assert "model_providers" not in native
    assert result["resume_providers"] == {"openai": True, "litellm": False}


def test_native_switch_keeps_foreign_litellm_provider(tmp_path, monkeypatch) -> None:
    paths = _paths(tmp_path, monkeypatch)
    paths.codex_config.write_text(
        '''
model = "other-model"
model_provider = "litellm"

[model_providers.litellm]
name = "User Provider"
base_url = "https://example.com/v1"
wire_api = "responses"
''',
        encoding="utf-8",
    )

    switch_to_native_codex(paths, "127.0.0.1", 4000)
    native = tomlkit.parse(paths.codex_config.read_text(encoding="utf-8"))

    assert native["model_provider"] == "openai"
    assert native["model_providers"]["litellm"]["name"] == "User Provider"


def test_smart_switch_repairs_config_drift_and_preserves_foreign_provider(
    tmp_path, monkeypatch
) -> None:
    paths = _paths(tmp_path, monkeypatch)
    original = '''
model = "gpt-5.6-luna"
model_provider = "litellm"
sandbox_mode = "workspace-write"

[model_providers.litellm]
name = "User Provider"
base_url = "https://example.com/v1"
wire_api = "responses"

[mcp_servers.example]
command = "example"
'''
    paths.codex_config.write_text(original, encoding="utf-8")

    result = configure_codex(paths, "phase3a", "127.0.0.1", 4000)
    repaired = tomlkit.parse(paths.codex_config.read_text(encoding="utf-8"))

    assert result["config_drift_repaired"] is True
    assert result["provider_replaced"] is False
    assert result["warnings"]
    assert repaired["model"] == "codex-auto"
    assert repaired["model_provider"] == "openai"
    assert repaired["openai_base_url"] == "http://127.0.0.1:4877/v1"
    assert repaired["sandbox_mode"] == "workspace-write"
    assert repaired["mcp_servers"]["example"]["command"] == "example"
    assert repaired["model_providers"]["litellm"]["base_url"] == "https://example.com/v1"
