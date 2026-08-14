from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve()


_MACOS_APP_DATA_ENV = "CODEXTHRIFT_MACOS_APP_DATA"
_MIGRATION_DIRECTORIES = ("config", "secrets", "data", "logs", "backups", "task-runs")
_MIGRATION_EXCLUSIONS = ("run", "cache", "litellm-venv")
_MIGRATION_VERSION = 2


def _pid_is_running(path: Path) -> bool:
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        return True
    except (FileNotFoundError, OSError, ValueError):
        return False


def _legacy_runtime_active(root: Path) -> bool:
    return _pid_is_running(root / "run" / "codexthriftd.pid") or _pid_is_running(root / "run" / "litellm.pid")


def _tree_inventory(root: Path) -> tuple[int, int]:
    total_bytes = 0
    file_count = 0
    for directory in _MIGRATION_DIRECTORIES:
        source = root / directory
        if not source.exists():
            continue
        for current, _, files in os.walk(source):
            for name in files:
                path = Path(current) / name
                try:
                    if path.is_symlink():
                        continue
                    total_bytes += path.stat().st_size
                    file_count += 1
                except OSError:
                    continue
    return total_bytes, file_count


def _ignore_symlinks(directory: str, names: list[str]) -> set[str]:
    root = Path(directory)
    return {name for name in names if (root / name).is_symlink()}


