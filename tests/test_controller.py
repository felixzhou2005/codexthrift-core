from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import tomlkit

import codexthrift.application.codex_mode_service as codex_mode_service_module
import codexthrift.application.proxy_service as proxy_service_module
from codexthrift.config_templates import profile_probe_models
from codexthrift.controller import STABLE_RUNTIME_SPECS, RouterController
from codexthrift.paths import WorkspacePaths
from codexthrift.util import ControlError


def _controller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RouterController:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    return RouterController(
        WorkspacePaths(tmp_path / "router-home"),
        port=45454,
        ensure_paths=True,
    )


def test_read_only_controller_does_not_initialize_workspace(tmp_path: Path) -> None:
    paths = WorkspacePaths(tmp_path / "router-home")

    RouterController(paths)

    assert not paths.root.exists()


def test_environment_check_does_not_initialize_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "router-home"
    monkeypatch.setenv("CODEX_ROUTER_HOME", str(root))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))

    result = RouterController().environment_check()

    assert result["workspace"] == str(root.resolve())
    assert not root.exists()


def test_feedback_can_append_without_initializing_secrets(tmp_path: Path) -> None:
    paths = WorkspacePaths(tmp_path / "router-home")
    controller = RouterController(paths, ensure_paths=False)

    result = controller.record_feedback(
        prompt_fingerprint=None,
        logical_turn_id="turn-review",
        expected_tier="MEDIUM",
        outcome="success",
    )

    assert result["ok"] is True
    assert paths.feedback_log.exists()
    assert not paths.secrets_dir.exists()


def _write_probe(controller: RouterController, phase: str, profile: str, ok: bool = True) -> None:
    path = controller.paths.probe_result_for(phase, profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "timestamp": "2026-08-04T00:00:00+00:00",
                "phase": phase,
                "profile": profile,
                "all_ok": ok,
                "results": [
                    {**spec, "ok": ok}
                    for spec in profile_probe_models(profile, phase)
                ],
            }
        ),
        encoding="utf-8",
    )


def test_prepare_generates_phase2_without_embedding_master_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    result = controller.prepare_configs(profile="gpt56", confirm="APPLY")
    assert result["active_phase"] == "phase2"
    config = controller.paths.active_config.read_text(encoding="utf-8")
    master_key = controller.paths.master_key_file.read_text(encoding="utf-8").strip()
    assert "os.environ/LITELLM_MASTER_KEY" in config
    assert master_key not in config
    assert controller.paths.master_key_file.stat().st_size > 20


