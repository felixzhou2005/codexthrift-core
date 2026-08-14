from __future__ import annotations

import tomlkit

from codexthrift.codex_mode import (
    CodexClientMode,
    capture_native_snapshot,
    classify_codex_document,
    read_mode_state,
    read_native_snapshot,
    record_mode_switch,
)
from codexthrift.paths import WorkspacePaths


def _router_document() -> object:
    return tomlkit.parse(
        '''
model = "codex-auto"
model_provider = "openai"
openai_base_url = "http://127.0.0.1:4877/v1"
'''
    )


def _legacy_router_document() -> object:
    return tomlkit.parse(
        '''
model = "codex-auto"
model_provider = "litellm"

[model_providers.litellm]
name = "LiteLLM Personal Subscription Router"
base_url = "http://127.0.0.1:4000/v1"
wire_api = "responses"

[model_providers.litellm.auth]
command = "/tmp/python"
args = ["-m", "codex_router_app.key_reader"]
'''
    )


def _packaged_router_document() -> object:
    document = _legacy_router_document()
    document["model_providers"]["litellm"]["auth"]["args"] = [
        "--print-codex-proxy-token"
    ]
    return document


def test_codex_mode_classifies_native_router_and_drift() -> None:
    native = tomlkit.parse('model = "gpt-5.6-terra"\nmodel_provider = "openai"\n')
    drift = tomlkit.parse('model = "codex-auto"\nmodel_provider = "openai"\n')

    assert classify_codex_document(native, host="127.0.0.1", port=4000)["client_mode"] == "native_openai"
    assert classify_codex_document(_router_document(), host="127.0.0.1", port=4000)["client_mode"] == "smart_router"
    assert classify_codex_document(drift, host="127.0.0.1", port=4000)["client_mode"] == "config_drift"


def test_codex_mode_reports_legacy_provider_availability_without_selecting_it() -> None:
    invalid = _legacy_router_document()
    invalid["model_providers"]["litellm"]["auth"]["args"] = ["-m", "example.key_reader"]

    legacy = classify_codex_document(_legacy_router_document(), host="127.0.0.1", port=4000)
    invalid_status = classify_codex_document(invalid, host="127.0.0.1", port=4000)
    assert legacy["client_mode"] == "config_drift"
    assert legacy["legacy_provider_available"] is True
    assert invalid_status["legacy_provider_available"] is False


def test_codex_mode_accepts_packaged_key_reader_command() -> None:
    packaged = classify_codex_document(
        _packaged_router_document(), host="127.0.0.1", port=4000
    )
    assert packaged["client_mode"] == "config_drift"
    assert packaged["legacy_provider_available"] is True


def test_native_snapshot_preserves_presence_and_codex_home(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    paths = WorkspacePaths(tmp_path / "router-home")
    paths.ensure()
    document = tomlkit.parse('model = "gpt-5.6-terra"\nmodel_reasoning_effort = "medium"\n')

    capture_native_snapshot(paths, document)
    snapshot = read_native_snapshot(paths)

    assert snapshot is not None
    assert snapshot["managed_keys"]["model"] == {"present": True, "value": "gpt-5.6-terra"}
    assert snapshot["managed_keys"]["model_provider"] == {"present": False, "value": None}
    assert snapshot["codex_home"] == str(paths.codex_home)


def test_mode_state_generation_increments(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    paths = WorkspacePaths(tmp_path / "router-home")
    paths.ensure()

    first = record_mode_switch(paths, CodexClientMode.SMART_ROUTER)
    second = record_mode_switch(paths, CodexClientMode.NATIVE_OPENAI)

    assert first["generation"] == 1
    assert second["generation"] == 2
    assert read_mode_state(paths) == second