def _read_migration_manifest(root: Path) -> dict[str, object]:
    try:
        value = json.loads((root / "data" / "data-location-migration.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _migration_backup_path(parent: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    candidate = parent / f".CodexThrift-pre-migration-{stamp}"
    suffix = 0
    while candidate.exists():
        suffix += 1
        candidate = parent / f".CodexThrift-pre-migration-{stamp}-{suffix}"
    return candidate


def _write_migration_manifest(
    root: Path,
    *,
    legacy_root: Path,
    target_root: Path,
    copied_bytes: int,
    copied_files: int,
    migration_mode: str,
    previous_target_backup: Path | None = None,
) -> None:
    manifest: dict[str, object] = {
        "version": _MIGRATION_VERSION,
        "migration_version": _MIGRATION_VERSION,
        "state": "completed",
        "completed_at": datetime.now(UTC).isoformat(),
        "legacy_root": str(legacy_root),
        "target_root": str(target_root),
        "copied_bytes": copied_bytes,
        "copied_files": copied_files,
        "copied_directories": list(_MIGRATION_DIRECTORIES),
        "excluded_directories": list(_MIGRATION_EXCLUSIONS),
        "legacy_preserved": True,
        "migration_mode": migration_mode,
    }
    if previous_target_backup is not None:
        manifest["previous_target_backup"] = str(previous_target_backup)
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "data" / "data-location-migration.json").write_text(
        json.dumps(manifest, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _rename_macos_app_root(
    legacy_root: Path,
    target_root: Path,
    total_bytes: int,
    file_count: int,
    previous_target_backup: Path | None,
) -> Path:
    """Move the canonical data in-place when a second full copy will not fit."""
    staging = Path(tempfile.mkdtemp(prefix=".CodexThrift-renaming-", dir=str(target_root.parent)))
    moved: list[str] = []
    try:
        for directory in _MIGRATION_DIRECTORIES:
            source = legacy_root / directory
            if source.exists():
                os.replace(source, staging / directory)
                moved.append(directory)
        copied_bytes, copied_files = _tree_inventory(staging)
        if copied_bytes != total_bytes or copied_files != file_count:
            raise OSError("Renamed migration inventory does not match the legacy root.")
        _write_migration_manifest(
            staging,
            legacy_root=legacy_root,
            target_root=target_root,
            copied_bytes=copied_bytes,
            copied_files=copied_files,
            migration_mode="rename",
            previous_target_backup=previous_target_backup,
        )
        os.replace(staging, target_root)
        for directory in moved:
            link = legacy_root / directory
            if link.exists() or link.is_symlink():
                continue
            try:
                link.symlink_to(target_root / directory, target_is_directory=True)
            except OSError:
                # The canonical target is already committed. A missing legacy
                # convenience link does not invalidate the migration.
                continue
        return target_root
    except OSError:
        for directory in reversed(moved):
            source = staging / directory
            if source.exists() and not (legacy_root / directory).exists():
                os.replace(source, legacy_root / directory)
        shutil.rmtree(staging, ignore_errors=True)
        return legacy_root


def _migrate_macos_app_root(legacy_root: Path, target_root: Path) -> Path:
    previous_target_backup: Path | None = None
    if target_root.exists():
        try:
            if any(target_root.iterdir()):
                manifest = _read_migration_manifest(target_root)
                if (
                    manifest.get("state") == "completed"
                    and manifest.get("legacy_root") == str(legacy_root)
                    and manifest.get("migration_version") == _MIGRATION_VERSION
                ):
                    return target_root
                if not legacy_root.exists():
                    return target_root
                if _legacy_runtime_active(legacy_root):
                    return legacy_root
                # An unmarked target can be a partial first boot. Preserve it as a
                # recoverable snapshot, then make the legacy root the canonical
                # source so the first migration cannot hide old sessions.
                backup = _migration_backup_path(target_root.parent)
                os.replace(target_root, backup)
                previous_target_backup = backup
            else:
                target_root.rmdir()
        except OSError:
            return target_root
    if not legacy_root.exists():
        return target_root
    if _legacy_runtime_active(legacy_root):
        return legacy_root

    total_bytes, file_count = _tree_inventory(legacy_root)
    target_root.parent.mkdir(parents=True, exist_ok=True)
    available = shutil.disk_usage(target_root.parent).free
    if available < total_bytes + 64 * 1024 * 1024:
        return _rename_macos_app_root(
            legacy_root,
            target_root,
            total_bytes,
            file_count,
            previous_target_backup,
        )

    staging = Path(tempfile.mkdtemp(prefix=".CodexThrift-migrating-", dir=str(target_root.parent)))
    try:
        for directory in _MIGRATION_DIRECTORIES:
            source = legacy_root / directory
            if source.exists():
                shutil.copytree(source, staging / directory, symlinks=False, ignore=_ignore_symlinks)
        copied_bytes, copied_files = _tree_inventory(staging)
        if copied_bytes != total_bytes or copied_files != file_count:
            raise OSError("Migrated data inventory does not match the legacy root.")
        _write_migration_manifest(
            staging,
            legacy_root=legacy_root,
            target_root=target_root,
            copied_bytes=copied_bytes,
            copied_files=copied_files,
            migration_mode="copy",
            previous_target_backup=previous_target_backup,
        )
        os.replace(staging, target_root)
        return target_root
    except OSError:
        shutil.rmtree(staging, ignore_errors=True)
        return legacy_root


def _discover_root() -> Path:
    configured = os.environ.get("CODEX_ROUTER_HOME")
    if configured:
        return Path(configured)
    legacy_root = Path.home() / ".litellm-codex-router"
    if os.environ.get(_MACOS_APP_DATA_ENV) != "1" or os.name != "posix" or not (Path.home() / "Library").exists():
        return legacy_root
    target_root = Path.home() / "Library" / "Application Support" / "CodexThrift"
    return _migrate_macos_app_root(legacy_root, target_root)


@dataclass(frozen=True)
class WorkspacePaths:
    root: Path

    @classmethod
    def discover(cls) -> WorkspacePaths:
        return cls(_resolved(_discover_root()))

    @classmethod
    def default(cls) -> WorkspacePaths:
        """Compatibility alias for side-effect-free path discovery."""
        return cls.discover()

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def secrets_dir(self) -> Path:
        return self.root / "secrets"

    @property
    def token_dir(self) -> Path:
        return self.secrets_dir / "chatgpt"

    @property
    def auth_file(self) -> Path:
        return self.token_dir / "auth.json"

    @property
    def master_key_file(self) -> Path:
        return self.secrets_dir / "litellm-master-key"

    @property
    def run_dir(self) -> Path:
        return self.root / "run"

    @property
    def daemon_socket(self) -> Path:
        return self.run_dir / "codexthrift.sock"

    @property
    def daemon_lock(self) -> Path:
        return self.run_dir / "codexthriftd.lock"

    @property
    def daemon_pid(self) -> Path:
        return self.run_dir / "codexthriftd.pid"

    @property
    def daemon_log(self) -> Path:
        return self.logs_dir / "codexthriftd.log"

    @property
    def desired_state_file(self) -> Path:
        return self.data_dir / "desired-state.json"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def cache_dir(self) -> Path:
        return self.root / "cache"

    @property
    def audit_log(self) -> Path:
        return self.logs_dir / "route-audit.jsonl"

    @property
    def feedback_log(self) -> Path:
        return self.logs_dir / "route-feedback.jsonl"

    @property
    def proxy_log(self) -> Path:
        return self.logs_dir / "litellm-proxy.log"

    @property
    def oauth_log(self) -> Path:
        return self.logs_dir / "oauth.log"

    @property
    def oauth_result(self) -> Path:
        return self.run_dir / "oauth-result.json"

    @property
    def oauth_pid(self) -> Path:
        return self.run_dir / "oauth.pid"

    @property
    def probe_logs_dir(self) -> Path:
        return self.logs_dir / "model-probes"

    @property
    def probe_results_dir(self) -> Path:
        return self.run_dir / "model-probes"

    def probe_log_for(self, phase: str, profile: str) -> Path:
        return self.probe_logs_dir / f"{phase}-{profile}.log"

    def probe_result_for(self, phase: str, profile: str) -> Path:
        return self.probe_results_dir / f"{phase}-{profile}.json"

    def probe_spec_for(self, phase: str, profile: str) -> Path:
        return self.probe_results_dir / f"{phase}-{profile}-spec.json"

    @property
    def probe_job_meta(self) -> Path:
        return self.run_dir / "model-probe-job.json"

    @property
    def probe_pid(self) -> Path:
        return self.run_dir / "model-probe.pid"

    @property
    def install_log(self) -> Path:
        return self.logs_dir / "runtime-install.log"

    @property
    def install_result(self) -> Path:
        return self.run_dir / "runtime-install-result.json"

    @property
    def install_pid(self) -> Path:
        return self.run_dir / "runtime-install.pid"

    @property
    def proxy_pid(self) -> Path:
        return self.run_dir / "litellm.pid"

    @property
    def active_phase_file(self) -> Path:
        return self.run_dir / "active-phase.txt"

    @property
    def active_profile_file(self) -> Path:
        return self.run_dir / "active-profile.txt"

    @property
    def runtime_venv(self) -> Path:
        return self.root / "litellm-venv"

    @property
    def bundled_runtime_root(self) -> Path | None:
        configured = os.environ.get("CODEXTHRIFT_RUNTIME_ROOT")
        return _resolved(Path(configured)) if configured else None

    @property
    def runtime_source(self) -> str:
        return "bundled" if self.bundled_runtime_root is not None else "managed_venv"

    @property
    def runtime_python(self) -> Path:
        if self.bundled_runtime_root is not None:
            return self.bundled_runtime_root / "bin" / "python"
        if os.name == "nt":
            return self.runtime_venv / "Scripts" / "python.exe"
        return self.runtime_venv / "bin" / "python"

    @property
    def runtime_litellm(self) -> Path:
        if self.bundled_runtime_root is not None:
            return self.bundled_runtime_root / "bin" / "litellm"
        if os.name == "nt":
            return self.runtime_venv / "Scripts" / "litellm.exe"
        return self.runtime_venv / "bin" / "litellm"

    @property
    def phase2_config(self) -> Path:
        return self.config_dir / "phase2.yaml"

    @property
    def phase3_config(self) -> Path:
        return self.config_dir / "phase3.yaml"

    @property
    def phase3a_config(self) -> Path:
        return self.config_dir / "phase3a-candidate.yaml"

    @property
    def phase21_config(self) -> Path:
        return self.config_dir / "phase2.1-candidate.yaml"

    @property
    def active_config(self) -> Path:
        return self.config_dir / "active.yaml"

    @property
    def audit_callback(self) -> Path:
        return self.config_dir / "audit_callback.py"

    @property
    def phase21_audit_callback(self) -> Path:
        return self.config_dir / "audit_callback_candidate.py"

    @property
    def phase21_routing_callback(self) -> Path:
        return self.config_dir / "routing_callback_candidate.py"

    @property
    def phase21_routing_policy(self) -> Path:
        return self.config_dir / "codex_routing_policy.py"

    @property
    def phase3a_routing_policy(self) -> Path:
        return self.config_dir / "codex_routing_policy_phase3a.py"

    @property
    def phase3a_routing_callback(self) -> Path:
        return self.config_dir / "routing_callback_phase3a.py"

    @property
    def phase3a_audit_callback(self) -> Path:
        return self.config_dir / "audit_callback_phase3a.py"

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def adaptive_db(self) -> Path:
        return self.data_dir / "adaptive-router.sqlite3"

    @property
    def dashboard_db(self) -> Path:
        """SQLite read model used by the local desktop dashboard."""
        return self.data_dir / "routerllm.sqlite3"

    @property
    def encrypted_payload_log(self) -> Path:
        """Append-only encrypted call bodies written by the LiteLLM audit callback."""
        return self.data_dir / "call-payloads.jsonl.enc"

    @property
    def content_key_file(self) -> Path:
        """Non-macOS fallback for the content key; macOS uses Keychain."""
        return self.secrets_dir / "call-payload-key"

    @property
    def native_codex_snapshot(self) -> Path:
        return self.data_dir / "native-codex-config.json"

    @property
    def codex_mode_state(self) -> Path:
        return self.data_dir / "codex-mode-state.json"

    @property
    def session_migration_ledger(self) -> Path:
        return self.data_dir / "codex-session-migrations.json"

    @property
    def dashboard_pid(self) -> Path:
        return self.run_dir / "routerd.pid"

    @property
    def dashboard_log(self) -> Path:
        return self.logs_dir / "routerd.log"

    @property
    def dashboard_access_token_file(self) -> Path:
        return self.secrets_dir / "dashboard-access-token"

    @property
    def launch_agent_plist(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / "com.rifo.codexthrift.core.plist"

    @property
    def adaptive_settings(self) -> Path:
        return self.data_dir / "adaptive-settings.json"

    @property
    def active_policy_file(self) -> Path:
        return self.run_dir / "active-policy.txt"

    @property
    def oauth_script(self) -> Path:
        return self.run_dir / "oauth_probe.py"

    @property
    def model_probe_script(self) -> Path:
        return self.run_dir / "model_probe.py"

    @property
    def runtime_install_script(self) -> Path:
        return self.run_dir / "install_runtime.py"

    @property
    def backups_dir(self) -> Path:
        return self.root / "backups"

    @property
    def codex_config(self) -> Path:
        return self.codex_home / "config.toml"

    @property
    def codex_home(self) -> Path:
        configured = os.environ.get("CODEX_HOME")
        home = Path(configured).expanduser() if configured else Path.home() / ".codex"
        return _resolved(home)

    @property
    def task_runs_dir(self) -> Path:
        return self.root / "task-runs"

    @property
    def storage_maintenance_dir(self) -> Path:
        return self.data_dir / "storage-maintenance"

    @property
    def mode_switch_dir(self) -> Path:
        return self.data_dir / "mode-switch"

    @property
    def mode_switch_current(self) -> Path:
        return self.mode_switch_dir / "current.json"

    @property
    def data_location_migration(self) -> Path:
        return self.data_dir / "data-location-migration.json"

    def data_location_status(self) -> dict[str, object]:
        legacy_root = _resolved(Path.home() / ".litellm-codex-router")
        target_root = _resolved(Path.home() / "Library" / "Application Support" / "CodexThrift")
        manifest: dict[str, object] = {}
        try:
            value = json.loads(self.data_location_migration.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                manifest = value
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            pass
        app_mode = os.environ.get(_MACOS_APP_DATA_ENV) == "1"
        explicit_root = bool(os.environ.get("CODEX_ROUTER_HOME"))
        if explicit_root:
            state = "explicit_override"
        elif not app_mode:
            state = "explicit_or_legacy"
        elif self.root == target_root:
            state = "migrated" if manifest else "application_support"
        elif _legacy_runtime_active(legacy_root):
            state = "migration_deferred_active_runtime"
        else:
            state = "migration_deferred"
        return {
            "state": state,
            "current_root": str(self.root),
            "legacy_root": str(legacy_root),
            "target_root": str(target_root),
            "app_data_mode": app_mode,
            "explicit_root": explicit_root,
            "legacy_preserved": bool(manifest.get("legacy_preserved")),
            "migration": manifest,
        }

    def ensure_mutable(self) -> None:
        for path in (
            self.root,
            self.config_dir,
            self.secrets_dir,
            self.token_dir,
            self.run_dir,
            self.logs_dir,
            self.probe_logs_dir,
            self.probe_results_dir,
            self.backups_dir,
            self.task_runs_dir,
            self.data_dir,
            self.storage_maintenance_dir,
            self.mode_switch_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.root.chmod(0o700)
            self.run_dir.chmod(0o700)
            self.secrets_dir.chmod(0o700)
            self.token_dir.chmod(0o700)

    def ensure(self) -> None:
        """Compatibility alias for callers not yet migrated to ensure_mutable()."""
        self.ensure_mutable()
