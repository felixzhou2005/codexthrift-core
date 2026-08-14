from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import psutil

from .codex_mode import read_mode_state
from .paths import WorkspacePaths

_VSCODE_EXTENSION_RE = re.compile(
    r"^openai\.chatgpt-(?P<version>\d+(?:\.\d+)+)-(?P<platform>[^/]+)$"
)


def classify_codex_process(cmdline: list[str], parent_names: list[str]) -> str | None:
    if not cmdline:
        return None
    executable = Path(cmdline[0]).name.lower()
    joined = " ".join(cmdline).lower()
    if executable not in {"codex", "codex.exe"}:
        return None
    if "app-server" in joined:
        parents = " ".join(parent_names).lower()
        if any(marker in parents for marker in ("visual studio code", "code helper", "cursor")):
            return "vscode_app_server"
        return "codex_app_server"
    if "sandbox" in (argument.lower() for argument in cmdline[1:]):
        return None
    return "codex_cli"


def command_fingerprint(cmdline: list[str]) -> str:
    """Identify a command without persisting its potentially sensitive arguments."""
    payload = "\0".join(cmdline).encode("utf-8", errors="surrogatepass")
    return hashlib.sha256(payload).hexdigest()


def codex_distribution(executable_path: str) -> dict[str, Any]:
    """Describe the host distribution without reading command arguments."""
    path = Path(executable_path)
    for parent in path.parents:
        matched = _VSCODE_EXTENSION_RE.match(parent.name)
        if matched is not None:
            version = matched.group("version")
            installed_versions = _installed_vscode_extension_versions(parent.parent)
            latest = max(installed_versions, key=_version_key) if installed_versions else version
            return {
                "distribution": "vscode_extension",
                "client_version": version,
                "latest_installed_version": latest,
                "update_available": _version_key(version) < _version_key(latest),
                "recommended_action": (
                    "reload_vscode_window"
                    if _version_key(version) < _version_key(latest)
                    else "reload_host_after_mode_switch"
                ),
            }
    if "/Applications/ChatGPT.app/" in executable_path:
        return {
            "distribution": "codex_desktop",
            "client_version": None,
            "latest_installed_version": None,
            "update_available": False,
            "recommended_action": "restart_codex_desktop_after_mode_switch",
        }
    return {
        "distribution": "codex_cli",
        "client_version": None,
        "latest_installed_version": None,
        "update_available": False,
        "recommended_action": "restart_cli_after_mode_switch",
    }


def _installed_vscode_extension_versions(extensions_dir: Path) -> list[str]:
    try:
        entries = extensions_dir.iterdir()
    except (OSError, PermissionError):
        return []
    versions: list[str] = []
    for entry in entries:
        matched = _VSCODE_EXTENSION_RE.match(entry.name)
        if matched is not None:
            versions.append(matched.group("version"))
    return versions


def _version_key(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return ()


def codex_client_identity(pid: int) -> dict[str, Any] | None:
    identity, _ = _codex_client_identity(pid)
    return identity


def _codex_client_identity(pid: int) -> tuple[dict[str, Any] | None, bool]:
    try:
        process = psutil.Process(int(pid))
        cmdline = [str(item) for item in process.cmdline()]
        parent_names = _parent_names(process.ppid())
        kind = classify_codex_process(cmdline, parent_names)
        if kind is None:
            return None, False
        has_config_override = "--profile" in cmdline or "--config" in cmdline
        distribution = codex_distribution(cmdline[0])
        return ({
            "pid": int(process.pid),
            "create_time": float(process.create_time()),
            "kind": kind,
            "command_fingerprint": command_fingerprint(cmdline),
            "executable_path": cmdline[0],
            **distribution,
            "has_config_override": has_config_override,
            "eligible_for_termination": not has_config_override,
            "restart_behavior": (
                "user_relaunch_required"
                if kind == "codex_cli"
                else "host_managed_respawn"
            ),
        }, False)
    except (psutil.AccessDenied, PermissionError):
        return None, True
    except (
        psutil.NoSuchProcess,
        psutil.ZombieProcess,
        OSError,
        TypeError,
        ValueError,
    ):
        return None, False


def detect_codex_clients(*, exclude_pids: Iterable[int] = ()) -> dict[str, Any]:
    excluded = set(exclude_pids)
    clients: list[dict[str, Any]] = []
    permission_limited = False
    try:
        processes = psutil.process_iter(["pid", "name"])
    except (OSError, psutil.Error):
        processes = ()
        permission_limited = True
    for process in processes:
        try:
            pid = int(process.info["pid"])
            if pid in excluded:
                continue
            process_name = str(process.info.get("name") or "").lower()
            if process_name and process_name not in {"codex", "codex.exe"}:
                continue
            identity, identity_limited = _codex_client_identity(pid)
            permission_limited = permission_limited or identity_limited
            if identity is None:
                continue
            clients.append(identity)
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, TypeError, ValueError):
            permission_limited = True
    counts = {
        kind: sum(1 for item in clients if item["kind"] == kind)
        for kind in ("codex_cli", "codex_app_server", "vscode_app_server")
    }
    return {
        "clients": sorted(clients, key=lambda item: (item["kind"], item["pid"])),
        "counts": counts,
        "detection_status": "limited" if permission_limited else "available",
    }


def restart_status(paths: WorkspacePaths, current: dict[str, Any]) -> dict[str, Any]:
    state = read_mode_state(paths)
    observed = state.get("observed_clients") if isinstance(state, dict) else []
    observed = observed if isinstance(observed, list) else []
    current_clients = current.get("clients") if isinstance(current, dict) else []
    current_clients = current_clients if isinstance(current_clients, list) else []
    current_keys = {
        (item.get("pid"), item.get("create_time"))
        for item in current_clients
        if isinstance(item, dict)
    }
    stale = [
        item
        for item in observed
        if isinstance(item, dict) and (item.get("pid"), item.get("create_time")) in current_keys
    ]
    limited = current.get("detection_status") == "limited"
    return {
        "restart_required": bool(stale) or limited,
        "stale_clients": stale,
        "detection_status": current.get("detection_status", "unavailable"),
        "counts": current.get("counts") or {},
        "generation": state.get("generation") if isinstance(state, dict) else None,
        "switched_at": state.get("switched_at") if isinstance(state, dict) else None,
    }


def _parent_names(ppid: Any) -> list[str]:
    names: list[str] = []
    try:
        process = psutil.Process(int(ppid))
        for _ in range(5):
            names.append(process.name())
            parent = process.parent()
            if parent is None:
                break
            process = parent
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, TypeError, ValueError):
        return names
    return names
