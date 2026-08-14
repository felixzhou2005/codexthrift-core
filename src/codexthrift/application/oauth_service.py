from __future__ import annotations

import re
from typing import Any

from ..config_templates import (
    PROFILES,
)
from ..platform.process import pid_from_file, process_info, spawn_detached
from ..security.path_guard import require_confirmation
from ..security.redaction import redact_sensitive_text, redact_sensitive_value
from ..storage.atomic_io import (
    atomic_write_text,
    read_json,
    tail_text,
)
from .base import BaseApplicationService


class OAuthService(BaseApplicationService):
    def begin_oauth(self, *, confirm: str) -> dict[str, Any]:
        require_confirmation(confirm)
        self._require_runtime()
        profile = self.active_profile()
        model = PROFILES[profile].oauth_bootstrap
        running = process_info(pid_from_file(self.paths.oauth_pid))
        if running.get("running"):
            return {"ok": True, "already_running": True, **self.oauth_status()}
        for path in (self.paths.oauth_log, self.paths.oauth_result):
            path.unlink(missing_ok=True)
        env = self._runtime_env()
        command = [
            str(self.paths.runtime_python),
            str(self.paths.oauth_script),
            model,
            str(self.paths.oauth_result),
        ]
        proc = spawn_detached(command, log_path=self.paths.oauth_log, env=env, cwd=self.paths.run_dir)
        atomic_write_text(self.paths.oauth_pid, f"{proc.pid}\n", mode=0o600)
        return {
            "ok": True,
            "started": True,
            "pid": proc.pid,
            "bootstrap_model": model,
            "next": "调用 oauth_status 获取验证网址和设备码；完成浏览器授权后再次调用。",
        }

    def oauth_status(self) -> dict[str, Any]:
        pid = pid_from_file(self.paths.oauth_pid)
        info = process_info(pid)
        result = read_json(self.paths.oauth_result)
        log = tail_text(self.paths.oauth_log, 120, 25_000)
        urls = []
        for match in re.findall(r"https?://[^\s<>'\"]+", log):
            cleaned = match.rstrip(".,);]")
            if cleaned not in urls:
                urls.append(cleaned)
        codes = []
        patterns = [
            r"(?i)(?:device\s+code|user\s+code|verification\s+code|code)\s*[:=]\s*([A-Z0-9]{4,10}(?:-[A-Z0-9]{4,10})?)",
            r"\b([A-Z0-9]{4}-[A-Z0-9]{4})\b",
        ]
        for pattern in patterns:
            for match in re.findall(pattern, log):
                value = match.upper()
                if value not in codes:
                    codes.append(value)
        return {
            "running": info.get("running", False),
            "pid": pid,
            "auth_file_exists": self.paths.auth_file.exists(),
            "result": redact_sensitive_value(result),
            "verification_urls": urls[:5],
            "device_codes": codes[:5],
            "log_tail": redact_sensitive_text(log),
            "token_dir": str(self.paths.token_dir),
        }