def test_start_proxy_waits_for_authenticated_health_and_records_listener_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    controller.paths.active_config.write_text("model_list: []\n", encoding="utf-8")
    controller.paths.auth_file.parent.mkdir(parents=True, exist_ok=True)
    controller.paths.auth_file.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(controller, "_require_runtime", lambda: None)
    monkeypatch.setattr(controller, "_probe_is_ready", lambda *_args: True)
    monkeypatch.setattr(controller.services.proxy, "proxy_status", lambda: {"running": False})
    monkeypatch.setattr(controller, "_ensure_master_key", lambda: "sk-test")
    monkeypatch.setattr(controller, "_runtime_env", dict)
    monkeypatch.setattr(proxy_service_module, "encryption_key", lambda *_args, **_kwargs: b"a" * 32)
    monkeypatch.setattr(proxy_service_module, "is_port_open", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(proxy_service_module, "spawn_detached", lambda *_args, **_kwargs: SimpleNamespace(pid=41))
    health_results = iter([{"ok": False, "error": "starting"}, {"ok": True, "status_code": 200}])
    monkeypatch.setattr(controller.services.proxy, "_proxy_health", lambda: next(health_results))
    listener_states = iter([None, 73, 73, 73])
    monkeypatch.setattr(
        proxy_service_module,
        "listening_pid",
        lambda *_args, **_kwargs: next(listener_states),
    )
    managed_listener = {
        "pid": 73,
        "running": True,
        "is_litellm": True,
        "managed_by_core": True,
        "command": ["litellm", "--config", str(controller.paths.active_config)],
        "error": None,
    }
    listener_states = iter([
        {
            "pid": None,
            "running": False,
            "is_litellm": False,
            "managed_by_core": False,
            "command": [],
            "error": "listener_not_found",
        },
        managed_listener,
        managed_listener,
        managed_listener,
    ])
    monkeypatch.setattr(
        controller.services.proxy,
        "_listener_state",
        lambda: next(listener_states),
    )
    monkeypatch.setattr(proxy_service_module.time, "sleep", lambda _seconds: None)

    result = controller.start_proxy(confirm="APPLY")

    assert result["ok"] is True
    assert result["pid"] == 73
    assert controller.paths.proxy_pid.read_text(encoding="utf-8") == "73\n"


def test_proxy_status_uses_real_managed_listener_when_pid_file_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    controller.paths.run_dir.mkdir(parents=True, exist_ok=True)
    controller.paths.proxy_pid.write_text("52703\n", encoding="utf-8")
    listener = {
        "pid": 57472,
        "running": True,
        "is_litellm": True,
        "managed_by_core": True,
        "command": ["python", "litellm", "--config", str(controller.paths.active_config)],
        "error": None,
    }
    monkeypatch.setattr(controller.services.proxy, "_listener_state", lambda: listener)
    monkeypatch.setattr(
        proxy_service_module,
        "process_info",
        lambda _pid: {"running": True, "cmdline": listener["command"]},
    )
    monkeypatch.setattr(controller.services.proxy, "_proxy_health", lambda: {"ok": True, "status_code": 200})

    status = controller.proxy_status()

    assert status["running"] is True
    assert status["pid"] == 57472
    assert status["listener_pid"] == 57472


def test_proxy_directly_kills_unknown_listener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    monkeypatch.setattr(
        controller.services.proxy,
        "_listener_state",
        lambda: {
            "pid": 99,
            "running": True,
            "is_litellm": False,
            "managed_by_core": False,
            "command": ["python", "other-service"],
            "error": "listener_is_not_litellm",
        },
    )
    killed: list[int] = []
    monkeypatch.setattr(proxy_service_module, "listening_pid", lambda *_args, **_kwargs: 99)
    monkeypatch.setattr(proxy_service_module, "force_kill_process", killed.append)
    monkeypatch.setattr(proxy_service_module, "is_port_open", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        proxy_service_module,
        "wait_for_port_closed",
        lambda *_args, **_kwargs: True,
    )
    controller.paths.proxy_pid.write_text("99\n", encoding="utf-8")

    result = controller.stop_proxy(confirm="APPLY")

    assert result["ok"] is True
    assert result["forced"] is True
    assert killed == [99]


def test_proxy_force_kills_managed_listener_when_normal_stop_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    listener = {
        "pid": 57472,
        "running": True,
        "is_litellm": True,
        "managed_by_core": True,
        "command": ["litellm", "--config", str(controller.paths.active_config)],
        "error": None,
    }
    monkeypatch.setattr(controller.services.proxy, "_listener_state", lambda: listener)
    monkeypatch.setattr(
        proxy_service_module,
        "stop_process_tree",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ControlError("stop failed")),
    )
    killed: list[int] = []
    monkeypatch.setattr(proxy_service_module, "listening_pid", lambda *_args, **_kwargs: 57472)
    monkeypatch.setattr(proxy_service_module, "force_kill_process", killed.append)
    monkeypatch.setattr(proxy_service_module, "is_port_open", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        proxy_service_module,
        "wait_for_port_closed",
        lambda *_args, **_kwargs: True,
    )

    result = controller.stop_proxy(confirm="APPLY")

    assert result["ok"] is True
    assert result["forced"] is True
    assert result["normal_stop_error"] == "ControlError: stop failed"
    assert killed == [57472]


