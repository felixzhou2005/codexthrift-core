from __future__ import annotations

import os
import shutil
from pathlib import Path

from codexthrift.paths import WorkspacePaths


def test_discover_does_not_create_workspace(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "router-home"
    monkeypatch.setenv("CODEX_ROUTER_HOME", str(root))

    paths = WorkspacePaths.discover()

    assert paths.root == root.resolve()
    assert not root.exists()


def test_ensure_mutable_creates_required_directories(tmp_path: Path) -> None:
    paths = WorkspacePaths(tmp_path / "router-home")

    paths.ensure_mutable()

    assert paths.config_dir.is_dir()
    assert paths.data_dir.is_dir()
    assert paths.mode_switch_dir.is_dir()
    assert paths.run_dir.is_dir()
    assert paths.secrets_dir.is_dir()
    if os.name != "nt":
        assert paths.secrets_dir.stat().st_mode & 0o777 == 0o700


def test_bundled_runtime_root_takes_precedence_over_legacy_venv(
    tmp_path: Path, monkeypatch
) -> None:
    bundle = tmp_path / "CodexThrift Runtime"
    monkeypatch.setenv("CODEXTHRIFT_RUNTIME_ROOT", str(bundle))
    paths = WorkspacePaths(tmp_path / "router-home")

    assert paths.bundled_runtime_root == bundle.resolve()
    assert paths.runtime_source == "bundled"
    assert paths.runtime_python == bundle.resolve() / "bin" / "python"
    assert paths.runtime_litellm == bundle.resolve() / "bin" / "litellm"


def test_invalid_bundled_runtime_does_not_fall_back_to_legacy_venv(
    tmp_path: Path, monkeypatch
) -> None:
    missing_bundle = tmp_path / "missing-runtime"
    monkeypatch.setenv("CODEXTHRIFT_RUNTIME_ROOT", str(missing_bundle))
    paths = WorkspacePaths(tmp_path / "router-home")

    assert paths.runtime_python == missing_bundle.resolve() / "bin" / "python"
    assert paths.runtime_litellm == missing_bundle.resolve() / "bin" / "litellm"
    assert paths.runtime_python != paths.runtime_venv / "bin" / "python"


def test_legacy_runtime_remains_default_without_bundle_environment(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("CODEXTHRIFT_RUNTIME_ROOT", raising=False)
    paths = WorkspacePaths(tmp_path / "router-home")

    assert paths.bundled_runtime_root is None
    assert paths.runtime_source == "managed_venv"


def test_macos_app_data_migrates_legacy_workspace_without_deleting_source(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("CODEX_ROUTER_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEXTHRIFT_MACOS_APP_DATA", "1")
    (tmp_path / "Library").mkdir()
    legacy = tmp_path / ".litellm-codex-router"
    (legacy / "data").mkdir(parents=True)
    (legacy / "config").mkdir()
    (legacy / "run").mkdir()
    (legacy / "cache").mkdir()
    (legacy / "data" / "routerllm.sqlite3").write_bytes(b"sqlite-data")
    (legacy / "config" / "active.yaml").write_text("model: test\n", encoding="utf-8")
    (legacy / "run" / "stale.sock").write_text("skip", encoding="utf-8")
    (legacy / "cache" / "temporary.bin").write_bytes(b"skip")

    paths = WorkspacePaths.discover()
    target = tmp_path / "Library" / "Application Support" / "CodexThrift"

    assert paths.root == target.resolve()
    assert (target / "data" / "routerllm.sqlite3").read_bytes() == b"sqlite-data"
    assert (target / "config" / "active.yaml").exists()
    assert not (target / "run").exists()
    assert not (target / "cache").exists()
    assert (legacy / "data" / "routerllm.sqlite3").exists()
    assert paths.data_location_status()["state"] == "migrated"


def test_macos_app_data_defers_migration_while_legacy_daemon_is_running(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("CODEX_ROUTER_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEXTHRIFT_MACOS_APP_DATA", "1")
    (tmp_path / "Library").mkdir()
    legacy = tmp_path / ".litellm-codex-router"
    (legacy / "run").mkdir(parents=True)
    (legacy / "data").mkdir()
    (legacy / "run" / "codexthriftd.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    (legacy / "data" / "routerllm.sqlite3").write_bytes(b"active")

    paths = WorkspacePaths.discover()

    assert paths.root == legacy.resolve()
    assert paths.data_location_status()["state"] == "migration_deferred_active_runtime"
    assert not (tmp_path / "Library" / "Application Support" / "CodexThrift").exists()


def test_explicit_router_home_overrides_macos_app_data_mode(tmp_path: Path, monkeypatch) -> None:
    explicit = tmp_path / "explicit"
    monkeypatch.setenv("CODEX_ROUTER_HOME", str(explicit))
    monkeypatch.setenv("CODEXTHRIFT_MACOS_APP_DATA", "1")

    paths = WorkspacePaths.discover()

    assert paths.root == explicit.resolve()


def test_macos_app_data_migrates_into_an_empty_target_directory(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("CODEX_ROUTER_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEXTHRIFT_MACOS_APP_DATA", "1")
    target = tmp_path / "Library" / "Application Support" / "CodexThrift"
    target.mkdir(parents=True)
    legacy = tmp_path / ".litellm-codex-router"
    (legacy / "data").mkdir(parents=True)
    (legacy / "data" / "routerllm.sqlite3").write_bytes(b"legacy")

    paths = WorkspacePaths.discover()

    assert paths.root == target.resolve()
    assert (target / "data" / "routerllm.sqlite3").read_bytes() == b"legacy"


def test_unmarked_target_is_backed_up_before_legacy_becomes_canonical(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("CODEX_ROUTER_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEXTHRIFT_MACOS_APP_DATA", "1")
    (tmp_path / "Library").mkdir()
    target = tmp_path / "Library" / "Application Support" / "CodexThrift"
    (target / "data").mkdir(parents=True)
    (target / "data" / "new-only.json").write_text("new", encoding="utf-8")
    legacy = tmp_path / ".litellm-codex-router"
    (legacy / "data").mkdir(parents=True)
    (legacy / "data" / "routerllm.sqlite3").write_bytes(b"legacy-history")

    paths = WorkspacePaths.discover()
    manifest = paths.data_location_status()["migration"]
    backups = list((target.parent).glob(".CodexThrift-pre-migration-*"))

    assert paths.root == target.resolve()
    assert (target / "data" / "routerllm.sqlite3").read_bytes() == b"legacy-history"
    assert backups and (backups[0] / "data" / "new-only.json").read_text(encoding="utf-8") == "new"
    assert manifest["migration_version"] == 2
    assert manifest["previous_target_backup"] == str(backups[0])


def test_low_disk_migration_renames_data_and_leaves_legacy_links(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("CODEX_ROUTER_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEXTHRIFT_MACOS_APP_DATA", "1")
    (tmp_path / "Library").mkdir()
    legacy = tmp_path / ".litellm-codex-router"
    (legacy / "data").mkdir(parents=True)
    (legacy / "config").mkdir()
    (legacy / "data" / "routerllm.sqlite3").write_bytes(b"large-history")
    low_space = shutil._ntuple_diskusage(total=100, used=99, free=1)
    monkeypatch.setattr("codexthrift.paths.shutil.disk_usage", lambda _path: low_space)

    paths = WorkspacePaths.discover()
    target = tmp_path / "Library" / "Application Support" / "CodexThrift"
    manifest = paths.data_location_status()["migration"]

    assert paths.root == target.resolve()
    assert (target / "data" / "routerllm.sqlite3").read_bytes() == b"large-history"
    assert (legacy / "data").is_symlink()
    assert (legacy / "data").resolve() == (target / "data").resolve()
    assert manifest["migration_mode"] == "rename"
