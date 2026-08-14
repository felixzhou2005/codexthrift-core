from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any


def _core_version() -> str:
    try:
        return version("codexthrift")
    except PackageNotFoundError:
        return "0.0.0-dev"


def _runtime_root() -> Path | None:
    raw = os.environ.get("CODEXTHRIFT_RUNTIME_ROOT")
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def _manifest(runtime_root: Path | None) -> tuple[str, str]:
    if runtime_root is None:
        return os.environ.get("CODEXTHRIFT_SOURCE_COMMIT", "unknown"), "unavailable"
    path = runtime_root / "runtime-manifest.json"
    try:
        content = path.read_bytes()
        value = json.loads(content)
        source_commit = str(value.get("sourceCommit") or "unknown")
        return source_commit, hashlib.sha256(content).hexdigest()
    except (OSError, ValueError, TypeError):
        return os.environ.get("CODEXTHRIFT_SOURCE_COMMIT", "unknown"), "unavailable"


def build_identity(
    *,
    data_root: Path | None = None,
    instance_id: str | None = None,
    runtime_root: Path | None = None,
    executable_path: str | None = None,
) -> dict[str, Any]:
    runtime = runtime_root if runtime_root is not None else _runtime_root()
    runtime = runtime.resolve() if runtime is not None else None
    core_version = _core_version()
    source_commit, manifest_sha = _manifest(runtime)
    build_material = f"{core_version}|{source_commit}|{manifest_sha}".encode()
    build_id = f"codexthrift-{core_version}-{hashlib.sha256(build_material).hexdigest()[:16]}"
    resolved_executable = executable_path or os.environ.get(
        "CODEXTHRIFT_LAUNCHER_PATH"
    ) or sys.argv[0] or sys.executable
    return {
        "coreVersion": core_version,
        "sourceCommit": source_commit,
        "runtimeManifestSha256": manifest_sha,
        "coreBuildId": build_id,
        "instanceId": instance_id or str(uuid.uuid4()),
        "pid": os.getpid(),
        "startedAt": datetime.now(UTC).isoformat(),
        "dataRoot": str(data_root.resolve()) if data_root is not None else None,
        "runtimeRoot": str(runtime) if runtime is not None else None,
        "executablePath": str(Path(resolved_executable).resolve()),
    }