def test_proxy_listener_identity_uses_core_pid_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    controller.paths.run_dir.mkdir(parents=True, exist_ok=True)
    controller.paths.proxy_pid.write_text("57472\n", encoding="utf-8")
    monkeypatch.setattr(proxy_service_module, "listening_pid", lambda *_args, **_kwargs: 57472)
    monkeypatch.setattr(
        proxy_service_module,
        "process_info",
        lambda _pid: {"running": True, "cmdline": ["python", "litellm"]},
    )

    listener = controller.services.proxy._listener_state()

    assert listener["is_litellm"] is True
    assert listener["managed_by_core"] is True


def test_phase21_candidate_does_not_overwrite_active_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    controller.prepare_configs(profile="gpt56", confirm="APPLY")
    active_before = controller.paths.active_config.read_bytes()
    result = controller.prepare_phase21_candidate(profile="gpt56", confirm="APPLY")
    assert result["activated"] is False
    assert result["subscription_calls"] == 0
    assert controller.paths.active_config.read_bytes() == active_before
    assert controller.paths.phase21_config.exists()
    assert controller.paths.phase21_routing_callback.exists()
    assert controller.paths.phase21_audit_callback.exists()
    assert controller.paths.phase21_routing_policy.exists()


def test_phase21_activation_requires_phase2_probe_and_marks_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    controller.prepare_configs(profile="gpt56", confirm="APPLY")
    controller.prepare_phase21_candidate(profile="gpt56", confirm="APPLY")
    with pytest.raises(ControlError, match="phase2/gpt56"):
        controller.activate_phase21(profile="gpt56", confirm="APPLY")
    _write_probe(controller, "phase2", "gpt56")
    monkeypatch.setattr(controller, "proxy_status", lambda: {"running": False})
    result = controller.activate_phase21(profile="gpt56", confirm="APPLY")
    assert result["policy"] == "phase2.1"
    assert controller.active_policy() == "phase2.1"
    assert "routing_callback_candidate.route_controller" in controller.paths.active_config.read_text()


def test_phase3_activation_requires_matching_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    controller.prepare_configs(profile="gpt56", confirm="APPLY")
    with pytest.raises(ControlError, match="phase3/gpt56"):
        controller.activate_phase(phase="phase3", profile="gpt56", confirm="APPLY")

    _write_probe(controller, "phase3", "gpt56")
    result = controller.activate_phase(phase="phase3", profile="gpt56", confirm="APPLY")
    assert result["phase"] == "phase3"
    assert controller.active_phase() == "phase3"
    document = tomlkit.parse(controller.paths.codex_config.read_text(encoding="utf-8"))
    assert document["model"] == "codex-auto"
    assert "model_reasoning_effort" not in document
    assert document["model_provider"] == "openai"
    assert document["openai_base_url"] == "http://127.0.0.1:4877/v1"
    assert "model_providers" not in document


def test_setup_guide_uses_active_phase_specific_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    controller.prepare_configs(profile="gpt56", confirm="APPLY")
    controller.paths.runtime_python.parent.mkdir(parents=True, exist_ok=True)
    controller.paths.runtime_python.write_text("", encoding="utf-8")
    controller.paths.runtime_litellm.write_text("", encoding="utf-8")
    controller.paths.auth_file.write_text("{}", encoding="utf-8")

    guide = controller.setup_guide()
    assert guide["next_action"]["tool"] == "probe_subscription_models"
    assert guide["next_action"]["arguments"] == {"phase": "phase2", "profile": "gpt56"}

    _write_probe(controller, "phase2", "gpt56")
    guide = controller.setup_guide()
    assert guide["ready"]["model_probe"] is True
    assert guide["next_action"]["tool"] == "start_litellm_proxy"


def test_codex_configuration_cannot_drift_from_active_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    controller.prepare_configs(profile="gpt56", confirm="APPLY")
    _write_probe(controller, "phase2", "gpt56")
    with pytest.raises(ControlError, match="必须与活动"):
        controller.configure_codex(phase="phase3", confirm="APPLY")


