from __future__ import annotations

import re
import threading
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

from ..errors import ControlError
from ..paths import WorkspacePaths
from ..security.redaction import redact_sensitive_text, redact_sensitive_value
from ..storage.atomic_io import atomic_write_json, read_json, utc_now_iso

MODE_SWITCH_JOB_VERSION = 2
LEGACY_MODE_SWITCH_JOB_VERSIONS = frozenset({1})
MODE_SWITCH_TARGETS = frozenset({"native", "routed"})
MODE_SWITCH_CLIENT_POLICIES = frozenset({"wait", "defer", "terminate_selected"})
MODE_SWITCH_STAGES = frozenset(
    {
        "preview",
        "waiting_client_choice",
        "prepare_target",
        "verify_proxy",
        "commit_config",
        "verify_target_provider",
        "recycle_clients",
        "verify_convergence",
        "cleanup_target",
        "completed",
        "action_required",
        "failed",
        "canceled",
        "superseded",
    }
)
ACTIVE_MODE_SWITCH_STATES = frozenset({"queued", "running", "waiting"})
TERMINAL_MODE_SWITCH_STATES = frozenset(
    {"completed", "action_required", "failed", "canceled", "superseded"}
)
_JOB_ID_PATTERN = re.compile(r"[0-9a-f]{32}")


