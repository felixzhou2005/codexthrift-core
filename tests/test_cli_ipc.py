from __future__ import annotations

import codexthrift.cli as cli_module


def test_cli_proxy_status_uses_daemon_by_default(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        cli_module.IpcClient,
        "call",
        lambda _self, method, params: calls.append((method, params)) or {"running": True},
    )
    args = cli_module._parser().parse_args(["proxy-status"])

    assert cli_module.dispatch(args) == {"running": True}
    assert calls == [("proxy.status", {})]


def test_cli_direct_flag_skips_daemon_mapping() -> None:
    args = cli_module._parser().parse_args(["--direct", "proxy-status"])

    assert args.direct is True
    assert cli_module._rpc_request(args) == ("proxy.status", {})


def test_every_cli_command_has_daemon_mapping() -> None:
    command_lines = [
        ["guide"], ["status"], ["environment"], ["prepare", "--confirm", "APPLY"],
        ["install-runtime", "--confirm", "APPLY"], ["install-status"],
        ["oauth", "--confirm", "APPLY"], ["oauth-status"],
        ["probe", "--confirm", "APPLY"], ["probe-status"],
        ["configure-codex", "--confirm", "APPLY"], ["codex-mode", "status"],
        ["codex-mode", "smart", "--confirm", "ENABLE_ROUTER"],
        ["codex-mode", "native", "--confirm", "SWITCH_NATIVE"],
        ["proxy-start", "--confirm", "APPLY"], ["proxy-status"],
        ["proxy-stop", "--confirm", "APPLY"], ["proxy-restart", "--confirm", "APPLY"],
        ["phase2", "--confirm", "APPLY"], ["phase3", "--confirm", "APPLY"],
        ["phase21-prepare", "--confirm", "APPLY"], ["phase21-activate", "--confirm", "APPLY"],
        ["phase3a-prepare", "--confirm", "APPLY"], ["phase3a-activate", "--confirm", "APPLY"],
        ["phase3a-recover", "--confirm", "APPLY"], ["adaptive-status"],
        ["adaptive-import-tasks", "--confirm", "APPLY"], ["adaptive-evaluate", "task"],
        ["adaptive-shadow", "--confirm", "APPLY"],
        ["adaptive-canary", "--percent", "10", "--confirm", "APPLY"],
        ["adaptive-disable", "--confirm", "APPLY"], ["logs"], ["report"], ["calls"],
        ["turns"], ["explain", "--turn-id", "turn"],
        [
            "feedback", "--turn-id", "turn", "--expected-tier", "MEDIUM",
            "--outcome", "success",
        ],
        ["backups"], ["restore-codex", "/tmp/backup", "--confirm", "APPLY"],
        ["task-run", "/tmp/repo", "task", "--confirm", "RUN_CODEX"],
        ["task-status", "task-1234"], ["task-list"],
        ["task-stop", "task-1234", "--confirm", "STOP_CODEX"],
    ]

    for command_line in command_lines:
        args = cli_module._parser().parse_args(command_line)
        assert cli_module._rpc_request(args) is not None, command_line
