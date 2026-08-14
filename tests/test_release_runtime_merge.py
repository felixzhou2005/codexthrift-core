from __future__ import annotations

import sys
import tarfile
from pathlib import Path

import pytest

RELEASE_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "release"
sys.path.insert(0, str(RELEASE_SCRIPTS))

from merge_runtime import validate_archive_members, validate_manifests


def runtime_manifest(architecture: str) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "artifact": f"codexthrift-runtime-macos-{architecture}.tar.zst",
        "coreVersion": "0.2.2",
        "sourceCommit": "source-commit-test",
        "pythonVersion": "3.13.12",
        "architectures": {
            architecture: [
                {"name": "codexthrift", "version": "0.2.2"},
                {"name": "cryptography", "version": "50.0.0"},
                {"name": "litellm", "version": "1.96.2"},
            ]
        },
    }


def test_validate_manifests_accepts_matching_native_partials() -> None:
    validate_manifests(
        {
            "arm64": runtime_manifest("arm64"),
            "x86_64": runtime_manifest("x86_64"),
        }
    )


def test_validate_manifests_rejects_key_package_version_drift() -> None:
    arm64 = runtime_manifest("arm64")
    x86_64 = runtime_manifest("x86_64")
    packages = x86_64["architectures"]["x86_64"]
    packages[-1]["version"] = "1.95.0"

    with pytest.raises(RuntimeError, match="litellm versions differ"):
        validate_manifests({"arm64": arm64, "x86_64": x86_64})


@pytest.mark.parametrize(
    "name, message",
    [
        ("../private.env", "unsafe partial Runtime entry"),
        ("/tmp/private.env", "unsafe partial Runtime entry"),
        ("unexpected/file", "unexpected partial Runtime entry"),
    ],
)
def test_validate_archive_members_rejects_unsafe_paths(
    name: str, message: str
) -> None:
    member = tarfile.TarInfo(name)

    with pytest.raises(RuntimeError, match=message):
        validate_archive_members([member])


def test_validate_archive_members_accepts_runtime_tree() -> None:
    validate_archive_members(
        [tarfile.TarInfo("codexthrift-runtime/arm64/site-packages/module.py")]
    )
