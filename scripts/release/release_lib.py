from __future__ import annotations

import hashlib
import json
import subprocess
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def project_version() -> str:
    value = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(value["project"]["version"])


def git_commit() -> str:
    return run_output(["git", "rev-parse", "HEAD"], cwd=ROOT)


def run(
    command: list[str], *, cwd: Path = ROOT, env: dict[str, str] | None = None
) -> None:
    subprocess.run(command, cwd=cwd, env=env, check=True)


def run_output(command: list[str], *, cwd: Path = ROOT) -> str:
    return subprocess.check_output(command, cwd=cwd, text=True).strip()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def release_artifact_names(version: str) -> set[str]:
    return {
        f"codexthrift-{version}-py3-none-any.whl",
        f"codexthrift-{version}.tar.gz",
        f"codexthrift-{version}-source.tar.gz",
        "codexthriftd-arm64-apple-darwin",
        "codexthriftd-x86_64-apple-darwin",
        "codexthriftd-universal-apple-darwin",
        "codexthrift-runtime-macos-universal.tar.zst",
        "runtime-manifest.json",
        "release-manifest.json",
        f"codexthrift-core-v{version}.spdx.json",
        "RELEASE_NOTES.md",
        "SHA256SUMS",
    }
