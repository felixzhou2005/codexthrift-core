from __future__ import annotations

import argparse
import json
import os
import signal
import threading
import time
from pathlib import Path

import uvicorn

from .dashboard_api import create_dashboard_app
from .codex_clients import detect_codex_clients
from .codex_config import switch_to_native_codex
from .codex_mode import CodexClientMode, record_mode_switch
from .key_reader import main as print_codex_proxy_token
from .paths import WorkspacePaths
from .util import ControlError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local RouterLLM dashboard service.")
    parser.add_argument(
        "--print-codex-proxy-token",
        action="store_true",
        help="Print the local Proxy bearer token for Codex command-backed authentication.",
    )
    parser.add_argument(
        "--force-native",
        action="store_true",
        help="Force Codex back to the native OpenAI provider without RouterD or Proxy checks.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4876)
    parser.add_argument(
        "--token-file",
        type=Path,
        help="Read the dashboard access token from this file.",
    )
    parser.add_argument(
        "--insecure-development",
        action="store_true",
        help="Allow local requests without ROUTERLLM_DASHBOARD_TOKEN. Development only.",
    )
    parser.add_argument("--build-id", default=os.environ.get("ROUTERLLM_BUILD_ID", "dev"))
    parser.add_argument(
        "--parent-pid",
        type=int,
        help="Exit RouterD when the managing desktop process is no longer running.",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.print_codex_proxy_token:
        return print_codex_proxy_token()
    if args.force_native:
        try:
            result = _force_native_fallback(host="127.0.0.1", proxy_port=4000)
        except ControlError as error:
            print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
            return 1
        print(json.dumps(result, ensure_ascii=False))
        return 0
    token = os.environ.get("ROUTERLLM_DASHBOARD_TOKEN") or _token_from_file(args.token_file)
    if not token and not args.insecure_development:
        raise SystemExit("A dashboard token or token file is required outside development mode.")
    if args.parent_pid is not None:
        _start_parent_watchdog(args.parent_pid)
    app = create_dashboard_app(access_token=token, build_id=args.build_id)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def _force_native_fallback(*, host: str, proxy_port: int) -> dict[str, object]:
    paths = WorkspacePaths.default()
    try:
        observed = detect_codex_clients()
    except (OSError, PermissionError):
        observed = {
            "clients": [],
            "counts": {},
            "detection_status": "limited",
        }
    switched = switch_to_native_codex(paths, host, proxy_port)
    verification = {
        "status": "forced_without_online_verification",
        "verified": False,
        "native_auth_state": "unknown",
    }
    state = record_mode_switch(
        paths,
        CodexClientMode.NATIVE_OPENAI,
        observed_clients=observed.get("clients") or [],
        verification=verification,
    )
    stale_client_count = len(observed.get("clients") or [])
    return {
        "ok": True,
        "forced": True,
        "stage": "config_written",
        "switch": switched,
        "generation": state["generation"],
        "mode": {
            "client_mode": CodexClientMode.NATIVE_OPENAI.value,
            "effective_provider": "openai",
            "new_session_mode": "native",
            "proxy_used_by_codex": False,
            "proxy_route_state": "standby",
            "stale_client_count": stale_client_count,
            "restart_required": stale_client_count > 0 or observed.get("detection_status") == "limited",
            "verification": verification,
        },
        "rollback": {"performed": False, "reason": "forced_fallback"},
        "notice": "Codex 配置已强制切回原生 OpenAI；请重启 Codex CLI 或重新加载 VS Code 后新建会话。",
    }


def _token_from_file(explicit_path: Path | None = None) -> str | None:
    raw_path = explicit_path or os.environ.get("ROUTERLLM_DASHBOARD_TOKEN_FILE")
    if not raw_path:
        return None
    try:
        token = Path(raw_path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None


def _process_exists(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _watch_parent(parent_pid: int, interval_seconds: float = 1.0) -> None:
    while True:
        time.sleep(interval_seconds)
        if _process_exists(parent_pid):
            continue
        os.kill(os.getpid(), signal.SIGTERM)
        return


def _start_parent_watchdog(parent_pid: int) -> threading.Thread:
    if not _process_exists(parent_pid):
        raise SystemExit("The managing RouterLLM Desk process is not running.")
    thread = threading.Thread(
        target=_watch_parent,
        args=(parent_pid,),
        name="routerd-parent-watchdog",
        daemon=True,
    )
    thread.start()
    return thread


if __name__ == "__main__":
    raise SystemExit(main())
