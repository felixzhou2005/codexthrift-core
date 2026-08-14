"""Compatibility exports for infrastructure helpers.

New code should import from storage, security, platform, or errors directly.
"""

from .errors import ControlError
from .platform.process import (
    ensure_expected_process,
    force_kill_process,
    is_port_open,
    listening_pid,
    normalized_child_env,
    pid_from_file,
    process_info,
    spawn_detached,
    stop_process_tree,
    wait_for_port,
    which,
)
from .security.path_guard import require_confirmation, safe_home_path
from .security.redaction import (
    redact_secret,
    redact_sensitive_text,
    redact_sensitive_value,
)
from .storage.atomic_io import (
    atomic_write_json,
    atomic_write_text,
    backup_file,
    read_json,
    tail_text,
    utc_now_iso,
)

__all__ = [
    "ControlError",
    "atomic_write_json",
    "atomic_write_text",
    "backup_file",
    "ensure_expected_process",
    "force_kill_process",
    "is_port_open",
    "listening_pid",
    "normalized_child_env",
    "pid_from_file",
    "process_info",
    "read_json",
    "redact_secret",
    "redact_sensitive_text",
    "redact_sensitive_value",
    "require_confirmation",
    "safe_home_path",
    "spawn_detached",
    "stop_process_tree",
    "tail_text",
    "utc_now_iso",
    "wait_for_port",
    "which",
]
