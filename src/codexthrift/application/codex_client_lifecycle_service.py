from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import psutil

from ..codex_clients import codex_client_identity, detect_codex_clients
from ..errors import ControlError

ClientDetector = Callable[..., dict[str, Any]]
IdentityReader = Callable[[int], dict[str, Any] | None]
ProcessFactory = Callable[[int], psutil.Process]


class CodexClientLifecycleService:
    """Safely inspect and stop exact Codex client generations only."""

    def __init__(
        self,
        *,
        detector: ClientDetector = detect_codex_clients,
        identity_reader: IdentityReader = codex_client_identity,
        process_factory: ProcessFactory = psutil.Process,
    ) -> None:
        self._detector = detector
        self._identity_reader = identity_reader
        self._process_factory = process_factory

    def preview(self, *, exclude_pids: tuple[int, ...] = ()) -> dict[str, Any]:
        return self._detector(exclude_pids=exclude_pids)

    def terminate_selected(
        self,
        selected_clients: list[dict[str, Any]],
        *,
        force: bool = False,
        timeout: float = 8.0,
    ) -> dict[str, Any]:
        observed = self._detector()
        if observed.get("detection_status") != "available":
            raise ControlError("Codex 客户端探测权限受限，拒绝自动终止任何客户端。")
        current = {
            self._identity_key(item): item
            for item in observed.get("clients") or []
            if isinstance(item, dict)
        }
        selected = [self._validate_selection(item) for item in selected_clients]
        if len({self._identity_key(item) for item in selected}) != len(selected):
            raise ControlError("客户端选择包含重复进程。")

        terminated: list[dict[str, Any]] = []
        still_running: list[dict[str, Any]] = []
        for expected in selected:
            key = self._identity_key(expected)
            matched = current.get(key)
            if matched is None:
                raise ControlError(
                    f"拒绝终止 PID {expected['pid']}：进程已退出、被重用或身份已变化。"
                )
            if matched.get("has_config_override") or not matched.get("eligible_for_termination"):
                raise ControlError(
                    f"拒绝终止 PID {expected['pid']}：客户端使用独立 profile/config 覆盖。"
                )
            live = self._identity_reader(int(expected["pid"]))
            if live is None or self._identity_key(live) != key:
                raise ControlError(
                    f"拒绝终止 PID {expected['pid']}：发送信号前身份复核失败。"
                )
            process = self._process_factory(int(expected["pid"]))
            if abs(float(process.create_time()) - float(expected["create_time"])) > 0.001:
                raise ControlError(
                    f"拒绝终止 PID {expected['pid']}：检测到 PID 重用。"
                )
            alive = self._terminate_process_tree(process, timeout=max(0.0, timeout))
            if alive and force:
                for item in alive:
                    try:
                        item.kill()
                    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
                        pass
                _, alive = psutil.wait_procs(alive, timeout=3)
            result = {
                "pid": expected["pid"],
                "create_time": expected["create_time"],
                "kind": expected["kind"],
                "restart_behavior": matched.get("restart_behavior"),
            }
            if alive:
                still_running.append(result)
            else:
                terminated.append(result)
        return {
            "ok": not still_running,
            "force": bool(force),
            "terminated": terminated,
            "still_running": still_running,
        }

    def wait_for_exit(
        self,
        clients: list[dict[str, Any]],
        *,
        timeout: float,
        interval: float = 0.2,
    ) -> dict[str, Any]:
        expected = [self._validate_selection(item) for item in clients]
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            remaining = [
                item
                for item in expected
                if self._same_generation_still_running(item)
            ]
            if not remaining or time.monotonic() >= deadline:
                break
            time.sleep(max(0.01, interval))
        return {
            "exited": [item for item in expected if item not in remaining],
            "still_running": remaining,
            "complete": not remaining,
        }

    def convergence(self, observed_clients: list[dict[str, Any]]) -> dict[str, Any]:
        expected = [self._validate_selection(item) for item in observed_clients]
        detection = self._detector()
        current_clients = detection.get("clients") or []
        current_keys = {
            self._identity_key(item)
            for item in current_clients
            if isinstance(item, dict)
        }
        stale = [item for item in expected if self._identity_key(item) in current_keys]
        return {
            "detection_status": detection.get("detection_status", "unavailable"),
            "stale_clients": stale,
            "converged": detection.get("detection_status") == "available" and not stale,
        }

    @staticmethod
    def _terminate_process_tree(process: psutil.Process, timeout: float) -> list[psutil.Process]:
        try:
            children = process.children(recursive=True)
        except (
            psutil.AccessDenied,
            psutil.NoSuchProcess,
            psutil.ZombieProcess,
            PermissionError,
        ):
            children = []
        targets = [*children, process]
        for target in targets:
            try:
                target.terminate()
            except (
                psutil.AccessDenied,
                psutil.NoSuchProcess,
                psutil.ZombieProcess,
                PermissionError,
            ):
                pass
        _, alive = psutil.wait_procs(targets, timeout=timeout)
        return alive

    def _same_generation_still_running(self, expected: dict[str, Any]) -> bool:
        current = self._identity_reader(int(expected["pid"]))
        return current is not None and self._identity_key(current) == self._identity_key(expected)

    @staticmethod
    def _identity_key(item: dict[str, Any]) -> tuple[int, float, str, str]:
        return (
            int(item.get("pid") or 0),
            float(item.get("create_time") or 0),
            str(item.get("kind") or ""),
            str(item.get("command_fingerprint") or ""),
        )

    @staticmethod
    def _validate_selection(item: dict[str, Any]) -> dict[str, Any]:
        try:
            pid = int(item["pid"])
            create_time = float(item["create_time"])
            kind = str(item["kind"])
            fingerprint = str(item["command_fingerprint"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ControlError("客户端选择缺少完整的进程身份。") from exc
        if (
            pid <= 0
            or create_time <= 0
            or kind not in {"codex_cli", "codex_app_server", "vscode_app_server"}
            or len(fingerprint) != 64
        ):
            raise ControlError("客户端选择包含无效的进程身份。")
        return {
            "pid": pid,
            "create_time": create_time,
            "kind": kind,
            "command_fingerprint": fingerprint,
        }
