from __future__ import annotations

import argparse
import json
from typing import Any

from .codex_sessions import launch_codex_session, list_codex_sessions
from .controller import RouterController
from .daemon.ipc_client import IpcClient
from .paths import WorkspacePaths
from .util import ControlError

PROFILE_CHOICES = ["gpt56"]


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codexthrift",
        description="CodexThrift local routing, account, and observability core",
    )
    parser.add_argument(
        "--direct",
        action="store_true",
        help="Bypass codexthriftd for bootstrap or recovery.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("guide")
    sub.add_parser("status")
    sub.add_parser("environment")

    sessions = sub.add_parser("codex-sessions")
    sessions.add_argument("--provider", choices=["all", "openai", "litellm"], default="all")
    sessions.add_argument("--last", type=int, default=100)
    sessions.add_argument("--archived", action="store_true")
    resume = sub.add_parser("codex-resume")
    resume.add_argument("session_id")

    prepare = sub.add_parser("prepare")
    prepare.add_argument("--profile", choices=PROFILE_CHOICES, default="gpt56")
    prepare.add_argument("--confirm", required=True)

    install = sub.add_parser("install-runtime")
    install.add_argument("--confirm", required=True)
    sub.add_parser("install-status")

    oauth = sub.add_parser("oauth")
    oauth.add_argument("--confirm", required=True)
    sub.add_parser("oauth-status")

    probe = sub.add_parser("probe")
    probe.add_argument("--profile", choices=PROFILE_CHOICES, default=None)
    probe.add_argument("--phase", choices=["phase2", "phase3", "phase3a"], default=None)
    probe.add_argument("--confirm", required=True)
    probe_status = sub.add_parser("probe-status")
    probe_status.add_argument("--profile", choices=PROFILE_CHOICES, default=None)
    probe_status.add_argument("--phase", choices=["phase2", "phase3", "phase3a"], default=None)

    configure = sub.add_parser("configure-codex")
    configure.add_argument("--phase", choices=["phase2", "phase3", "phase3a"], default=None)
    configure.add_argument("--confirm", required=True)

    codex_mode = sub.add_parser("codex-mode")
    codex_mode_sub = codex_mode.add_subparsers(dest="codex_mode_command", required=True)
    codex_mode_sub.add_parser("status")
    codex_mode_smart = codex_mode_sub.add_parser("smart")
    codex_mode_smart.add_argument("--confirm", required=True)
    codex_mode_smart.add_argument("--allow-unverified", action="store_true")
    codex_mode_native = codex_mode_sub.add_parser("native")
    codex_mode_native.add_argument("--confirm", required=True)

    proxy_start = sub.add_parser("proxy-start")
    proxy_start.add_argument("--allow-unverified", action="store_true")
    proxy_start.add_argument("--confirm", required=True)
    sub.add_parser("proxy-status")
    proxy_stop = sub.add_parser("proxy-stop")
    proxy_stop.add_argument("--confirm", required=True)
    proxy_restart = sub.add_parser("proxy-restart")
    proxy_restart.add_argument("--allow-unverified", action="store_true")
    proxy_restart.add_argument("--confirm", required=True)

    phase2 = sub.add_parser("phase2")
    phase2.add_argument("--profile", choices=PROFILE_CHOICES, default=None)
    phase2.add_argument("--confirm", required=True)
    phase3 = sub.add_parser("phase3")
    phase3.add_argument("--profile", choices=PROFILE_CHOICES, default=None)
    phase3.add_argument("--confirm", required=True)
    phase3a_prepare = sub.add_parser("phase3a-prepare")
    phase3a_prepare.add_argument("--profile", choices=PROFILE_CHOICES, default="gpt56")
    phase3a_prepare.add_argument("--confirm", required=True)
    phase3a_activate = sub.add_parser("phase3a-activate")
    phase3a_activate.add_argument("--profile", choices=PROFILE_CHOICES, default=None)
    phase3a_activate.add_argument("--confirm", required=True)
    phase3a_recover = sub.add_parser("phase3a-recover")
    phase3a_recover.add_argument("--confirm", required=True)

    sub.add_parser("adaptive-status")
    adaptive_import = sub.add_parser("adaptive-import-tasks")
    adaptive_import.add_argument("--confirm", required=True)
    adaptive_evaluate = sub.add_parser("adaptive-evaluate")
    adaptive_evaluate.add_argument("task")
    adaptive_evaluate.add_argument("--repo-path", default="")
    adaptive_shadow = sub.add_parser("adaptive-shadow")
    adaptive_shadow.add_argument("--confirm", required=True)
    adaptive_canary = sub.add_parser("adaptive-canary")
    adaptive_canary.add_argument("--percent", type=int, required=True)
    adaptive_canary.add_argument("--confirm", required=True)
    adaptive_disable = sub.add_parser("adaptive-disable")
    adaptive_disable.add_argument("--confirm", required=True)

    phase21_prepare = sub.add_parser("phase21-prepare")
    phase21_prepare.add_argument("--profile", choices=PROFILE_CHOICES, default="gpt56")
    phase21_prepare.add_argument("--confirm", required=True)
    phase21_activate = sub.add_parser("phase21-activate")
    phase21_activate.add_argument("--profile", choices=PROFILE_CHOICES, default=None)
    phase21_activate.add_argument("--confirm", required=True)

    logs = sub.add_parser("logs")
    logs.add_argument("--component", choices=["proxy", "oauth", "probe", "install"], default="proxy")
    logs.add_argument("--lines", type=int, default=100)

    report = sub.add_parser("report")
    report.add_argument("--days", type=int, default=30)
    report.add_argument("--unit", choices=["turn", "call"], default="turn")

    calls = sub.add_parser("calls")
    calls.add_argument("--last", type=int, default=20)
    turns = sub.add_parser("turns")
    turns.add_argument("--last", type=int, default=20)
    explain = sub.add_parser("explain")
    explain.add_argument("--turn-id", required=True)

    feedback = sub.add_parser("feedback")
    feedback_scope = feedback.add_mutually_exclusive_group(required=True)
    feedback_scope.add_argument("--fingerprint")
    feedback_scope.add_argument("--turn-id")
    feedback.add_argument("--expected-tier", choices=["SIMPLE", "MEDIUM", "COMPLEX", "REASONING"], required=True)
    feedback.add_argument("--outcome", choices=["success", "partial", "failure"], required=True)
    feedback.add_argument("--note", default="")

    sub.add_parser("backups")
    restore = sub.add_parser("restore-codex")
    restore.add_argument("backup_path")
    restore.add_argument("--confirm", required=True)

    task_run = sub.add_parser("task-run")
    task_run.add_argument("repo_path")
    task_run.add_argument("task")
    task_run.add_argument("--test-profile", choices=["none", "auto", "python", "node", "go", "rust"], default="auto")
    task_run.add_argument("--max-escalations", type=int, choices=[0, 1], default=1)
    task_run.add_argument("--allow-dirty", action="store_true")
    task_run.add_argument("--confirm", required=True)
    task_status = sub.add_parser("task-status")
    task_status.add_argument("task_id")
    task_list = sub.add_parser("task-list")
    task_list.add_argument("--limit", type=int, default=20)
    task_stop = sub.add_parser("task-stop")
    task_stop.add_argument("task_id")
    task_stop.add_argument("--confirm", required=True)

    return parser


READ_ONLY_COMMANDS = frozenset({
    "guide",
    "status",
    "environment",
    "install-status",
    "oauth-status",
    "probe-status",
    "proxy-status",
    "logs",
    "report",
    "calls",
    "turns",
    "explain",
    "backups",
    "task-status",
    "task-list",
    "adaptive-status",
    "adaptive-evaluate",
    "codex-sessions",
})
NO_WORKSPACE_INIT_COMMANDS = READ_ONLY_COMMANDS | frozenset({"feedback", "codex-resume"})


def dispatch(args: argparse.Namespace) -> Any:
    rpc = _rpc_request(args)
    if rpc is not None and not args.direct:
        method, params = rpc
        return IpcClient(WorkspacePaths.discover().daemon_socket).call(method, params)
    if args.command == "codex-sessions":
        return list_codex_sessions(
            WorkspacePaths.default(),
            limit=args.last,
            provider=args.provider,
            archived=args.archived,
        )
    if args.command == "codex-resume":
        return launch_codex_session(WorkspacePaths.default(), args.session_id)
    controller = RouterController(
        ensure_paths=args.command not in NO_WORKSPACE_INIT_COMMANDS,
    )
    cmd = args.command
    if cmd == "guide":
        return controller.setup_guide()
    if cmd == "status":
        return controller.full_status()
    if cmd == "environment":
        return controller.environment_check()
    if cmd == "prepare":
        return controller.prepare_configs(profile=args.profile, confirm=args.confirm)
    if cmd == "install-runtime":
        return controller.start_runtime_install(confirm=args.confirm)
    if cmd == "install-status":
        return controller.runtime_install_status()
    if cmd == "oauth":
        return controller.begin_oauth(confirm=args.confirm)
    if cmd == "oauth-status":
        return controller.oauth_status()
    if cmd == "probe":
        return controller.start_model_probe(
            profile=args.profile,
            phase=args.phase,
            confirm=args.confirm,
        )
    if cmd == "probe-status":
        return controller.model_probe_status(profile=args.profile, phase=args.phase)
    if cmd == "configure-codex":
        return controller.configure_codex(phase=args.phase, confirm=args.confirm)
    if cmd == "codex-mode":
        if args.codex_mode_command == "status":
            return controller.codex_mode_status()
        if args.codex_mode_command == "smart":
            return controller.enable_smart_routing(
                confirm=args.confirm,
                allow_unverified=args.allow_unverified,
            )
        if args.codex_mode_command == "native":
            return controller.switch_to_native_codex(confirm=args.confirm)
    if cmd == "proxy-start":
        return controller.start_proxy(confirm=args.confirm, allow_unverified=args.allow_unverified)
    if cmd == "proxy-status":
        return controller.proxy_status()
    if cmd == "proxy-stop":
        return controller.stop_proxy(confirm=args.confirm)
    if cmd == "proxy-restart":
        return controller.restart_proxy(confirm=args.confirm, allow_unverified=args.allow_unverified)
    if cmd in {"phase2", "phase3"}:
        return controller.activate_phase(phase=cmd, profile=args.profile, confirm=args.confirm)
    if cmd == "phase21-prepare":
        return controller.prepare_phase21_candidate(profile=args.profile, confirm=args.confirm)
    if cmd == "phase21-activate":
        return controller.activate_phase21(profile=args.profile, confirm=args.confirm)
    if cmd == "phase3a-prepare":
        return controller.prepare_phase3a_candidate(profile=args.profile, confirm=args.confirm)
    if cmd == "phase3a-activate":
        return controller.activate_phase3a(profile=args.profile, confirm=args.confirm)
    if cmd == "phase3a-recover":
        return controller.rollback_pending_phase3a(confirm=args.confirm)
    if cmd == "adaptive-status":
        return controller.adaptive_status()
    if cmd == "adaptive-import-tasks":
        return controller.adaptive_import_tasks(confirm=args.confirm)
    if cmd == "adaptive-evaluate":
        return controller.adaptive_evaluate(task=args.task, repo_path=args.repo_path)
    if cmd == "adaptive-shadow":
        return controller.enable_adaptive_shadow(confirm=args.confirm)
    if cmd == "adaptive-canary":
        return controller.enable_adaptive_canary(percent=args.percent, confirm=args.confirm)
    if cmd == "adaptive-disable":
        return controller.disable_adaptive(confirm=args.confirm)
    if cmd == "logs":
        return controller.logs(component=args.component, lines=args.lines)
    if cmd == "report":
        return controller.routing_report(days=args.days, unit=args.unit)
    if cmd == "calls":
        return controller.recent_calls(limit=args.last)
    if cmd == "turns":
        return controller.recent_turns(limit=args.last)
    if cmd == "explain":
        return controller.explain_turn(turn_id=args.turn_id)
    if cmd == "feedback":
        return controller.record_feedback(
            prompt_fingerprint=args.fingerprint,
            logical_turn_id=args.turn_id,
            expected_tier=args.expected_tier,
            outcome=args.outcome,
            note=args.note,
        )
    if cmd == "backups":
        return controller.list_backups()
    if cmd == "restore-codex":
        return controller.restore_codex_config(backup_path=args.backup_path, confirm=args.confirm)
    if cmd == "task-run":
        return controller.start_codex_task(
            repo_path=args.repo_path,
            task=args.task,
            test_profile=args.test_profile,
            max_escalations=args.max_escalations,
            allow_dirty=args.allow_dirty,
            confirm=args.confirm,
        )
    if cmd == "task-status":
        return controller.codex_task_status(task_id=args.task_id)
    if cmd == "task-list":
        return controller.list_codex_tasks(limit=args.limit)
    if cmd == "task-stop":
        return controller.stop_codex_task(task_id=args.task_id, confirm=args.confirm)
    raise ControlError(f"unsupported command: {cmd}")


def _rpc_request(args: argparse.Namespace) -> tuple[str, dict[str, Any]] | None:
    cmd = args.command
    if cmd == "guide":
        return "system.doctor", {}
    if cmd == "status":
        return "system.status", {}
    if cmd == "environment":
        return "system.environment", {}
    if cmd == "prepare":
        return "runtime.prepare", {"profile": args.profile, "confirm": args.confirm}
    if cmd == "install-runtime":
        return "runtime.install", {"confirm": args.confirm}
    if cmd == "install-status":
        return "runtime.installStatus", {}
    if cmd == "oauth":
        return "subscription.oauth.begin", {"confirm": args.confirm}
    if cmd == "oauth-status":
        return "subscription.oauth.status", {}
    if cmd == "proxy-status":
        return "proxy.status", {}
    if cmd in {"proxy-start", "proxy-stop", "proxy-restart"}:
        method = cmd.replace("-", ".")
        params = {"confirm": args.confirm}
        if hasattr(args, "allow_unverified"):
            params["allow_unverified"] = args.allow_unverified
        return method, params
    if cmd == "codex-mode":
        if args.codex_mode_command == "status":
            return "codex.status", {}
        if args.codex_mode_command == "native":
            return "codex.switchNative", {"confirm": args.confirm}
        return "codex.switchSmart", {
            "confirm": args.confirm,
            "allow_unverified": args.allow_unverified,
        }
    if cmd == "probe":
        return "models.probe", {
            "profile": args.profile,
            "phase": args.phase,
            "confirm": args.confirm,
        }
    if cmd == "probe-status":
        return "models.probeStatus", {"profile": args.profile, "phase": args.phase}
    if cmd == "configure-codex":
        return "codex.configure", {"phase": args.phase, "confirm": args.confirm}
    if cmd in {"phase2", "phase3"}:
        return "routing.activatePhase", {
            "phase": cmd,
            "profile": args.profile,
            "confirm": args.confirm,
        }
    if cmd == "phase21-prepare":
        return "routing.phase21.prepare", {"profile": args.profile, "confirm": args.confirm}
    if cmd == "phase21-activate":
        return "routing.phase21.activate", {"profile": args.profile, "confirm": args.confirm}
    if cmd == "phase3a-prepare":
        return "routing.phase3a.prepare", {"profile": args.profile, "confirm": args.confirm}
    if cmd == "phase3a-activate":
        return "routing.phase3a.activate", {"profile": args.profile, "confirm": args.confirm}
    if cmd == "phase3a-recover":
        return "routing.phase3a.recover", {"confirm": args.confirm}
    if cmd == "adaptive-status":
        return "routing.status", {}
    if cmd == "adaptive-import-tasks":
        return "routing.adaptive.import", {"confirm": args.confirm}
    if cmd == "adaptive-evaluate":
        return "routing.adaptive.evaluate", {"task": args.task, "repo_path": args.repo_path}
    if cmd == "adaptive-shadow":
        return "routing.adaptive.shadow", {"confirm": args.confirm}
    if cmd == "adaptive-canary":
        return "routing.adaptive.canary", {"percent": args.percent, "confirm": args.confirm}
    if cmd == "adaptive-disable":
        return "routing.adaptive.disable", {"confirm": args.confirm}
    if cmd == "logs":
        return "logs.tail", {"component": args.component, "lines": args.lines}
    if cmd == "report":
        return "reports.summary", {"days": args.days, "unit": args.unit}
    if cmd == "calls":
        return "reports.calls", {"limit": args.last}
    if cmd == "turns":
        return "reports.turns", {"limit": args.last}
    if cmd == "explain":
        return "reports.explain", {"turn_id": args.turn_id}
    if cmd == "feedback":
        return "reports.feedback", {
            "prompt_fingerprint": args.fingerprint,
            "logical_turn_id": args.turn_id,
            "expected_tier": args.expected_tier,
            "outcome": args.outcome,
            "note": args.note,
        }
    if cmd == "backups":
        return "codex.backups", {}
    if cmd == "restore-codex":
        return "codex.restoreBackup", {"backup_path": args.backup_path, "confirm": args.confirm}
    if cmd == "task-run":
        return "tasks.run", {
            "repo_path": args.repo_path,
            "task": args.task,
            "test_profile": args.test_profile,
            "max_escalations": args.max_escalations,
            "allow_dirty": args.allow_dirty,
            "confirm": args.confirm,
        }
    if cmd == "task-status":
        return "tasks.status", {"task_id": args.task_id}
    if cmd == "task-list":
        return "reports.tasks", {"limit": args.limit}
    if cmd == "task-stop":
        return "tasks.stop", {"task_id": args.task_id, "confirm": args.confirm}
    return None


def main() -> int:
    args = _parser().parse_args()
    try:
        _print(dispatch(args))
        return 0
    except (ControlError, ValueError) as exc:
        _print({"ok": False, "error": str(exc), "error_type": type(exc).__name__})
        return 2
    except KeyboardInterrupt:
        _print({"ok": False, "error": "interrupted"})
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
