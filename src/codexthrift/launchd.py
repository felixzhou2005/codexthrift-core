from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path
from typing import Any

from .util import ControlError, atomic_write_text

LAUNCH_AGENT_LABEL = "com.rifo.codexthrift.core"


def launch_agent_definition(
    *,
    executable: str,
    working_directory: Path,
    log_path: Path,
) -> dict[str, Any]:
    return {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [executable],
        "WorkingDirectory": str(working_directory),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "ProcessType": "Background",
        "EnvironmentVariables": {"CODEX_ROUTER_HOME": str(working_directory)},
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }


def render_launch_agent(
    *,
    executable: str,
    working_directory: Path,
    log_path: Path,
) -> bytes:
    return plistlib.dumps(
        launch_agent_definition(
            executable=executable,
            working_directory=working_directory,
            log_path=log_path,
        ),
        fmt=plistlib.FMT_XML,
        sort_keys=True,
    )


def install_launch_agent(
    *,
    plist_path: Path,
    executable: str,
    working_directory: Path,
    log_path: Path,
) -> dict[str, Any]:
    if os.uname().sysname != "Darwin":
        raise ControlError("LaunchAgent 仅支持 macOS。")
    if not Path(executable).is_absolute():
        raise ControlError("LaunchAgent 必须使用绝对可执行文件路径。")
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(plist_path, render_launch_agent(
        executable=executable,
        working_directory=working_directory,
        log_path=log_path,
    ).decode("utf-8"), mode=0o600)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(plist_path)], check=False, capture_output=True)
    completed = subprocess.run(
        ["launchctl", "bootstrap", domain, str(plist_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        raise ControlError(f"LaunchAgent 安装失败：{completed.stderr.strip()[:500]}")
    return {"ok": True, "label": LAUNCH_AGENT_LABEL, "plist_path": str(plist_path)}


def uninstall_launch_agent(*, plist_path: Path) -> dict[str, Any]:
    if os.uname().sysname != "Darwin":
        raise ControlError("LaunchAgent 仅支持 macOS。")
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(plist_path)], check=False, capture_output=True)
    plist_path.unlink(missing_ok=True)
    return {"ok": True, "label": LAUNCH_AGENT_LABEL, "plist_path": str(plist_path)}
