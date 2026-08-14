from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import psutil

from ..errors import ControlError


def which(name: str) -> str | None:
    return shutil.which(name)


def normalized_child_env(source: dict[str, str] | None = None) -> dict[str, str]:
    """Return a child environment with a locale supported by the host OS."""
    env = dict(os.environ if source is None else source)
    if sys.platform == "darwin" and env.get("LANG") == "C.UTF-8":
        env["LANG"] = "C"
        env["LC_ALL"] = "C"
    return env


def pid_from_file(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError, OSError):
        return None


def process_info(pid: int | None) -> dict[str, Any]:
    if not pid:
        return {"running": False, "pid": None}
    try:
        proc = psutil.Process(pid)
        return {
            "running": proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE,
            "pid": pid,
            "status": proc.status(),
            "create_time": proc.create_time(),
            "cmdline": proc.cmdline(),
        }
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return {"running": False, "pid": pid}


def ensure_expected_process(pid: int, required_fragments: Iterable[str]) -> psutil.Process:
    try:
        proc = psutil.Process(pid)
        cmdline = " ".join(proc.cmdline())
    except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
        raise ControlError(f"进程 {pid} 不存在或无权访问。") from exc
    missing = [fragment for fragment in required_fragments if fragment not in cmdline]
    if missing:
        raise ControlError(
            f"拒绝终止 PID {pid}：进程命令行与受管服务不匹配，缺少 {missing}。"
        )
    return proc


def stop_process_tree(pid: int, required_fragments: Iterable[str], timeout: float = 8.0) -> None:
    proc = ensure_expected_process(pid, required_fragments)
    try:
        children = proc.children(recursive=True)
    except PermissionError:
        # macOS sandboxing can permit inspection of the managed root process
        # while denying psutil's system-wide child enumeration. The command
        # line check above still protects the only PID we terminate.
        children = []
    if os.name != "nt":
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            proc.terminate()
    else:
        for child in children:
            child.terminate()
        proc.terminate()
    try:
        _, alive = psutil.wait_procs([proc, *children], timeout=timeout)
    except PermissionError:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not proc.is_running():
                return
            time.sleep(0.1)
        alive = [proc] if proc.is_running() else []
    if alive:
        for item in alive:
            try:
                item.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        psutil.wait_procs(alive, timeout=3)


def force_kill_process(pid: int) -> None:
    """Immediately terminate a listener whose PID was discovered from the port."""
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError as exc:
        raise ControlError(f"无权终止 listener PID {pid}。") from exc
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if not process_info(pid).get("running"):
            return
        time.sleep(0.1)
    if process_info(pid).get("running"):
        raise ControlError(f"listener PID {pid} 已发送 kill，但进程仍在运行。")


def is_port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def wait_for_port(host: str, port: int, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_port_open(host, port):
            return True
        time.sleep(0.25)
    return False


def wait_for_port_closed(host: str, port: int, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_port_open(host, port):
            return True
        time.sleep(0.25)
    return not is_port_open(host, port)


def listening_pid(host: str, port: int, required_fragments: Iterable[str] = ()) -> int | None:
    """Return a verified listener PID without trusting a short-lived launcher PID."""
    try:
        connections = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, PermissionError):
        return _listening_pid_with_lsof(host, port)
    for connection in connections:
        local = connection.laddr
        if not local or local.port != port or connection.status != psutil.CONN_LISTEN:
            continue
        if host and local.ip not in {host, "0.0.0.0", "::"}:
            continue
        if not connection.pid:
            continue
        info = process_info(connection.pid)
        command = " ".join(info.get("cmdline") or [])
        if info.get("running") and all(fragment in command for fragment in required_fragments):
            return connection.pid
    return None


def _listening_pid_with_lsof(host: str, port: int) -> int | None:
    """Use macOS lsof when sandbox policy blocks psutil connection enumeration."""
    if sys.platform != "darwin":
        return None
    try:
        output = subprocess.run(
            ["/usr/sbin/lsof", "-nP", f"-iTCP@{host}:{port}", "-sTCP:LISTEN", "-t"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in output.stdout.splitlines():
        try:
            pid = int(line.strip())
        except ValueError:
            continue
        if pid > 0 and process_info(pid).get("running"):
            return pid
    return None


def spawn_detached(
    command: list[str],
    *,
    log_path: Path,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.Popen[bytes]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = open(log_path, "ab", buffering=0)
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
        "cwd": str(cwd) if cwd else None,
        "env": env,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(command, **kwargs)
    finally:
        log_handle.close()
