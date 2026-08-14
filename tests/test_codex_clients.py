from __future__ import annotations

from types import SimpleNamespace

import codexthrift.codex_clients as codex_clients_module
from codexthrift.codex_clients import (
    classify_codex_process,
    codex_distribution,
    command_fingerprint,
    detect_codex_clients,
    restart_status,
)
from codexthrift.codex_mode import CodexClientMode, record_mode_switch
from codexthrift.paths import WorkspacePaths


def test_classifies_cli_and_vscode_app_server() -> None:
    assert classify_codex_process(["/opt/homebrew/bin/codex"], []) == "codex_cli"
    assert classify_codex_process(
        ["/Applications/ChatGPT.app/Contents/Resources/codex", "app-server", "--stdio"],
        ["Code Helper (Plugin)", "Visual Studio Code"],
    ) == "vscode_app_server"
    assert classify_codex_process(["/usr/bin/python"], []) is None


def test_ignores_routerllm_and_chatgpt_helper_processes() -> None:
    assert classify_codex_process(
        ["/Applications/RouterLLM Desk.app/Contents/MacOS/routerllm-dashboard", "--print-codex-proxy-token"],
        [],
    ) is None


def test_command_fingerprint_is_stable_without_exposing_arguments() -> None:
    command = ["/opt/homebrew/bin/codex", "--config", "secret-project-token"]

    fingerprint = command_fingerprint(command)

    assert fingerprint == command_fingerprint(command)
    assert len(fingerprint) == 64
    assert "secret-project-token" not in fingerprint


def test_vscode_distribution_marks_an_older_installed_extension_for_reload(tmp_path) -> None:
    extensions = tmp_path / "extensions"
    old = extensions / "openai.chatgpt-26.803.41515-darwin-arm64"
    new = extensions / "openai.chatgpt-26.803.61601-darwin-arm64"
    old.mkdir(parents=True)
    new.mkdir(parents=True)

    result = codex_distribution(str(old / "bin" / "macos-aarch64" / "codex"))

    assert result == {
        "distribution": "vscode_extension",
        "client_version": "26.803.41515",
        "latest_installed_version": "26.803.61601",
        "update_available": True,
        "recommended_action": "reload_vscode_window",
    }


def test_current_vscode_distribution_still_requires_reload_after_mode_switch(tmp_path) -> None:
    extension = tmp_path / "extensions" / "openai.chatgpt-26.803.61601-darwin-arm64"
    extension.mkdir(parents=True)

    result = codex_distribution(str(extension / "bin" / "macos-aarch64" / "codex"))

    assert result["update_available"] is False
    assert result["recommended_action"] == "reload_host_after_mode_switch"


def test_named_codex_but_unclassified_process_is_not_permission_limited(
    monkeypatch,
) -> None:
    process = SimpleNamespace(
        pid=44,
        cmdline=lambda: ["/tmp/codex", "sandbox", "--", "/bin/zsh"],
        ppid=lambda: 1,
    )
    monkeypatch.setattr(codex_clients_module.psutil, "Process", lambda _pid: process)
    monkeypatch.setattr(codex_clients_module, "_parent_names", lambda _pid: [])

    identity, limited = codex_clients_module._codex_client_identity(44)

    assert identity is None
    assert limited is False


def test_detect_marks_permission_limited_when_one_process_cannot_be_read(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        codex_clients_module.psutil,
        "process_iter",
        lambda _attrs: [SimpleNamespace(info={"pid": 42, "name": "codex"})],
    )
    monkeypatch.setattr(
        codex_clients_module,
        "_codex_client_identity",
        lambda _pid: (None, True),
    )

    detected = detect_codex_clients()

    assert detected["clients"] == []
    assert detected["detection_status"] == "limited"
    assert classify_codex_process(
        ["/Applications/ChatGPT.app/Contents/MacOS/Codex (Service)", "--type=utility"],
        ["ChatGPT"],
    ) is None
    assert classify_codex_process(
        ["/Applications/ChatGPT.app/Contents/Resources/codex", "sandbox", "--", "/bin/zsh"],
        ["codex"],
    ) is None


def test_detect_ignores_non_codex_process_before_sensitive_inspection(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        codex_clients_module.psutil,
        "process_iter",
        lambda _attrs: [SimpleNamespace(info={"pid": 99, "name": "system-daemon"})],
    )
    inspected: list[int] = []
    monkeypatch.setattr(
        codex_clients_module,
        "_codex_client_identity",
        lambda pid: inspected.append(pid) or (None, True),
    )

    detected = detect_codex_clients()

    assert detected["detection_status"] == "available"
    assert inspected == []


def test_restart_status_clears_after_observed_process_exits(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    paths = WorkspacePaths(tmp_path / "router-home")
    paths.ensure()
    observed = [{"pid": 42, "create_time": 100.0, "kind": "codex_cli"}]
    record_mode_switch(paths, CodexClientMode.NATIVE_OPENAI, observed_clients=observed)

    pending = restart_status(
        paths,
        {"clients": observed, "counts": {"codex_cli": 1}, "detection_status": "available"},
    )
    complete = restart_status(
        paths,
        {"clients": [], "counts": {"codex_cli": 0}, "detection_status": "available"},
    )

    assert pending["restart_required"] is True
    assert complete["restart_required"] is False
