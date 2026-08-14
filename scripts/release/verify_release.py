from __future__ import annotations

import argparse
import json
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path

from release_lib import project_version, release_artifact_names, run_output, sha256


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", type=Path, required=True)
    args = parser.parse_args()
    release = args.release_dir.resolve()
    version = project_version()
    expected = release_artifact_names(version)
    actual = {path.name for path in release.iterdir() if path.is_file()}
    missing = sorted(expected - actual)
    if missing:
        raise SystemExit(f"Missing release artifacts: {missing}")
    unexpected = sorted(actual - expected)
    if unexpected:
        raise SystemExit(f"Unexpected release artifacts: {unexpected}")
    verify_checksums(release)
    verify_architectures(release)
    release_manifest = json.loads(
        (release / "release-manifest.json").read_text(encoding="utf-8")
    )
    runtime_manifest = json.loads(
        (release / "runtime-manifest.json").read_text(encoding="utf-8")
    )
    if release_manifest.get("artifacts") != sorted(expected):
        raise SystemExit("Release manifest artifact set mismatch")
    if release_manifest.get("sourceCommit") != runtime_manifest.get("sourceCommit"):
        raise SystemExit("Release and Runtime source commits differ")
    if not isinstance(release_manifest.get("sourceTreeClean"), bool):
        raise SystemExit("Release manifest sourceTreeClean must be boolean")
    json.loads(
        (release / f"codexthrift-core-v{version}.spdx.json").read_text(encoding="utf-8")
    )
    with tempfile.TemporaryDirectory(prefix="codexthrift-release-verify-") as temporary:
        runtime = extract_runtime(
            release / "codexthrift-runtime-macos-universal.tar.zst", Path(temporary)
        )
        verify_runtime(runtime, release)
    return 0


def verify_checksums(release: Path) -> None:
    expected = release_artifact_names(project_version()) - {"SHA256SUMS"}
    seen = set()
    for line in (release / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, name = line.split("  ", 1)
        if name in seen:
            raise SystemExit(f"Duplicate checksum entry: {name}")
        seen.add(name)
        if sha256(release / name) != digest:
            raise SystemExit(f"Checksum mismatch: {name}")
    if seen != expected:
        raise SystemExit("Checksum artifact set mismatch")


def verify_architectures(release: Path) -> None:
    expectations = {
        "codexthriftd-arm64-apple-darwin": {"arm64"},
        "codexthriftd-x86_64-apple-darwin": {"x86_64"},
        "codexthriftd-universal-apple-darwin": {"arm64", "x86_64"},
    }
    for name, expected in expectations.items():
        actual = set(run_output(["lipo", "-archs", str(release / name)]).split())
        if actual != expected:
            raise SystemExit(f"Unexpected architectures for {name}: {sorted(actual)}")


def extract_runtime(archive: Path, temporary: Path) -> Path:
    tar_path = temporary / "runtime.tar"
    with tar_path.open("wb") as output:
        subprocess.run(
            ["zstd", "-d", "--stdout", str(archive)], check=True, stdout=output
        )
    with tarfile.open(tar_path) as value:
        value.extractall(temporary, filter="data")
    return temporary / "codexthrift-runtime"


def verify_runtime(runtime: Path, release: Path) -> None:
    manifest = json.loads(
        (runtime / "runtime-manifest.json").read_text(encoding="utf-8")
    )
    published_manifest = json.loads(
        (release / "runtime-manifest.json").read_text(encoding="utf-8")
    )
    if manifest != published_manifest:
        raise SystemExit("Embedded and published Runtime manifests differ")
    if manifest["coreVersion"] != project_version():
        raise SystemExit("Runtime Core version mismatch")
    commands = [
        ([str(release / "codexthriftd-arm64-apple-darwin"), "--self-test"], "arm64"),
        (
            [
                "arch",
                "-x86_64",
                str(release / "codexthriftd-x86_64-apple-darwin"),
                "--self-test",
            ],
            "x86_64",
        ),
        (
            [str(release / "codexthriftd-universal-apple-darwin"), "--self-test"],
            "arm64",
        ),
        (
            [
                "arch",
                "-x86_64",
                str(release / "codexthriftd-universal-apple-darwin"),
                "--self-test",
            ],
            "x86_64",
        ),
    ]
    for command, architecture in commands:
        env = os.environ.copy()
        env["CODEXTHRIFT_RUNTIME_ROOT"] = str(runtime)
        env["CODEXTHRIFT_RUNTIME_ARCH"] = architecture
        subprocess.run(command, env=env, check=True)


if __name__ == "__main__":
    raise SystemExit(main())
