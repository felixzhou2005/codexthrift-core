from __future__ import annotations

import sys
import tarfile
from pathlib import Path

import pytest

RELEASE_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "release"
sys.path.insert(0, str(RELEASE_SCRIPTS))

from build_runtime import (
    prune_nonportable_optional_packages,
    validate_macho_dependencies,
)
from merge_runtime import validate_archive_members, validate_manifests


def runtime_manifest(architecture: str) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "artifact": f"codexthrift-runtime-macos-{architecture}.tar.zst",
        "coreVersion": "0.2.2",
        "sourceCommit": "source-commit-test",
        "pythonVersion": "3.13.12",
        "opensslVersions": {architecture: "OpenSSL 3.5.6 test"},
        "excludedPackages": {
            "pyroscope-io": "upstream macOS wheels reference an unbundled absolute build-machine library"
        },
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


def test_validate_macho_dependencies_accepts_system_and_relative_libraries() -> None:
    validate_macho_dependencies(
        Path("module.so"),
        "/private/tmp/module.so (architecture x86_64):\n"
        "\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1351.0.0)\n"
        "/private/tmp/module.so (architecture arm64):\n"
        "\t@rpath/librelative.dylib (compatibility version 1.0.0, current version 1.0.0)\n"
        "\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1351.0.0)\n",
    )


def test_prune_nonportable_optional_packages_removes_exact_pyroscope_files(
    tmp_path: Path,
) -> None:
    paths = [
        tmp_path / "pyroscope",
        tmp_path / "pyroscope_io",
        tmp_path / "pyroscope_io-0.8.16.dist-info",
    ]
    for path in paths:
        path.mkdir()
        (path / "file").write_text("test", encoding="utf-8")
    retained = tmp_path / "retained-package"
    retained.mkdir()

    prune_nonportable_optional_packages(tmp_path)

    assert not any(path.exists() for path in paths)
    assert retained.is_dir()


@pytest.mark.parametrize(
    "dependency",
    [
        "/usr/local/opt/openssl@3/lib/libssl.3.dylib",
        "/opt/homebrew/opt/openssl@3/lib/libcrypto.3.dylib",
        "/Users/runner/build/private.dylib",
        "/private/var/folders/build/private.dylib",
    ],
)
def test_validate_macho_dependencies_rejects_build_machine_paths(
    dependency: str,
) -> None:
    with pytest.raises(RuntimeError, match="non-portable Mach-O dependency"):
        validate_macho_dependencies(
            Path("module.so"),
            f"module.so:\n\t{dependency} (compatibility version 3.0.0, current version 3.0.0)\n",
        )
