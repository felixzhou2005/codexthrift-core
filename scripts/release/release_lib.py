from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
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


def verify_runtime_lock() -> None:
    """Require the committed Runtime lock to match a fresh export from uv.lock."""
    committed = ROOT / "packaging/runtime/requirements.lock"
    with tempfile.TemporaryDirectory(prefix="codexthrift-runtime-lock-") as temporary:
        generated = Path(temporary) / "requirements.lock"
        subprocess.run(
            [
                "uv",
                "export",
                "--extra",
                "runtime",
                "--no-emit-project",
                "--format",
                "requirements-txt",
                "--output-file",
                str(generated),
            ],
            cwd=ROOT,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        # uv records the output path in the second generated comment line.
        # The dependency and hash payload begins after the two-line header.
        expected = generated.read_text(encoding="utf-8").splitlines()[2:]
        actual = committed.read_text(encoding="utf-8").splitlines()[2:]
        if actual != expected:
            raise RuntimeError(
                "packaging/runtime/requirements.lock is stale; regenerate it from uv.lock"
            )