class ModeSwitchCoordinator:
    """Persist and guard the lifecycle of one in-flight mode switch.

    Phase-specific side effects are intentionally supplied by the application
    service. This coordinator owns durable state, transition validation,
    idempotent creation, cancellation boundaries, and restart recovery.
    """

    def __init__(self, paths: WorkspacePaths) -> None:
        self.paths = paths
        self._lock = threading.RLock()

    def create_job(
        self,
        *,
        source: str,
        target: str,
        client_policy: str,
        selected_clients: list[dict[str, Any]] | None = None,
        snapshot: dict[str, Any] | None = None,
        replace_active: bool = True,
        allow_unverified: bool = True,
    ) -> dict[str, Any]:
        self._validate_target(target)
        self._validate_client_policy(client_policy)
        selected = self._normalized_clients(selected_clients or [])
        sanitized_snapshot = redact_sensitive_value(snapshot or {})
        request_fingerprint = {
            "source": str(source),
            "target": target,
            "client_policy": client_policy,
            "selected_clients": selected,
            "allow_unverified": bool(allow_unverified),
        }
        with self._lock:
            active = self.active_job()
            if active is not None:
                active_request = active.get("request") or {}
                if all(
                    active_request.get(key) == value
                    for key, value in request_fingerprint.items()
                ):
                    return {**active, "reused": True}
                if not replace_active:
                    raise ControlError("已有 Codex 模式切换正在进行，且请求未允许替换。")
            now = utc_now_iso()
            job_id = uuid.uuid4().hex
            predecessor_job_id: str | None = None
            waiting_for_predecessor = False
            supersedes: str | None = None
            if active is not None:
                supersedes = str(active["job_id"])
                inherited_predecessor = active.get("predecessor_job_id")
                if active.get("waiting_for_predecessor") and isinstance(
                    inherited_predecessor, str
                ):
                    predecessor_job_id = inherited_predecessor
                    waiting_for_predecessor = True
                    predecessor = self._read_job(predecessor_job_id)
                    if predecessor is not None and predecessor.get("state") not in TERMINAL_MODE_SWITCH_STATES:
                        predecessor["superseded_by"] = job_id
                        predecessor["supersede_reason"] = "newer_user_intent"
                        predecessor["updated_at"] = now
                        self._write_job(predecessor)
                    self._mark_superseded(active, superseded_by=job_id)
                elif active.get("state") == "waiting" or bool(active.get("cancellable")):
                    self._mark_superseded(active, superseded_by=job_id)
                else:
                    predecessor_job_id = str(active["job_id"])
                    waiting_for_predecessor = True
                    active["superseded_by"] = job_id
                    active["supersede_reason"] = "newer_user_intent"
                    active["updated_at"] = now
                    self._write_job(active)
            job = {
                "version": MODE_SWITCH_JOB_VERSION,
                "job_id": job_id,
                "intent_sequence": self._next_intent_sequence(),
                "request": request_fingerprint,
                "state": "queued",
                "stage": "preview",
                "progress": 0,
                "cancellable": True,
                "created_at": now,
                "updated_at": now,
                "snapshot": sanitized_snapshot,
                "result": None,
                "rollback": None,
                "error": None,
                "supersedes": supersedes,
                "superseded_by": None,
                "supersede_reason": None,
                "waiting_for_predecessor": waiting_for_predecessor,
                "predecessor_job_id": predecessor_job_id,
            }
            self._write_job(job)
            self._write_current(job["job_id"])
            return deepcopy(job)

    def current_job(self) -> dict[str, Any] | None:
        with self._lock:
            pointer = read_json(self.paths.mode_switch_current)
            job_id = pointer.get("job_id") if isinstance(pointer, dict) else None
            if not isinstance(job_id, str) or not _JOB_ID_PATTERN.fullmatch(job_id):
                return None
            return self._read_job(job_id)

    def active_job(self) -> dict[str, Any] | None:
        job = self.current_job()
        return job if job and job.get("state") in ACTIVE_MODE_SWITCH_STATES else None

    def recover(self) -> dict[str, Any] | None:
        """Return durable unfinished work for the application service to resume."""
        return self.active_job()

    def is_latest(self, job_id: str) -> bool:
        with self._lock:
            current = self.current_job()
            return bool(current and current.get("job_id") == job_id)

    def acknowledge_supersede(self, job_id: str) -> dict[str, Any]:
        """Stop an older worker after it reaches a cooperative safe boundary."""
        with self._lock:
            job = self._require_job(job_id)
            if job.get("state") == "superseded":
                return deepcopy(job)
            if not job.get("superseded_by"):
                raise ControlError("模式切换任务没有待处理的替换请求。")
            return deepcopy(
                self._mark_superseded(
                    job,
                    superseded_by=str(job["superseded_by"]),
                )
            )

    def release_predecessor(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._require_job(job_id)
            if not job.get("waiting_for_predecessor"):
                return deepcopy(job)
            predecessor_id = job.get("predecessor_job_id")
            predecessor = (
                self._read_job(str(predecessor_id))
                if isinstance(predecessor_id, str)
                else None
            )
            if predecessor is not None and predecessor.get("state") not in TERMINAL_MODE_SWITCH_STATES:
                raise ControlError("前序模式切换尚未到达安全边界。")
            job["waiting_for_predecessor"] = False
            job["updated_at"] = utc_now_iso()
            self._write_job(job)
            return deepcopy(job)

    def update_snapshot(
        self,
        job_id: str,
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        """Replace the preflight snapshot after rebasing on live state."""
        with self._lock:
            job = self._require_job(job_id)
            if job.get("state") in TERMINAL_MODE_SWITCH_STATES:
                raise ControlError("模式切换任务已经结束，不能更新快照。")
            if not self.is_latest(job_id):
                raise ControlError("只有最新模式切换意图可以更新快照。")
            job["snapshot"] = redact_sensitive_value(snapshot)
            job["updated_at"] = utc_now_iso()
            self._write_job(job)
            return deepcopy(job)

    def update_stage(
        self,
        job_id: str,
        *,
        state: str = "running",
        stage: str,
        progress: int,
        cancellable: bool,
        result: dict[str, Any] | None = None,
        rollback: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if state not in ACTIVE_MODE_SWITCH_STATES | TERMINAL_MODE_SWITCH_STATES:
            raise ControlError(f"未知模式切换状态：{state}")
        if stage not in MODE_SWITCH_STAGES:
            raise ControlError(f"未知模式切换阶段：{stage}")
        normalized_progress = int(progress)
        if not 0 <= normalized_progress <= 100:
            raise ControlError("模式切换进度必须在 0 到 100 之间。")
        with self._lock:
            job = self._require_job(job_id)
            if job.get("state") in TERMINAL_MODE_SWITCH_STATES:
                raise ControlError("模式切换任务已经结束，不能继续更新。")
            if job.get("superseded_by"):
                raise ControlError("模式切换任务已被更新的用户意图取代。")
            if normalized_progress < int(job.get("progress") or 0):
                raise ControlError("模式切换进度不能倒退。")
            job.update(
                {
                    "state": state,
                    "stage": stage,
                    "progress": normalized_progress,
                    "cancellable": bool(cancellable),
                    "updated_at": utc_now_iso(),
                }
            )
            if result is not None:
                job["result"] = redact_sensitive_value(result)
            if rollback is not None:
                job["rollback"] = redact_sensitive_value(rollback)
            self._write_job(job)
            return deepcopy(job)

    def complete(
        self,
        job_id: str,
        *,
        result: dict[str, Any],
        action_required: bool = False,
    ) -> dict[str, Any]:
        state = "action_required" if action_required else "completed"
        return self.update_stage(
            job_id,
            state=state,
            stage=state,
            progress=100,
            cancellable=False,
            result=result,
        )

    def fail(
        self,
        job_id: str,
        *,
        stage: str,
        error: Exception | str,
        rollback: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if stage not in MODE_SWITCH_STAGES:
            raise ControlError(f"未知模式切换失败阶段：{stage}")
        with self._lock:
            job = self._require_job(job_id)
            if job.get("state") in TERMINAL_MODE_SWITCH_STATES:
                raise ControlError("模式切换任务已经结束，不能标记失败。")
            job.update(
                {
                    "state": "failed",
                    "stage": "failed",
                    "cancellable": False,
                    "updated_at": utc_now_iso(),
                    "error": {
                        "stage": stage,
                        "type": type(error).__name__ if isinstance(error, Exception) else "Error",
                        "message": redact_sensitive_text(str(error)),
                    },
                    "rollback": redact_sensitive_value(rollback) if rollback is not None else None,
                }
            )
            self._write_job(job)
            return deepcopy(job)

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._require_job(job_id)
            if job.get("state") in TERMINAL_MODE_SWITCH_STATES:
                return deepcopy(job)
            if not job.get("cancellable"):
                raise ControlError("模式切换已提交配置或进入不可取消阶段。")
            job.update(
                {
                    "state": "canceled",
                    "stage": "canceled",
                    "cancellable": False,
                    "updated_at": utc_now_iso(),
                }
            )
            self._write_job(job)
            return deepcopy(job)

    def resume_action_required(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._require_job(job_id)
            if job.get("state") != "action_required":
                raise ControlError("只有等待客户端处理的任务可以继续。")
            job.update(
                {
                    "state": "running",
                    "stage": "recycle_clients",
                    "cancellable": False,
                    "updated_at": utc_now_iso(),
                }
            )
            self._write_job(job)
            return deepcopy(job)

    def job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._require_job(job_id))

    def _job_path(self, job_id: str) -> Path:
        if not _JOB_ID_PATTERN.fullmatch(job_id):
            raise ControlError("模式切换 job_id 格式无效。")
        return self.paths.mode_switch_dir / f"job-{job_id}.json"

    def _read_job(self, job_id: str) -> dict[str, Any] | None:
        value = read_json(self._job_path(job_id))
        if not isinstance(value, dict) or value.get("version") not in (
            LEGACY_MODE_SWITCH_JOB_VERSIONS | {MODE_SWITCH_JOB_VERSION}
        ):
            return None
        if value.get("job_id") != job_id:
            return None
        return self._with_compatibility_defaults(value)

    def _require_job(self, job_id: str) -> dict[str, Any]:
        value = self._read_job(job_id)
        if value is None:
            raise ControlError("模式切换任务不存在或状态文件无效。")
        return value

    def _write_job(self, job: dict[str, Any]) -> None:
        atomic_write_json(self._job_path(str(job["job_id"])), job, mode=0o600)

    def _write_current(self, job_id: str) -> None:
        atomic_write_json(
            self.paths.mode_switch_current,
            {"version": MODE_SWITCH_JOB_VERSION, "job_id": job_id},
            mode=0o600,
        )

    def _next_intent_sequence(self) -> int:
        maximum = 0
        for path in self.paths.mode_switch_dir.glob("job-*.json"):
            value = read_json(path)
            if not isinstance(value, dict):
                continue
            try:
                maximum = max(maximum, int(value.get("intent_sequence") or 1))
            except (TypeError, ValueError):
                continue
        return maximum + 1

    def _mark_superseded(
        self,
        job: dict[str, Any],
        *,
        superseded_by: str,
    ) -> dict[str, Any]:
        job.update(
            {
                "state": "superseded",
                "stage": "superseded",
                "cancellable": False,
                "superseded_by": superseded_by,
                "supersede_reason": "newer_user_intent",
                "updated_at": utc_now_iso(),
            }
        )
        self._write_job(job)
        return job

    @staticmethod
    def _with_compatibility_defaults(job: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(job)
        normalized.setdefault("intent_sequence", 1)
        normalized.setdefault("supersedes", None)
        normalized.setdefault("superseded_by", None)
        normalized.setdefault("supersede_reason", None)
        normalized.setdefault("waiting_for_predecessor", False)
        normalized.setdefault("predecessor_job_id", None)
        return normalized

    @staticmethod
    def _validate_target(target: str) -> None:
        if target not in MODE_SWITCH_TARGETS:
            raise ControlError(f"模式切换目标必须是 {sorted(MODE_SWITCH_TARGETS)}。")

    @staticmethod
    def _validate_client_policy(client_policy: str) -> None:
        if client_policy not in MODE_SWITCH_CLIENT_POLICIES:
            raise ControlError(
                f"客户端处理策略必须是 {sorted(MODE_SWITCH_CLIENT_POLICIES)}。"
            )

    @staticmethod
    def _normalized_clients(clients: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in clients:
            try:
                pid = int(item["pid"])
                create_time = float(item["create_time"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ControlError("客户端选择必须包含有效的 pid 和 create_time。") from exc
            if pid <= 0 or create_time <= 0:
                raise ControlError("客户端选择包含无效的 pid 或 create_time。")
            normalized.append({"pid": pid, "create_time": create_time})
            kind = item.get("kind")
            fingerprint = item.get("command_fingerprint")
            if kind is not None or fingerprint is not None:
                if kind not in {"codex_cli", "codex_app_server", "vscode_app_server"}:
                    raise ControlError("客户端选择包含无效的进程类型。")
                if not isinstance(fingerprint, str) or len(fingerprint) != 64:
                    raise ControlError("客户端选择包含无效的命令指纹。")
                normalized[-1].update(
                    {"kind": kind, "command_fingerprint": fingerprint}
                )
        normalized.sort(key=lambda item: (item["pid"], item["create_time"]))
        if len({(item["pid"], item["create_time"]) for item in normalized}) != len(normalized):
            raise ControlError("客户端选择包含重复进程。")
        return normalized
