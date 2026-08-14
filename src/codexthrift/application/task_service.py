from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from ..errors import ControlError
from ..platform.process import (
    pid_from_file,
    process_info,
    spawn_detached,
    stop_process_tree,
    which,
)
from ..security.path_guard import require_confirmation, safe_home_path
from ..storage.atomic_io import (
    atomic_write_json,
    atomic_write_text,
    read_json,
    tail_text,
    utc_now_iso,
)
from .base import BaseApplicationService


class TaskService(BaseApplicationService):
    def start_codex_task(
        self,
        *,
        repo_path: str,
        task: str,
        test_profile: str = "auto",
        max_escalations: int = 1,
        allow_dirty: bool = False,
        confirm: str,
    ) -> dict[str, Any]:
        require_confirmation(confirm, "RUN_CODEX_DIRTY" if allow_dirty else "RUN_CODEX")
        if self.active_phase() not in {"phase3", "phase3a"}:
            raise ControlError("任务级 effort/失败升级要求 phase3 或 phase3a 已启用。")
        if not self._probe_is_ready(self.active_phase(), self.active_profile()):
            raise ControlError(f"{self.active_phase()} 当前 profile 尚未通过对应模型探测。")
        if not self.proxy_status().get("running"):
            raise ControlError("LiteLLM Proxy 未运行。")
        repo = safe_home_path(repo_path)
        if not (repo / ".git").exists():
            raise ControlError("为降低误操作风险，任务目录必须是 Git 仓库根目录。")
        git = which("git")
        if not git:
            raise ControlError("未找到 git 命令。")
        try:
            root_check = subprocess.run(
                [git, "-C", str(repo), "rev-parse", "--show-toplevel"],
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
            actual_root = Path(root_check.stdout.strip()).resolve() if root_check.returncode == 0 else None
            if actual_root != repo:
                raise ControlError("repo_path 必须精确指向 Git 仓库根目录。")
            status_check = subprocess.run(
                [git, "-C", str(repo), "status", "--porcelain=v1"],
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ControlError(f"无法检查 Git 工作区：{exc}") from exc
        if status_check.returncode != 0:
            raise ControlError(f"无法读取 Git 状态：{status_check.stderr[-1000:]}")
        dirty = bool(status_check.stdout.strip())
        if dirty and not allow_dirty:
            raise ControlError(
                "Git 工作区存在未提交更改。请先提交/暂存到安全位置，或设置 allow_dirty=true 并使用 confirm='RUN_CODEX_DIRTY'。"
            )
        if not task.strip():
            raise ControlError("task 不能为空。")
        if len(task) > 20_000:
            raise ControlError("task 过长；请控制在 20,000 字符以内。")
        allowed_profiles = {"none", "auto", "python", "node", "go", "rust"}
        if test_profile not in allowed_profiles:
            raise ControlError(f"test_profile 必须是 {sorted(allowed_profiles)}")
        escalations = max(0, min(int(max_escalations), 1))
        task_id = datetime_id() + "-" + uuid.uuid4().hex[:8]
        run_dir = self.paths.task_runs_dir / task_id
        run_dir.mkdir(parents=True, exist_ok=False)
        spec = {
            "task_id": task_id,
            "repo_path": str(repo),
            "task": task,
            "test_profile": test_profile,
            "max_escalations": escalations,
            "phase": self.active_phase(),
            "allow_dirty": allow_dirty,
            "dirty_at_start": dirty,
            "created_at": utc_now_iso(),
        }
        spec_path = run_dir / "spec.json"
        atomic_write_json(spec_path, spec, mode=0o600)
        result_path = run_dir / "result.json"
        log_path = run_dir / "runner.log"
        command = [
            sys.executable,
            "-m",
            "codexthrift.task_runner",
            str(spec_path),
            str(result_path),
        ]
        env = os.environ.copy()
        env["CODEX_ROUTER_HOME"] = str(self.paths.root)
        proc = spawn_detached(command, log_path=log_path, env=env, cwd=repo)
        atomic_write_text(run_dir / "pid", f"{proc.pid}\n", mode=0o600)
        return {
            "ok": True,
            "task_id": task_id,
            "pid": proc.pid,
            "repo_path": str(repo),
            "test_profile": test_profile,
            "max_escalations": escalations,
            "dirty_at_start": dirty,
            "status_tool": "codex_task_status",
        }

    def list_codex_tasks(self, *, limit: int = 20) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for run_dir in sorted(
            (path for path in self.paths.task_runs_dir.iterdir() if path.is_dir()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[: max(1, min(int(limit), 100))]:
            pid = pid_from_file(run_dir / "pid")
            result = read_json(run_dir / "result.json")
            spec = read_json(run_dir / "spec.json")
            rows.append({
                "task_id": run_dir.name,
                "running": process_info(pid).get("running", False),
                "pid": pid,
                "repo_path": spec.get("repo_path") if isinstance(spec, dict) else None,
                "created_at": spec.get("created_at") if isinstance(spec, dict) else None,
                "success": result.get("success") if isinstance(result, dict) else None,
                "final_model": result.get("final_model") if isinstance(result, dict) else None,
            })
        return {"tasks": rows}

    def stop_codex_task(self, *, task_id: str, confirm: str) -> dict[str, Any]:
        require_confirmation(confirm, "STOP_CODEX")
        run_dir = self._task_run_dir(task_id)
        pid = pid_from_file(run_dir / "pid")
        if not pid or not process_info(pid).get("running"):
            return {"ok": True, "already_stopped": True, "task_id": task_id}
        stop_process_tree(
            pid,
            required_fragments=["codexthrift.task_runner", str(run_dir / "spec.json")],
            timeout=10,
        )
        previous = read_json(run_dir / "result.json", {})
        if not isinstance(previous, dict):
            previous = {}
        previous.update({
            "success": False,
            "cancelled": True,
            "cancelled_at": utc_now_iso(),
            "error": "Task was stopped by an explicit controller action.",
        })
        atomic_write_json(run_dir / "result.json", previous, mode=0o600)
        return {"ok": True, "task_id": task_id, "stopped_pid": pid}

    def _task_run_dir(self, task_id: str) -> Path:
        if not re.fullmatch(r"[0-9A-Za-z-]{8,80}", task_id):
            raise ControlError("task_id 格式无效。")
        run_dir = (self.paths.task_runs_dir / task_id).resolve()
        try:
            run_dir.relative_to(self.paths.task_runs_dir.resolve())
        except ValueError as exc:
            raise ControlError("task_id 越界。") from exc
        if not run_dir.exists():
            raise ControlError(f"任务不存在：{task_id}")
        return run_dir

    def codex_task_status(self, *, task_id: str) -> dict[str, Any]:
        run_dir = self._task_run_dir(task_id)
        pid = pid_from_file(run_dir / "pid")
        return {
            "task_id": task_id,
            "running": process_info(pid).get("running", False),
            "pid": pid,
            "spec": read_json(run_dir / "spec.json"),
            "result": read_json(run_dir / "result.json"),
            "log_tail": tail_text(run_dir / "runner.log", 160),
        }


def datetime_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S")
