from __future__ import annotations

import os
import signal

import tomlkit

from codexthrift import dashboard
from codexthrift.codex_mode import read_mode_state
from codexthrift.dashboard import _parser, _process_exists, _watch_parent, main
from codexthrift.paths import WorkspacePaths


def test_dashboard_can_print_codex_proxy_token(tmp_path, monkeypatch, capsys) -> None:
    router_home = tmp_path / "router-home"
    key_path = router_home / "secrets" / "litellm-master-key"
    key_path.parent.mkdir(parents=True)
    key_path.write_text("test-local-token\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_ROUTER_HOME", str(router_home))
    monkeypatch.setattr(
        "sys.argv",
        ["routerllm-dashboard", "--print-codex-proxy-token"],
    )

    assert main() == 0
    captured = capsys.readouterr()
    assert captured.out == "test-local-token\n"
    assert captured.err == ""


def test_dashboard_parser_accepts_parent_pid() -> None:
    args = _parser().parse_args(["--parent-pid", "1234"])

    assert args.parent_pid == 1234


def test_dashboard_parser_accepts_force_native() -> None:
    args = _parser().parse_args(["--force-native"])

    assert args.force_native is True


def test_force_native_fallback_bypasses_router_and_preserves_unrelated_config(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("CODEX_ROUTER_HOME", str(tmp_path / "router-home"))
    paths = WorkspacePaths.default()
    paths.ensure()
    paths.codex_config.parent.mkdir(parents=True, exist_ok=True)
    paths.codex_config.write_text(
        f'''
model = "codex-auto"
model_provider = "litellm"
sandbox_mode = "workspace-write"

[mcp_servers.example]
command = "example"

[model_providers.litellm]
name = "LiteLLM Personal Subscription Router"
base_url = "http://127.0.0.1:4000/v1"
wire_api = "responses"

[model_providers.litellm.auth]
command = "/bin/cat"
args = ["{paths.master_key_file}"]
''',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        dashboard,
        "detect_codex_clients",
        lambda: {
            "clients": [{"pid": 123, "create_time": 456.0, "kind": "codex_cli"}],
            "counts": {"codex_cli": 1},
            "detection_status": "available",
        },
        raising=False,
    )

    result = dashboard._force_native_fallback(host="127.0.0.1", proxy_port=4000)
    config = tomlkit.parse(paths.codex_config.read_text(encoding="utf-8"))
    state = read_mode_state(paths)

    assert result["ok"] is True
    assert result["forced"] is True
    assert result["mode"]["new_session_mode"] == "native"
    assert result["mode"]["stale_client_count"] == 1
    assert result["switch"]["backup_path"]
    assert config["model_provider"] == "openai"
    assert config["sandbox_mode"] == "workspace-write"
    assert config["mcp_servers"]["example"]["command"] == "example"
    assert config["model_providers"]["litellm"]["base_url"] == "http://127.0.0.1:4000/v1"
    assert state is not None
    assert state["verification"]["status"] == "forced_without_online_verification"


def test_force_native_fallback_continues_when_client_detection_is_denied(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("CODEX_ROUTER_HOME", str(tmp_path / "router-home"))
    paths = WorkspacePaths.default()
    paths.ensure()
    paths.codex_config.parent.mkdir(parents=True, exist_ok=True)
    paths.codex_config.write_text('model_provider = "litellm"\nmodel = "codex-auto"\n', encoding="utf-8")
    monkeypatch.setattr(dashboard, "detect_codex_clients", lambda: (_ for _ in ()).throw(PermissionError()))

    result = dashboard._force_native_fallback(host="127.0.0.1", proxy_port=4000)

    assert result["ok"] is True
    assert result["mode"]["restart_required"] is True
    assert result["mode"]["stale_client_count"] == 0


def test_process_exists_reports_current_and_missing_processes() -> None:
    assert _process_exists(os.getpid()) is True
    assert _process_exists(999_999_999) is False


def test_parent_watchdog_terminates_routerd_when_parent_exits(monkeypatch) -> None:
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(dashboard.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(dashboard, "_process_exists", lambda _pid: False)
    monkeypatch.setattr(dashboard.os, "kill", lambda pid, sig: signals.append((pid, sig)))

    _watch_parent(1234)

    assert signals == [(os.getpid(), signal.SIGTERM)]
