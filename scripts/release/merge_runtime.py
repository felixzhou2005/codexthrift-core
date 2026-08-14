from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from build_runtime import PRUNED_RUNTIME_PACKAGES, create_archive
from release_lib import ROOT, write_json

ARCHITECTURES = ("arm64", "x86_64")
MATCHING_PACKAGES = ("codexthrift", "cryptography", "litellm")
SHARED_FILES = (
    "bin/python",
    "bin/litellm",
    "bin/codexthriftd-python",
    "runtime_entry.py",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm64-release-dir", type=Path, required=True)
    parser.add_argument("--x86-64-release-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="codexthrift-runtime-merge-") as temporary:
        root = Path(temporary)
        runtimes = {
            "arm64": extract_partial(args.arm64_release_dir, "arm64", root / "arm64"),
            "x86_64": extract_partial(
                args.x86_64_release_dir, "x86_64", root / "x86_64"
            ),
        }
        manifests = {
            architecture: read_manifest(runtime / "runtime-manifest.json")
            for architecture, runtime in runtimes.items()
        }
        validate_manifests(manifests)
        validate_shared_files(runtimes)

        combined = root / "combined" / "codexthrift-runtime"
        shutil.copytree(runtimes["arm64"], combined, symlinks=True)
        shutil.copytree(
            runtimes["x86_64"] / "x86_64", combined / "x86_64", symlinks=True
        )
        merged_manifest = {
            "schemaVersion": 1,
            "artifact": "codexthrift-runtime-macos-universal.tar.zst",
            "coreVersion": manifests["arm64"]["coreVersion"],
            "sourceCommit": manifests["arm64"]["sourceCommit"],
            "pythonVersion": manifests["arm64"]["pythonVersion"],
            "opensslVersions": {
                architecture: manifests[architecture]["opensslVersions"][architecture]
                for architecture in ARCHITECTURES
            },
            "excludedPackages": PRUNED_RUNTIME_PACKAGES,
            "architectures": {
                architecture: manifests[architecture]["architectures"][architecture]
                for architecture in ARCHITECTURES
            },
        }
        write_json(combined / "runtime-manifest.json", merged_manifest)
        args.out_dir.mkdir(parents=True, exist_ok=True)
        write_json(args.out_dir / "runtime-manifest.json", merged_manifest)
        os.environ.setdefault(
            "SOURCE_DATE_EPOCH", git_timestamp(merged_manifest["sourceCommit"])
        )
        create_archive(
            combined,
            args.out_dir / "codexthrift-runtime-macos-universal.tar.zst",
        )
    return 0


def extract_partial(release_dir: Path, architecture: str, destination: Path) -> Path:
    expected = {
        f"codexthrift-runtime-macos-{architecture}.tar.zst",
        "runtime-manifest.json",
    }
    actual = {path.name for path in release_dir.iterdir() if path.is_file()}
    if actual != expected:
        raise RuntimeError(f"{architecture} Runtime partial file set is invalid")
    archive = release_dir / f"codexthrift-runtime-macos-{architecture}.tar.zst"
    destination.mkdir(parents=True)
    tar_path = destination / "runtime.tar"
    with tar_path.open("wb") as output:
        subprocess.run(["zstd", "-d", "--stdout", str(archive)], check=True, stdout=output)
    with tarfile.open(tar_path) as value:
        validate_archive_members(value.getmembers())
        value.extractall(destination, filter="data")
    tar_path.unlink()
    runtime = destination / "codexthrift-runtime"
    embedded = read_manifest(runtime / "runtime-manifest.json")
    published = read_manifest(release_dir / "runtime-manifest.json")
    if embedded != published:
        raise RuntimeError(f"{architecture} embedded Runtime manifest mismatch")
    present_architectures = {name for name in ARCHITECTURES if (runtime / name).is_dir()}
    if present_architectures != {architecture}:
        raise RuntimeError(f"{architecture} partial Runtime directory set is invalid")
    return runtime


def read_manifest(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"Runtime manifest must be an object: {path}")
    return value


def validate_manifests(manifests: dict[str, dict[str, object]]) -> None:
    reference = manifests["arm64"]
    for architecture, manifest in manifests.items():
        if manifest.get("schemaVersion") != 1:
            raise RuntimeError(f"{architecture} Runtime schema is invalid")
        if set(manifest.get("architectures", {})) != {architecture}:
            raise RuntimeError(f"{architecture} Runtime manifest architecture is invalid")
        expected_artifact = f"codexthrift-runtime-macos-{architecture}.tar.zst"
        if manifest.get("artifact") != expected_artifact:
            raise RuntimeError(f"{architecture} Runtime artifact name is invalid")
        openssl_versions = manifest.get("opensslVersions")
        if (
            not isinstance(openssl_versions, dict)
            or set(openssl_versions) != {architecture}
            or not isinstance(openssl_versions[architecture], str)
            or not openssl_versions[architecture].startswith("OpenSSL ")
        ):
            raise RuntimeError(f"{architecture} Runtime OpenSSL version is invalid")
        if manifest.get("excludedPackages") != PRUNED_RUNTIME_PACKAGES:
            raise RuntimeError(f"{architecture} Runtime excluded packages are invalid")
        for field in ("coreVersion", "sourceCommit", "pythonVersion"):
            if manifest.get(field) != reference.get(field):
                raise RuntimeError(f"partial Runtime {field} values differ")
    for package in MATCHING_PACKAGES:
        versions = {
            architecture: package_version(manifest, architecture, package)
            for architecture, manifest in manifests.items()
        }
        if len(set(versions.values())) != 1:
            raise RuntimeError(f"partial Runtime {package} versions differ")


def validate_archive_members(members: list[tarfile.TarInfo]) -> None:
    for member in members:
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise RuntimeError(f"unsafe partial Runtime entry: {member.name}")
        if path.parts[0] != "codexthrift-runtime":
            raise RuntimeError(f"unexpected partial Runtime entry: {member.name}")


def package_version(
    manifest: dict[str, object], architecture: str, package: str
) -> str:
    architectures = manifest.get("architectures")
    if not isinstance(architectures, dict):
        raise TypeError(f"{architecture} Runtime architectures are invalid")
    entries = architectures.get(architecture)
    if not isinstance(entries, list):
        raise TypeError(f"{architecture} Runtime package list is invalid")
    for entry in entries:
        if (
            isinstance(entry, dict)
            and str(entry.get("name", "")).lower() == package.lower()
            and isinstance(entry.get("version"), str)
        ):
            return str(entry["version"])
    raise RuntimeError(f"{package} missing from {architecture} Runtime")


def validate_shared_files(runtimes: dict[str, Path]) -> None:
    for relative in SHARED_FILES:
        values = {
            architecture: sha256(runtime / relative)
            for architecture, runtime in runtimes.items()
        }
        if len(set(values.values())) != 1:
            raise RuntimeError(f"partial Runtime shared file differs: {relative}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_timestamp(commit: object) -> str:
    if not isinstance(commit, str) or not commit:
        raise RuntimeError("merged Runtime sourceCommit is invalid")
    return subprocess.check_output(
        ["git", "show", "-s", "--format=%ct", commit], cwd=ROOT, text=True
    ).strip()


if __name__ == "__main__":
    raise SystemExit(main())
