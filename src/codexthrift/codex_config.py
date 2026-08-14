from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Literal

import tomlkit

from .codex_ingress import codex_ingress_base_url
from .codex_mode import (
    LEGACY_MANAGED_PROVIDER_ID,
    MANAGED_MODEL,
    MANAGED_PROVIDER_ID,
    NATIVE_PROVIDER_ID,
    CodexClientMode,
    capture_native_snapshot,
    classify_codex_document,
    is_routerllm_provider,
    read_native_snapshot,
    router_base_urls,
)
from .paths import WorkspacePaths
from .util import ControlError, atomic_write_text, backup_file, utc_now_iso

Phase = Literal["phase2", "phase3", "phase3a"]


def codex_proxy_auth_command(paths: WorkspacePaths | None = None) -> tuple[str, list[str]]:
    if Path("/bin/cat").is_file():
        key_path = (paths or WorkspacePaths.default()).master_key_file
        return "/bin/cat", [str(key_path)]
    if getattr(sys, "frozen", False):
        return sys.executable, ["--print-codex-proxy-token"]
    return sys.executable, ["-m", "codexthrift.key_reader"]


def _load_codex_document(config_path: Path) -> Any:
    if not config_path.exists():
        return tomlkit.document()
    try:
        return tomlkit.parse(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ControlError(
            f"现有 Codex 配置不是可解析的 TOML，未修改：{config_path}: {exc}"
        ) from exc


def _write_codex_document(config_path: Path, document: Any) -> None:
    rendered = tomlkit.dumps(document)
    try:
        tomlkit.parse(rendered)
    except Exception as exc:
        raise ControlError(f"生成的 Codex 配置无效，未写入：{exc}") from exc
    atomic_write_text(config_path, rendered, mode=0o600)


def _restore_optional_key(document: Any, key: str, state: Any, *, reject_values: set[Any] | None = None) -> None:
    if not isinstance(state, dict) or state.get("present") is not True:
        document.pop(key, None)
        return
    value = state.get("value")
    if reject_values and value in reject_values:
        document.pop(key, None)
        return
    document[key] = value


def configure_codex(paths: WorkspacePaths, phase: Phase, host: str, port: int) -> dict[str, Any]:
    paths.ensure()
    config_path = paths.codex_config
    config_path.parent.mkdir(parents=True, exist_ok=True)
    document = _load_codex_document(config_path)
    current = classify_codex_document(document, host=host, port=port)
    warnings: list[str] = []
    provider_was_replaced = False
    if current["client_mode"] == CodexClientMode.NATIVE_OPENAI.value:
        capture_native_snapshot(paths, document)
    elif current["client_mode"] == CodexClientMode.CONFIG_DRIFT.value:
        warnings.append("检测到 Codex 配置漂移；已按用户的智能代理切换操作修复目标配置。")
    elif current["client_mode"] == CodexClientMode.UNKNOWN.value:
        warnings.append("检测到未知 Codex Provider；已按用户的智能代理切换操作修复目标配置。")

    backup = backup_file(config_path, paths.backups_dir, "codex-config-before-smart")
    document["model"] = MANAGED_MODEL
    document["model_provider"] = MANAGED_PROVIDER_ID
    document["openai_base_url"] = codex_ingress_base_url()
    if phase == "phase2":
        document["model_reasoning_effort"] = "medium"
    else:
        # Alias-level reasoning effort must remain authoritative in phase 3.
        document.pop("model_reasoning_effort", None)

    document.add(tomlkit.comment(
        f"OpenAI endpoint override managed by CodexThrift; updated {utc_now_iso()}"
    ))
    _write_codex_document(config_path, document)
    return {
        "ok": True,
        "config_path": str(config_path),
        "backup_path": str(backup) if backup else None,
        "phase": phase,
        "model": MANAGED_MODEL,
        "provider": MANAGED_PROVIDER_ID,
        "reasoning_effort": "medium" if phase == "phase2" else "alias-controlled",
        "auth": "built-in OpenAI auth to loopback entry",
        "mode": CodexClientMode.SMART_ROUTER.value,
        "config_drift_repaired": current["client_mode"]
        in {
            CodexClientMode.CONFIG_DRIFT.value,
            CodexClientMode.UNKNOWN.value,
        },
        "provider_replaced": provider_was_replaced,
        "warnings": warnings,
    }


def switch_to_native_codex(paths: WorkspacePaths, host: str, port: int) -> dict[str, Any]:
    paths.ensure()
    config_path = paths.codex_config
    config_path.parent.mkdir(parents=True, exist_ok=True)
    document = _load_codex_document(config_path)
    backup = backup_file(config_path, paths.backups_dir, "codex-config-before-native")
    snapshot = read_native_snapshot(paths)
    managed_keys = snapshot.get("managed_keys") if isinstance(snapshot, dict) else None

    document["model_provider"] = NATIVE_PROVIDER_ID
    if isinstance(managed_keys, dict):
        _restore_optional_key(
            document,
            "model",
            managed_keys.get("model"),
            reject_values={MANAGED_MODEL},
        )
        _restore_optional_key(
            document,
            "model_reasoning_effort",
            managed_keys.get("model_reasoning_effort"),
        )
        _restore_optional_key(
            document,
            "openai_base_url",
            managed_keys.get("openai_base_url"),
            reject_values=router_base_urls(host, port),
        )
    else:
        document.pop("model", None)
        document.pop("model_reasoning_effort", None)
        if document.get("openai_base_url") in router_base_urls(host, port):
            document.pop("openai_base_url", None)

    providers = document.get("model_providers")
    legacy_provider = (
        providers.get(LEGACY_MANAGED_PROVIDER_ID)
        if hasattr(providers, "get")
        else None
    )
    managed_provider_available = bool(
        is_routerllm_provider(
            legacy_provider,
            host=host,
            port=port,
        )
    )

    _write_codex_document(config_path, document)
    status = classify_codex_document(document, host=host, port=port)
    if status["client_mode"] != CodexClientMode.NATIVE_OPENAI.value:
        raise ControlError("原生 Codex 配置已写入，但最终状态仍存在配置漂移。")
    return {
        "ok": True,
        "mode": CodexClientMode.NATIVE_OPENAI.value,
        "config_path": str(config_path),
        "backup_path": str(backup) if backup else None,
        "snapshot_restored": snapshot is not None,
        "model": document.get("model"),
        "provider": document.get("model_provider"),
        "reasoning_effort": document.get("model_reasoning_effort"),
        "resume_providers": {
            NATIVE_PROVIDER_ID: True,
            LEGACY_MANAGED_PROVIDER_ID: managed_provider_available,
        },
    }


def codex_connection_status(paths: WorkspacePaths, host: str, port: int) -> dict[str, Any]:
    config_path = paths.codex_config
    if not config_path.exists():
        status = classify_codex_document(tomlkit.document(), host=host, port=port)
        return {
            **status,
            "exists": False,
            "codex_home": str(paths.codex_home),
            "config_path": str(config_path),
        }
    try:
        document = _load_codex_document(config_path)
    except ControlError as exc:
        return {
            "client_mode": CodexClientMode.CONFIG_DRIFT.value,
            "exists": True,
            "codex_home": str(paths.codex_home),
            "config_path": str(config_path),
            "error": str(exc),
        }
    return {
        **classify_codex_document(document, host=host, port=port),
        "exists": True,
        "codex_home": str(paths.codex_home),
        "config_path": str(config_path),
    }


def codex_config_status(
    paths: WorkspacePaths,
    phase: Phase,
    host: str,
    port: int,
) -> dict[str, Any]:
    """Verify that the existing Codex config points at this managed router."""
    config_path = paths.codex_config
    expected_base_url = codex_ingress_base_url()
    mismatches: list[str] = []
    if not config_path.exists():
        return {
            "configured": False,
            "exists": False,
            "config_path": str(config_path),
            "mismatches": ["config_missing"],
        }
    try:
        document = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "configured": False,
            "exists": True,
            "config_path": str(config_path),
            "mismatches": ["config_not_parseable"],
            "error": f"{type(exc).__name__}: {exc}",
        }

    if document.get("model") != "codex-auto":
        mismatches.append("model_not_codex_auto")
    if document.get("model_provider") != MANAGED_PROVIDER_ID:
        mismatches.append("provider_not_openai")
    if document.get("openai_base_url") != expected_base_url:
        mismatches.append("openai_base_url_mismatch")
    if phase == "phase2":
        if document.get("model_reasoning_effort") != "medium":
            mismatches.append("phase2_effort_not_medium")
    elif "model_reasoning_effort" in document:
        mismatches.append("non_phase2_effort_must_be_alias_controlled")

    return {
        "configured": not mismatches,
        "exists": True,
        "config_path": str(config_path),
        "phase": phase,
        "expected_base_url": expected_base_url,
        "mismatches": mismatches,
    }


def restore_codex_backup(paths: WorkspacePaths, backup_path: str) -> dict[str, Any]:
    source = Path(backup_path).expanduser().resolve()
    backup_root = paths.backups_dir.resolve()
    try:
        source.relative_to(backup_root)
    except ValueError as exc:
        raise ControlError("只能恢复由本控制器创建的备份文件。") from exc
    if not source.is_file():
        raise ControlError(f"备份不存在：{source}")
    current_backup = backup_file(paths.codex_config, paths.backups_dir, "codex-config-before-restore")
    atomic_write_text(paths.codex_config, source.read_text(encoding="utf-8"), mode=0o600)
    return {
        "ok": True,
        "restored_from": str(source),
        "current_backup": str(current_backup) if current_backup else None,
        "config_path": str(paths.codex_config),
    }
