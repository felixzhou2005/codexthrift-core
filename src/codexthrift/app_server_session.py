from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from collections import defaultdict, deque
from functools import lru_cache
from pathlib import Path
from typing import Any, ClassVar

from . import __version__
from .util import normalized_child_env


class AppServerError(RuntimeError):
    pass


def codex_child_env(source: dict[str, str] | None = None) -> dict[str, str]:
    """Keep application-only runtime switches out of the official Codex child."""
    env = normalized_child_env(source)
    env.pop("CODEXTHRIFT_MACOS_APP_DATA", None)
    env.pop("CODEXTHRIFT_RUNTIME_ROOT", None)
    return env


def resolve_codex_executable() -> str:
    candidates = [
        os.environ.get("ROUTERLLM_CODEX_EXECUTABLE"),
        "/Applications/ChatGPT.app/Contents/Resources/codex",
        shutil.which("codex"),
        "/opt/homebrew/bin/codex",
        "/usr/local/bin/codex",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise AppServerError("未找到 Codex App Server；请安装或更新 ChatGPT/Codex。")


@lru_cache(maxsize=1)
def codex_runtime_diagnostics() -> dict[str, str | None]:
    try:
        executable = resolve_codex_executable()
    except AppServerError:
        return {"source": "unavailable", "version": None}

    configured = os.environ.get("ROUTERLLM_CODEX_EXECUTABLE")
    if configured and Path(configured) == Path(executable):
        source = "configured_override"
    elif executable == "/Applications/ChatGPT.app/Contents/Resources/codex":
        source = "chatgpt_app_bundle"
    elif executable in {"/opt/homebrew/bin/codex", "/usr/local/bin/codex"}:
        source = "homebrew_or_path"
    else:
        source = "path"

    try:
        completed = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
        output = completed.stdout.strip() or completed.stderr.strip()
        version = output.splitlines()[0][:120] if output else None
    except (OSError, subprocess.SubprocessError):
        version = None
    return {"source": source, "version": version}


class CodexAppServerSession:
    """Thread-safe persistent JSONL session for the official Codex App Server."""

    notification_methods: ClassVar[set[str]] = {
        "account/login/completed",
        "account/updated",
        "account/rateLimits/updated",
    }

    def __init__(
        self,
        *,
        command: tuple[str, ...] | None = None,
        timeout_seconds: float = 20.0,
        env: dict[str, str] | None = None,
        experimental_api: bool = False,
    ):
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.env = env
        self.experimental_api = experimental_api
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._notification_condition = threading.Condition(self._state_lock)
        self._pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._notifications: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=50))
        self._next_request_id = 1
        self._closed = False

    @property
    def pid(self) -> int | None:
        with self._state_lock:
            return self._process.pid if self._process is not None and self._process.poll() is None else None

    def request(self, method: str, params: Any = None) -> dict[str, Any]:
        for attempt in range(2):
            try:
                return self._request_once(method, params)
            except AppServerError:
                self._stop_process()
                if attempt:
                    raise
        raise AppServerError("Codex App Server 请求失败。")

    def wait_for_notification(
        self,
        methods: set[str],
        *,
        timeout_seconds: float = 0.0,
        predicate: Any = None,
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        with self._notification_condition:
            while True:
                for method in methods:
                    messages = self._notifications.get(method)
                    if not messages:
                        continue
                    for message in list(messages):
                        if predicate is None or predicate(message):
                            messages.remove(message)
                            return message
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._notification_condition.wait(remaining)

    def close(self) -> None:
        with self._state_lock:
            self._closed = True
        self._stop_process()

    def _request_once(self, method: str, params: Any) -> dict[str, Any]:
        self._ensure_started()
        with self._state_lock:
            request_id = self._next_request_id
            self._next_request_id += 1
            response_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
            self._pending[request_id] = response_queue
        try:
            self._send({"id": request_id, "method": method, "params": params})
            try:
                response = response_queue.get(timeout=self.timeout_seconds)
            except queue.Empty as exc:
                raise AppServerError(f"Codex App Server {method} 请求超时。") from exc
            return _result_or_error(response, method)
        finally:
            with self._state_lock:
                self._pending.pop(request_id, None)

    def _ensure_started(self) -> None:
        with self._start_lock:
            with self._state_lock:
                if self._closed:
                    raise AppServerError("Codex App Server 会话已关闭。")
                if self._process is not None and self._process.poll() is None:
                    return
                executable_command = self.command or (resolve_codex_executable(), "app-server", "--stdio")
                try:
                    process = subprocess.Popen(
                        executable_command,
                        text=True,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        bufsize=1,
                        env=codex_child_env(self.env),
                    )
                except (FileNotFoundError, OSError) as exc:
                    raise AppServerError("无法启动 Codex App Server。") from exc
                self._process = process
                self._reader = threading.Thread(target=self._read_stdout, args=(process,), daemon=True)
                self._stderr_reader = threading.Thread(target=self._drain_stderr, args=(process,), daemon=True)
                self._reader.start()
                self._stderr_reader.start()

            initialize = self._request_without_start(
                "initialize",
                {
                    "clientInfo": {"name": "codexthrift", "version": __version__},
                    "capabilities": {"experimentalApi": self.experimental_api},
                },
            )
            if not isinstance(initialize, dict):
                self._stop_process()
                raise AppServerError("Codex App Server 初始化失败。")
            self._send({"method": "initialized"})

    def _request_without_start(self, method: str, params: Any) -> dict[str, Any]:
        with self._state_lock:
            request_id = self._next_request_id
            self._next_request_id += 1
            response_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
            self._pending[request_id] = response_queue
        try:
            self._send({"id": request_id, "method": method, "params": params})
            try:
                response = response_queue.get(timeout=self.timeout_seconds)
            except queue.Empty as exc:
                raise AppServerError("Codex App Server 初始化超时。") from exc
            return _result_or_error(response, method)
        finally:
            with self._state_lock:
                self._pending.pop(request_id, None)

    def _send(self, value: dict[str, Any]) -> None:
        with self._write_lock:
            process = self._process
            if process is None or process.poll() is not None or process.stdin is None:
                raise AppServerError("Codex App Server 连接不可用。")
            try:
                process.stdin.write(json.dumps(value, separators=(",", ":")) + "\n")
                process.stdin.flush()
            except OSError as exc:
                raise AppServerError("Codex App Server 写入失败。") from exc

    def _read_stdout(self, process: subprocess.Popen[str]) -> None:
        if process.stdout is None:
            return
        for line in process.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            request_id = message.get("id")
            if isinstance(request_id, int):
                with self._state_lock:
                    pending = self._pending.get(request_id)
                if pending is not None:
                    try:
                        pending.put_nowait(message)
                    except queue.Full:
                        pass
                continue
            method = message.get("method")
            if isinstance(method, str) and method in self.notification_methods:
                with self._notification_condition:
                    self._notifications[method].append(message)
                    self._notification_condition.notify_all()
        self._fail_pending("Codex App Server 已退出。")

    @staticmethod
    def _drain_stderr(process: subprocess.Popen[str]) -> None:
        if process.stderr is None:
            return
        for _ in process.stderr:
            pass

    def _fail_pending(self, message: str) -> None:
        with self._state_lock:
            queues = list(self._pending.values())
        for response_queue in queues:
            try:
                response_queue.put_nowait({"error": {"message": message}})
            except queue.Full:
                pass

    def _stop_process(self) -> None:
        with self._state_lock:
            process = self._process
            self._process = None
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)


def _result_or_error(response: dict[str, Any], method: str) -> dict[str, Any]:
    if "error" in response:
        error = response["error"]
        message = error.get("message") if isinstance(error, dict) else "request failed"
        raise AppServerError(f"Codex App Server {method} 失败：{message}")
    result = response.get("result")
    return result if isinstance(result, dict) else {"value": result}