def test_setup_guide_does_not_accept_unrelated_codex_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    controller.prepare_configs(profile="gpt56", confirm="APPLY")
    controller.paths.runtime_python.parent.mkdir(parents=True, exist_ok=True)
    controller.paths.runtime_python.write_text("", encoding="utf-8")
    controller.paths.runtime_litellm.write_text("", encoding="utf-8")
    controller.paths.auth_file.write_text("{}", encoding="utf-8")
    _write_probe(controller, "phase2", "gpt56")
    controller.paths.codex_config.parent.mkdir(parents=True, exist_ok=True)
    controller.paths.codex_config.write_text('model = "some-other-model"\n', encoding="utf-8")

    monkeypatch.setattr(controller, "proxy_status", lambda: {"running": True})
    guide = controller.setup_guide()
    assert guide["ready"]["codex_config"] is False
    assert guide["codex_config_status"]["exists"] is True
    assert "model_not_codex_auto" in guide["codex_config_status"]["mismatches"]
    assert guide["next_action"]["tool"] == "configure_codex_client"


def test_configure_codex_produces_verified_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    controller.prepare_configs(profile="gpt56", confirm="APPLY")
    _write_probe(controller, "phase2", "gpt56")
    controller.configure_codex(phase="phase2", confirm="APPLY")

    monkeypatch.setattr(controller, "proxy_status", lambda: {"running": True})
    guide = controller.setup_guide()
    assert guide["ready"]["codex_config"] is True
    assert guide["codex_config_status"]["configured"] is True
    assert guide["codex_config_status"]["mismatches"] == []


def test_configure_codex_can_be_used_by_managed_switch_before_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    controller.prepare_configs(profile="gpt56", confirm="APPLY")

    result = controller.configure_codex(
        phase="phase2",
        confirm="APPLY",
        allow_unverified=True,
    )

    assert result["model_probe_verified"] is False
    assert result["allow_unverified"] is True
    assert result["provider"] == "openai"


def test_switch_to_native_codex_keeps_proxy_running_and_marks_existing_clients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    controller.paths.codex_config.parent.mkdir(parents=True, exist_ok=True)
    controller.paths.codex_config.write_text(
        '''
model = "codex-auto"
model_provider = "litellm"

[model_providers.litellm]
name = "LiteLLM Personal Subscription Router"
base_url = "http://127.0.0.1:45454/v1"
wire_api = "responses"

[model_providers.litellm.auth]
command = "/tmp/python"
args = ["-m", "codexthrift.key_reader"]
''',
        encoding="utf-8",
    )
    clients = {
        "clients": [{"pid": 22, "create_time": 1.0, "kind": "codex_cli"}],
        "counts": {"codex_cli": 1},
        "detection_status": "available",
    }
    monkeypatch.setattr(codex_mode_service_module, "detect_codex_clients", lambda: clients)

    class _Verifier:
        def __init__(self, paths: WorkspacePaths):
            assert paths == controller.paths

        def verify(self, expected_provider: str) -> dict[str, object]:
            assert expected_provider == "openai"
            return {
                "verified": True,
                "native_auth_state": "connected",
                "account_type": "chatgpt",
            }

    monkeypatch.setattr(codex_mode_service_module, "CodexModeVerifier", _Verifier)
    monkeypatch.setattr(
        controller,
        "proxy_status",
        lambda: {"running": True, "health": {"ok": True}},
    )

    result = controller.switch_to_native_codex(confirm="SWITCH_NATIVE")

    assert result["ok"] is True
    assert result["mode"]["client_mode"] == "native_openai"
    assert result["mode"]["proxy_used_by_codex"] is False
    assert result["mode"]["restart_required"] is True


