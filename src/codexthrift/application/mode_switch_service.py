from __future__ import annotations

import threading
from typing import Any, Protocol

from ..errors import ControlError
from .codex_client_lifecycle_service import CodexClientLifecycleService
from .mode_switch_coordinator import ModeSwitchCoordinator
from .session_continuity_service import CodexSessionContinuityService


class ModeSwitchController(Protocol):
    paths: Any

    def codex_mode_status(self) -> dict[str, Any]: ...

    def enable_smart_routing(
        self, *, confirm: str, allow_unverified: bool = False
    ) -> dict[str, Any]: ...

    def switch_to_native_codex(self, *, confirm: str) -> dict[str, Any]: ...

    def stop_proxy(self, *, confirm: str) -> dict[str, Any]: ...

    def proxy_status(self) -> dict[str, Any]: ...


class ModeSwitchService:
    """Run durable Native/Routed transitions outside the request thread."""

    def __init__(
        self,
        controller: ModeSwitchController,
        *,
        coordinator: ModeSwitchCoordinator | None = None,
        clients: CodexClientLifecycleService | None = None,
        continuity: CodexSessionContinuityService | None = None,
        recover: bool = True,
    ) -> None:
        self.controller = controller
        self.coordinator = coordinator or ModeSwitchCoordinator(controller.paths)
        self.clients = clients or CodexClientLifecycleService()
        self.continuity = continuity or CodexSessionContinuityService()
        self._threads: dict[str, threading.Thread] = {}
        self._wait_threads: dict[str, threading.Thread] = {}
        self._threads_lock = threading.Lock()
        self._waiting_resolution_lock = threading.Lock()
        self._execution_lock = threading.Lock()
        self._closed = threading.Event()
        if recover:
            unfinished = self.coordinator.recover()
            if unfinished is not None:
                self._schedule(str(unfinished["job_id"]))

    def preview(self, *, target: str) -> dict[str, Any]:
        self._validate_target(target)
        mode = self.controller.codex_mode_status()
        clients = self.clients.preview()
        source = self._source(mode)
        return {
            "source": source,
            "target": target,
            "already_at_target": source == target,
            "mode": mode,
            "clients": clients,
            "proxy_target": "running" if target == "routed" else "standby",
            "client_policies": ["wait", "defer", "terminate_selected"],
            "automatic_termination_available": clients.get("detection_status") == "available",
        }

    def create(
        self,
        *,
        target: str,
        client_policy: str,
        selected_clients: list[dict[str, Any]] | None,
        confirm: str,
        request_id: str | None = None,
        replace_active: bool = True,
        allow_unverified: bool = True,
    ) -> dict[str, Any]:
        if confirm != "SWITCH_CODEX_MODE":
            raise ControlError("模式切换需要 confirm='SWITCH_CODEX_MODE'。")
        preview = self.preview(target=target)
        selected = selected_clients or []
        if client_policy == "terminate_selected" and not selected:
            raise ControlError("自动结束客户端时必须选择至少一个客户端。")
        if client_policy != "terminate_selected" and selected:
            raise ControlError("只有 terminate_selected 策略可以提交客户端选择。")
        available = {
            self._client_key(item): item
            for item in preview["clients"].get("clients") or []
            if isinstance(item, dict)
        }
        normalized_selected: list[dict[str, Any]] = []
        for requested in selected:
            matched = available.get(self._client_key(requested))
            if matched is None:
                raise ControlError("选择的 Codex 客户端已退出或身份已变化，请刷新预览。")
            normalized_selected.append(matched)
        observed = preview["clients"].get("clients") or []
        job = self.coordinator.create_job(
            source=preview["source"],
            target=target,
            client_policy=client_policy,
            selected_clients=normalized_selected,
            snapshot={
                "mode": preview["mode"],
                "proxy": self.controller.proxy_status(),
                "observed_clients": observed,
                "detection_status": preview["clients"].get("detection_status"),
                "request_id": request_id,
                "session_continuity": {"status": "pending"},
            },
            replace_active=replace_active,
            allow_unverified=allow_unverified,
        )
        self._schedule(str(job["job_id"]))
        superseded_job = None
        superseded_id = job.get("supersedes")
        if isinstance(superseded_id, str):
            superseded_job = self._job_summary(self.coordinator.job(superseded_id))
        return {
            **job,
            "superseded_job": superseded_job,
            "waiting_for_safe_boundary": bool(job.get("waiting_for_predecessor")),
        }

    def status(self, job_id: str) -> dict[str, Any]:
        job = self.coordinator.job(job_id)
        if job.get("state") == "waiting":
            self._resolve_waiting(job)
            job = self.coordinator.job(job_id)
        return job

    def current(self) -> dict[str, Any]:
        job = self.coordinator.current_job()
        if job is not None and job.get("state") == "waiting":
            self._resolve_waiting(job)
            job = self.coordinator.current_job()
        return {"job": job}

    def cancel(self, job_id: str, *, confirm: str) -> dict[str, Any]:
        if confirm != "CANCEL_CODEX_MODE_SWITCH":
            raise ControlError("取消模式切换需要明确确认。")
        return self.coordinator.cancel(job_id)

    def retry_clients(self, job_id: str, *, force: bool, confirm: str) -> dict[str, Any]:
        expected = "FORCE_STOP_CODEX_CLIENTS" if force else "RETRY_CODEX_CLIENTS"
        if confirm != expected:
            raise ControlError(f"客户端重试需要 confirm='{expected}'。")
        job = self.coordinator.resume_action_required(job_id)
        self._schedule(str(job["job_id"]), force_clients=force)
        return job

    def close(self) -> None:
        self._closed.set()

    def _schedule(self, job_id: str, *, force_clients: bool = False) -> None:
        with self._threads_lock:
            existing = self._threads.get(job_id)
            if existing is not None and existing.is_alive():
                return
            thread = threading.Thread(
                target=self._run,
                args=(job_id,),
                kwargs={"force_clients": force_clients},
                name=f"codex-mode-switch-{job_id[:8]}",
                daemon=True,
            )
            self._threads[job_id] = thread
            thread.start()

    def _run(self, job_id: str, *, force_clients: bool = False) -> None:
        try:
            with self._execution_lock:
                self._run_exclusive(job_id, force_clients=force_clients)
        except Exception as error:  # noqa: BLE001 - worker must persist unexpected failures
            try:
                job = self.coordinator.job(job_id)
                if job.get("state") not in {
                    "completed",
                    "action_required",
                    "failed",
                    "canceled",
                    "superseded",
                } and self.coordinator.is_latest(job_id):
                    proxy_rollback = self._rollback_proxy_if_needed(job)
                    self.coordinator.fail(
                        job_id,
                        stage=str(job.get("stage") or "prepare_target"),
                        error=error,
                        rollback=(
                            {"proxy": proxy_rollback}
                            if proxy_rollback is not None
                            else None
                        ),
                    )
            except (ControlError, OSError, TypeError, ValueError):
                pass
        finally:
            with self._threads_lock:
                self._threads.pop(job_id, None)

    def _run_exclusive(self, job_id: str, *, force_clients: bool = False) -> None:
        try:
            job = self.coordinator.job(job_id)
            if job.get("state") not in {"queued", "running", "waiting"}:
                return
            if job.get("state") == "waiting":
                self._schedule_wait_monitor(job_id)
                return
            if job.get("waiting_for_predecessor"):
                predecessor_id = job.get("predecessor_job_id")
                if isinstance(predecessor_id, str):
                    predecessor = self.coordinator.job(predecessor_id)
                    if predecessor.get("state") not in {
                        "completed",
                        "action_required",
                        "failed",
                        "canceled",
                        "superseded",
                    } and predecessor.get("superseded_by") == job_id:
                        self.coordinator.acknowledge_supersede(predecessor_id)
                job = self.coordinator.release_predecessor(job_id)
            if not self.coordinator.is_latest(job_id):
                if job.get("superseded_by"):
                    self.coordinator.acknowledge_supersede(job_id)
                return
            live_clients = self.clients.preview()
            continuity_before = self.continuity.capture()
            previous_snapshot = job.get("snapshot") or {}
            job = self.coordinator.update_snapshot(
                job_id,
                {
                    "mode": self.controller.codex_mode_status(),
                    "proxy": self.controller.proxy_status(),
                    "observed_clients": live_clients.get("clients") or [],
                    "detection_status": live_clients.get("detection_status"),
                    "request_id": previous_snapshot.get("request_id"),
                    "session_continuity": continuity_before,
                    "rebased": True,
                },
            )
            target = str(job["request"]["target"])
            if job.get("stage") == "recycle_clients" and force_clients:
                self._finish_clients(job, force=True)
                return
            if not self._advance(job_id, "prepare_target", 10, cancellable=True):
                return
            mode = self.controller.codex_mode_status()
            proxy_before_target = self.controller.proxy_status()
            needs_routed_repair = target == "routed" and (
                self._source(mode) != target
                or not proxy_before_target.get("running")
                or not proxy_before_target.get("health", {}).get("ok")
            )
            needs_native_repair = target == "native" and (
                not proxy_before_target.get("running")
                or not proxy_before_target.get("health", {}).get("ok")
                or mode.get("provider_managed_by_routerllm") is not True
            )
            if self._source(mode) != target or needs_routed_repair or needs_native_repair:
                if target == "routed":
                    if not self._advance(job_id, "verify_proxy", 25, cancellable=True):
                        return
                    allow_unverified = bool(
                        (job.get("request") or {}).get("allow_unverified", True)
                    )
                    result = self.controller.enable_smart_routing(
                        confirm="ENABLE_ROUTER",
                        allow_unverified=allow_unverified,
                    )
                else:
                    if not self._advance(job_id, "commit_config", 35, cancellable=False):
                        return
                    result = self.controller.switch_to_native_codex(confirm="SWITCH_NATIVE")
                if result.get("ok") is not True:
                    result_stage = str(result.get("stage") or "")
                    proxy_rollback = self._rollback_proxy_if_needed(job)
                    rollback = dict(result.get("rollback") or {})
                    if proxy_rollback is not None:
                        rollback["proxy"] = proxy_rollback
                    self.coordinator.fail(
                        job_id,
                        stage=(
                            "verify_proxy"
                    if result_stage in {"proxy_preflight", "standby_proxy"}
                            else "verify_target_provider"
                        ),
                        error=str(result.get("verification") or result.get("route_preflight") or "mode switch failed"),
                        rollback=rollback,
                    )
                    return
            if not self._advance(
                job_id, "verify_target_provider", 60, cancellable=False
            ):
                return
            mode = self.controller.codex_mode_status()
            if self._source(mode) != target:
                raise ControlError("配置动作完成后未收敛到目标 Codex Provider。")
            if target == "routed":
                proxy = self.controller.proxy_status()
                if not proxy.get("running") or not proxy.get("health", {}).get("ok"):
                    raise ControlError("Routed 模式的 LiteLLM Proxy 未通过健康检查。")
            else:
                proxy = self.controller.proxy_status()
                if not proxy.get("running") or not proxy.get("health", {}).get("ok"):
                    raise ControlError(
                        "Native 模式已启用，但 LiteLLM Proxy 未处于待命状态；"
                        "无法保证恢复 routed 历史会话。"
                    )
            job = self.coordinator.job(job_id)
            self._finish_clients(job, force=False)
        except ControlError:
            job = self.coordinator.job(job_id)
            if job.get("superseded_by") and job.get("state") not in {
                "superseded",
                "completed",
                "action_required",
                "failed",
                "canceled",
            }:
                self.coordinator.acknowledge_supersede(job_id)
                return
            raise

    def _finish_clients(self, job: dict[str, Any], *, force: bool) -> None:
        job_id = str(job["job_id"])
        if not self._advance(job_id, "recycle_clients", 80, cancellable=False):
            return
        request = job["request"]
        policy = request["client_policy"]
        observed = job.get("snapshot", {}).get("observed_clients") or []
        termination = None
        if policy == "terminate_selected":
            termination = self.clients.terminate_selected(
                request.get("selected_clients") or [],
                force=force,
            )
        convergence = self.clients.convergence(observed)
        if not self._advance(job_id, "verify_convergence", 95, cancellable=False):
            return
        continuity = self._continuity_result(job_id)
        result = {
            "mode": self.controller.codex_mode_status(),
            "proxy": self.controller.proxy_status(),
            "clients": convergence,
            "termination": termination,
            "session_continuity": continuity,
        }
        if convergence["converged"] and continuity.get("verified"):
            self.coordinator.complete(job_id, result=result)
        elif policy == "wait" and not force:
            self.coordinator.update_stage(
                job_id,
                state="waiting",
                stage="verify_convergence",
                progress=95,
                cancellable=False,
                result=result,
            )
            self._schedule_wait_monitor(job_id)
        else:
            self.coordinator.complete(job_id, result=result, action_required=True)

    def _schedule_wait_monitor(self, job_id: str) -> None:
        with self._threads_lock:
            existing = self._wait_threads.get(job_id)
            if existing is not None and existing.is_alive():
                return
            thread = threading.Thread(
                target=self._wait_for_convergence,
                args=(job_id,),
                name=f"codex-mode-wait-{job_id[:8]}",
                daemon=True,
            )
            self._wait_threads[job_id] = thread
            thread.start()

    def _wait_for_convergence(self, job_id: str) -> None:
        try:
            while not self._closed.wait(1.0):
                job = self.coordinator.job(job_id)
                if job.get("state") != "waiting":
                    return
                self._resolve_waiting(job)
        except (ControlError, OSError, TypeError, ValueError):
            return
        finally:
            with self._threads_lock:
                self._wait_threads.pop(job_id, None)

    def _resolve_waiting(self, job: dict[str, Any]) -> None:
        with self._waiting_resolution_lock:
            current = self.coordinator.job(str(job["job_id"]))
            if current.get("state") != "waiting":
                return
            observed = current.get("snapshot", {}).get("observed_clients") or []
            convergence = self.clients.convergence(observed)
            if convergence["converged"]:
                continuity = self._continuity_result(str(job["job_id"]))
                self.coordinator.complete(
                    str(job["job_id"]),
                    result={
                        "mode": self.controller.codex_mode_status(),
                        "proxy": self.controller.proxy_status(),
                        "clients": convergence,
                        "termination": None,
                        "session_continuity": continuity,
                    },
                    action_required=not bool(continuity.get("verified")),
                )

    def _continuity_result(self, job_id: str) -> dict[str, Any]:
        job = self.coordinator.job(job_id)
        before = (job.get("snapshot") or {}).get("session_continuity")
        after = self.continuity.capture()
        return self.continuity.verify(before, after)

    def _rollback_proxy_if_needed(self, job: dict[str, Any]) -> dict[str, Any] | None:
        if job.get("request", {}).get("target") != "routed":
            return None
        original_proxy = job.get("snapshot", {}).get("proxy") or {}
        if original_proxy.get("running"):
            return None
        try:
            if self._source(self.controller.codex_mode_status()) == "routed":
                return None
            return self.controller.stop_proxy(confirm="APPLY")
        except (ControlError, OSError, TypeError, ValueError) as error:
            return {"ok": False, "error": str(error)}

    def _advance(
        self,
        job_id: str,
        stage: str,
        progress: int,
        *,
        cancellable: bool,
    ) -> bool:
        job = self.coordinator.job(job_id)
        if job.get("state") in {
            "canceled",
            "superseded",
            "completed",
            "action_required",
            "failed",
        }:
            return False
        if not self.coordinator.is_latest(job_id):
            if job.get("superseded_by"):
                self.coordinator.acknowledge_supersede(job_id)
            return False
        current_progress = int(job.get("progress") or 0)
        if progress < current_progress:
            return True
        try:
            self.coordinator.update_stage(
                job_id,
                state="running",
                stage=stage,
                progress=progress,
                cancellable=cancellable,
            )
        except ControlError:
            latest = self.coordinator.job(job_id)
            if latest.get("superseded_by"):
                self.coordinator.acknowledge_supersede(job_id)
                return False
            raise
        return True

    @staticmethod
    def _source(mode: dict[str, Any]) -> str:
        value = mode.get("client_mode")
        if value == "smart_router":
            return "routed"
        if value == "native_openai":
            return "native"
        return "unknown"

    @staticmethod
    def _validate_target(target: str) -> None:
        if target not in {"native", "routed"}:
            raise ControlError("模式切换目标必须是 native 或 routed。")

    @staticmethod
    def _client_key(item: dict[str, Any]) -> tuple[int, float, str]:
        try:
            return (
                int(item["pid"]),
                float(item["create_time"]),
                str(item["command_fingerprint"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ControlError("客户端选择缺少有效进程身份。") from exc

    @staticmethod
    def _job_summary(job: dict[str, Any]) -> dict[str, Any]:
        return {
            "job_id": job.get("job_id"),
            "intent_sequence": job.get("intent_sequence"),
            "state": job.get("state"),
            "stage": job.get("stage"),
            "target": (job.get("request") or {}).get("target"),
            "superseded_by": job.get("superseded_by"),
            "supersede_reason": job.get("supersede_reason"),
        }