def test_switch_to_native_refuses_when_resume_proxy_cannot_enter_standby(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    controller.paths.codex_config.parent.mkdir(parents=True, exist_ok=True)
    original = 'model = "codex-auto"\nmodel_provider = "litellm"\n'
    controller.paths.codex_config.write_text(original, encoding="utf-8")
    monkeypatch.setattr(
        codex_mode_service_module,
        "detect_codex_clients",
        lambda: {"clients": [], "counts": {}, "detection_status": "available"},
    )
    monkeypatch.setattr(
        controller,
        "proxy_status",
        lambda: {"running": False, "health": {"ok": False}},
    )
    monkeypatch.setattr(
        controller,
        "start_proxy",
        lambda **_kwargs: {"ok": False, "error": "not ready"},
    )

    result = controller.switch_to_native_codex(confirm="SWITCH_NATIVE")

    assert result["ok"] is False
    assert result["stage"] == "standby_proxy"
    assert result["rollback"] == {
        "performed": False,
        "reason": "config_not_modified",
    }
    assert controller.paths.codex_config.read_text(encoding="utf-8") == original


def test_force_native_switch_does_not_require_proxy_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    controller.paths.codex_config.parent.mkdir(parents=True, exist_ok=True)
    controller.paths.codex_config.write_text(
        'model = "codex-auto"\nmodel_provider = "litellm"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        codex_mode_service_module,
        "detect_codex_clients",
        lambda: {"clients": [], "counts": {}, "detection_status": "available"},
    )
    monkeypatch.setattr(
        controller,
        "proxy_status",
        lambda: {"running": False, "health": {"ok": False}, "listener_error": "port_closed"},
    )
    monkeypatch.setattr(
        codex_mode_service_module,
        "CodexModeVerifier",
        lambda _paths: SimpleNamespace(verify=lambda _provider: {"verified": False}),
    )

    result = controller.force_native_codex(confirm="FORCE_SWITCH_NATIVE")

    assert result["ok"] is True
    assert result["forced"] is True
    assert result["warnings"]
    assert result["mode"]["effective_provider"] == "openai"
    assert "model_provider = \"openai\"" in controller.paths.codex_config.read_text(encoding="utf-8")


def test_smart_route_preflight_failure_keeps_native_config_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    controller.paths.codex_config.parent.mkdir(parents=True, exist_ok=True)
    original = 'model = "gpt-5.6-sol"\nmodel_provider = "openai"\n'
    controller.paths.codex_config.write_text(original, encoding="utf-8")
    monkeypatch.setattr(codex_mode_service_module, "detect_codex_clients", lambda: {"clients": []})
    monkeypatch.setattr(
        controller,
        "start_proxy",
        lambda **kwargs: {"ok": True, "running": True},
    )
    monkeypatch.setattr(
        codex_mode_service_module,
        "verify_codex_proxy_route",
        lambda **kwargs: {
            "verified": False,
            "status": "failed",
            "error_code": "auth_command_failed",
        },
    )

    result = controller.enable_smart_routing(confirm="ENABLE_ROUTER")

    assert result["ok"] is False
    assert result["stage"] == "proxy_preflight"
    assert result["rollback"] == {
        "performed": False,
        "reason": "config_not_modified",
    }
    assert controller.paths.codex_config.read_text(encoding="utf-8") == original


def test_smart_route_config_verification_failure_restores_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    controller.paths.codex_config.parent.mkdir(parents=True, exist_ok=True)
    original = 'model = "gpt-5.6-sol"\nmodel_provider = "openai"\n'
    controller.paths.codex_config.write_text(original, encoding="utf-8")
    backup = controller.paths.backups_dir / "codex-config-before-smart-test.toml.bak"
    backup.write_text(original, encoding="utf-8")
    monkeypatch.setattr(codex_mode_service_module, "detect_codex_clients", lambda: {"clients": []})
    monkeypatch.setattr(
        controller,
        "start_proxy",
        lambda **kwargs: {"ok": True, "running": True},
    )
    monkeypatch.setattr(
        codex_mode_service_module,
        "verify_codex_proxy_route",
        lambda **kwargs: {"verified": True, "status": "verified"},
    )

    def configure(**kwargs):
        controller.paths.codex_config.write_text(
            'model = "codex-auto"\nmodel_provider = "openai"\nopenai_base_url = "http://127.0.0.1:4877/v1"\n',
            encoding="utf-8",
        )
        return {"ok": True, "backup_path": str(backup)}

    monkeypatch.setattr(controller.services.codex_mode, "configure_codex", configure)

    class _Verifier:
        def __init__(self, paths: WorkspacePaths):
            assert paths == controller.paths

        def verify(self, expected_provider: str) -> dict[str, object]:
            assert expected_provider == "openai"
            return {"verified": False, "status": "unavailable"}

    monkeypatch.setattr(codex_mode_service_module, "CodexModeVerifier", _Verifier)

    result = controller.enable_smart_routing(confirm="ENABLE_ROUTER")

    assert result["ok"] is False
    assert result["stage"] == "config_verification"
    assert result["rollback"]["performed"] is True
    assert controller.paths.codex_config.read_text(encoding="utf-8") == original


def test_native_config_verification_failure_restores_routed_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    controller.paths.codex_config.parent.mkdir(parents=True, exist_ok=True)
    original = '''
model = "codex-auto"
model_provider = "litellm"

[model_providers.litellm]
name = "LiteLLM Personal Subscription Router"
base_url = "http://127.0.0.1:45454/v1"
wire_api = "responses"

[model_providers.litellm.auth]
command = "/tmp/python"
args = ["-m", "codexthrift.key_reader"]
'''
    controller.paths.codex_config.write_text(original, encoding="utf-8")
    monkeypatch.setattr(
        codex_mode_service_module,
        "detect_codex_clients",
        lambda: {"clients": [], "counts": {}, "detection_status": "available"},
    )

    class _Verifier:
        def __init__(self, paths: WorkspacePaths):
            assert paths == controller.paths

        def verify(self, expected_provider: str) -> dict[str, object]:
            assert expected_provider == "openai"
            return {"verified": False, "status": "unavailable"}

    monkeypatch.setattr(codex_mode_service_module, "CodexModeVerifier", _Verifier)
    monkeypatch.setattr(
        controller,
        "proxy_status",
        lambda: {"running": True, "health": {"ok": True}},
    )

    result = controller.switch_to_native_codex(confirm="SWITCH_NATIVE")

    assert result["ok"] is False
    assert result["stage"] == "config_verification"
    assert result["rollback"]["performed"] is True
    assert controller.paths.codex_config.read_text(encoding="utf-8") == original


def test_codex_mode_status_distinguishes_running_proxy_from_native_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    monkeypatch.setattr(
        codex_mode_service_module,
        "codex_connection_status",
        lambda *args: {
            "client_mode": "native_openai",
            "effective_provider": "openai",
            "config_path": str(controller.paths.codex_config),
        },
    )
    monkeypatch.setattr(
        codex_mode_service_module,
        "detect_codex_clients",
        lambda: {"clients": [], "counts": {}, "detection_status": "available"},
    )
    monkeypatch.setattr(
        codex_mode_service_module,
        "restart_status",
        lambda *args: {"restart_required": False, "stale_clients": []},
    )
    monkeypatch.setattr(
        codex_mode_service_module,
        "read_mode_state",
        lambda *args: {"verification": {"native_auth_state": "connected"}},
    )
    monkeypatch.setattr(controller, "proxy_status", lambda: {"running": True})

    status = controller.codex_mode_status()

    assert status["new_session_mode"] == "native"
    assert status["proxy_running"] is True
    assert status["proxy_used_by_codex"] is False
    assert status["proxy_route_state"] == "standby"
    assert status["route_verification"] == {
        "status": "not_applicable",
        "verified": False,
        "error_code": None,
        "duration_ms": None,
    }


def test_codex_mode_status_reports_routed_verification_and_stale_clients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    monkeypatch.setattr(
        codex_mode_service_module,
        "codex_connection_status",
        lambda *args: {
            "client_mode": "smart_router",
            "effective_provider": "openai",
            "config_path": str(controller.paths.codex_config),
        },
    )
    monkeypatch.setattr(
        codex_mode_service_module,
        "detect_codex_clients",
        lambda: {
            "clients": [{"pid": 42, "create_time": 1.0, "kind": "codex_cli"}],
            "counts": {"codex_cli": 1},
            "detection_status": "available",
        },
    )
    monkeypatch.setattr(
        codex_mode_service_module,
        "restart_status",
        lambda *args: {
            "restart_required": True,
            "stale_clients": [{"pid": 42, "kind": "codex_cli"}],
        },
    )
    monkeypatch.setattr(
        codex_mode_service_module,
        "read_mode_state",
        lambda *args: {
            "verification": {
                "route_preflight": {
                    "status": "verified",
                    "verified": True,
                    "duration_ms": 125,
                }
            }
        },
    )
    monkeypatch.setattr(controller, "proxy_status", lambda: {"running": True})

    status = controller.codex_mode_status()

    assert status["new_session_mode"] == "routed"
    assert status["proxy_route_state"] == "routed"
    assert status["proxy_used_by_codex"] is True
    assert status["stale_client_count"] == 1
    assert status["route_verification"] == {
        "status": "verified",
        "verified": True,
        "error_code": None,
        "duration_ms": 125,
    }


def test_status_outputs_redact_nested_tokens_and_master_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    master_key = "test-master-key-redaction-fixture"
    controller.paths.master_key_file.write_text(master_key, encoding="utf-8")
    controller.paths.proxy_log.write_text(
        f"Authorization: Bearer test-oauth-token-redaction-fixture\nmaster={master_key}\n",
        encoding="utf-8",
    )
    controller.paths.oauth_result.write_text(
        json.dumps({"nested": {"access_token": "test-oauth-token-redaction-fixture"}}),
        encoding="utf-8",
    )

    proxy = controller.proxy_status()
    oauth = controller.oauth_status()
    serialized = json.dumps({"proxy": proxy, "oauth": oauth})

    assert master_key not in serialized
    assert "test-oauth-token-redaction-fixture" not in serialized
    assert "***REDACTED***" in serialized


def test_macos_child_environment_normalizes_unsupported_c_utf8(monkeypatch) -> None:
    from codexthrift.platform import process

    monkeypatch.setattr(process.sys, "platform", "darwin")
    env = process.normalized_child_env({"LANG": "C.UTF-8", "PATH": "/usr/bin"})

    assert env["LANG"] == "C"
    assert env["LC_ALL"] == "C"
    assert env["PATH"] == "/usr/bin"


def test_runtime_env_drops_incompatible_python_and_debug_settings(
    tmp_path: Path, monkeypatch
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    monkeypatch.setenv("DEBUG", "release")
    monkeypatch.setenv("PYTHONHOME", "/bundle/python")
    monkeypatch.setenv("PYTHONPATH", "/bundle/site-packages")

    env = controller._runtime_env()

    assert "DEBUG" not in env
    assert "PYTHONHOME" not in env
    assert "PYTHONPATH" not in env


def test_runtime_install_is_already_satisfied_by_bundled_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "bundled-runtime"
    (bundle / "bin").mkdir(parents=True)
    (bundle / "bin" / "python").write_text("", encoding="utf-8")
    (bundle / "bin" / "litellm").write_text("", encoding="utf-8")
    monkeypatch.setenv("CODEXTHRIFT_RUNTIME_ROOT", str(bundle))
    controller = _controller(tmp_path, monkeypatch)

    result = controller.start_runtime_install(confirm="APPLY")

    assert result == {
        "ok": True,
        "already_installed": True,
        "source": "bundled",
        "runtime_root": str(bundle.resolve()),
    }
    assert not controller.paths.install_pid.exists()


def test_runtime_install_caches_stay_inside_managed_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    env = controller._runtime_install_env()
    managed_root = str(controller.paths.root)

    for key in ("PIP_CACHE_DIR", "XDG_CACHE_HOME", "CARGO_HOME", "RUSTUP_HOME"):
        assert env[key].startswith(managed_root + "/cache/")
    assert env["PATH"].split(":", 1)[0] == env["CARGO_HOME"] + "/bin"


def test_stable_runtime_pins_known_compatible_fastapi() -> None:
    assert "litellm[proxy]==1.95.0" in STABLE_RUNTIME_SPECS
    assert "fastapi==0.136.3" in STABLE_RUNTIME_SPECS
