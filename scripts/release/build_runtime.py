from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

from release_lib import ROOT, git_commit, project_version, run, write_json

PYTHON_VERSION = "3.13.12"
ALLOWED_MACHO_DEPENDENCY_PREFIXES = (
    "@executable_path/",
    "@loader_path/",
    "@rpath/",
    "/System/Library/",
    "/usr/lib/",
)
PRUNED_RUNTIME_PACKAGES = {
    "pyroscope-io": "upstream macOS wheels reference an unbundled absolute build-machine library",
}
PYTHON_TARGETS = {
    "arm64": f"cpython-{PYTHON_VERSION}-macos-aarch64-none",
    "x86_64": f"cpython-{PYTHON_VERSION}-macos-x86_64-none",
}
RUST_TARGETS = {
    "arm64": "aarch64-apple-darwin",
    "x86_64": "x86_64-apple-darwin",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument(
        "--requirements",
        type=Path,
        default=ROOT / "packaging/runtime/requirements.lock",
    )
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument(
        "--python-cache", type=Path, default=Path.home() / ".cache/codexthrift/python"
    )
    parser.add_argument("--source-commit", default=git_commit())
    parser.add_argument(
        "--architecture",
        action="append",
        choices=tuple(PYTHON_TARGETS),
        dest="architectures",
        help="Build only the selected Runtime architecture; repeat for more than one.",
    )
    args = parser.parse_args()
    architectures = tuple(dict.fromkeys(args.architectures or PYTHON_TARGETS))
    verify_rust_targets(architectures)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="codexthrift-runtime-") as temporary:
        stage = Path(temporary) / "codexthrift-runtime"
        stage.mkdir()
        write_wrappers(stage)
        packages: dict[str, list[dict[str, str]]] = {}
        openssl_versions: dict[str, str] = {}
        for architecture in architectures:
            target = PYTHON_TARGETS[architecture]
            python_root = ensure_python(args.python_cache, target)
            packages[architecture] = build_architecture(
                architecture=architecture,
                python_root=python_root,
                stage=stage,
                wheel=args.wheel.resolve(),
                requirements=args.requirements.resolve(),
            )
            openssl_versions[architecture] = runtime_openssl_version(
                stage, architecture
            )
        artifact = (
            "codexthrift-runtime-macos-universal.tar.zst"
            if set(architectures) == set(PYTHON_TARGETS)
            else f"codexthrift-runtime-macos-{architectures[0]}.tar.zst"
        )
        manifest = {
            "schemaVersion": 1,
            "artifact": artifact,
            "coreVersion": project_version(),
            "sourceCommit": args.source_commit,
            "pythonVersion": PYTHON_VERSION,
            "opensslVersions": openssl_versions,
            "excludedPackages": PRUNED_RUNTIME_PACKAGES,
            "architectures": packages,
        }
        write_json(stage / "runtime-manifest.json", manifest)
        write_json(args.out_dir / "runtime-manifest.json", manifest)
        archive = args.out_dir / artifact
        create_archive(stage, archive)
    return 0


def ensure_python(cache: Path, target: str) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    root = cache / target
    executable = root / "bin/python3.13"
    if not executable.exists():
        run(
            ["uv", "python", "install", target, "--install-dir", str(cache), "--no-bin"]
        )
    if not executable.exists():
        raise RuntimeError(f"uv did not install {target}")
    return root


def verify_rust_targets(architectures: tuple[str, ...]) -> None:
    if shutil.which("rustc") is None:
        raise RuntimeError("rustc is required to build the macOS Runtime")
    missing = []
    for architecture in architectures:
        target = RUST_TARGETS[architecture]
        try:
            output = subprocess.check_output(
                ["rustc", "--print", "target-libdir", "--target", target],
                text=True,
            ).strip()
        except subprocess.CalledProcessError:
            missing.append(target)
            continue
        if not any(Path(output).glob("libstd-*")):
            missing.append(target)
    if missing:
        targets = " ".join(missing)
        raise RuntimeError(f"missing Rust targets; run: rustup target add {targets}")


def build_architecture(
    *,
    architecture: str,
    python_root: Path,
    stage: Path,
    wheel: Path,
    requirements: Path,
) -> list[dict[str, str]]:
    destination = stage / architecture
    shutil.copytree(python_root, destination / "python", symlinks=True)
    site_packages = destination / "site-packages"
    site_packages.mkdir()
    python = python_root / "bin/python3.13"
    run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--target",
            str(site_packages),
            "--requirements",
            str(requirements),
            "--require-hashes",
            "--link-mode",
            "copy",
            "--compile-bytecode",
        ]
    )
    run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--target",
            str(site_packages),
            "--no-deps",
            "--link-mode",
            "copy",
            str(wheel),
        ]
    )
    prune_nonportable_optional_packages(site_packages)
    verify_portable_macho_files(site_packages)
    subprocess.run(
        runtime_command(
            stage,
            architecture,
            [
                "-c",
                "import litellm; import litellm.proxy.proxy_server; import pydantic_core",
            ],
        ),
        env=runtime_env(stage, architecture),
        check=True,
    )
    command = runtime_command(
        stage,
        architecture,
        [
            "-c",
            "import json,importlib.metadata as m; print(json.dumps(sorted([{'name': d.metadata['Name'], 'version': d.version} for d in m.distributions()], key=lambda x: x['name'].lower())))",
        ],
    )
    output = subprocess.check_output(
        command, text=True, env=runtime_env(stage, architecture)
    )
    packages = json.loads(output)
    required = {item["name"].lower(): item["version"] for item in packages}
    for name in ("codexthrift", "litellm", "fastapi"):
        if name not in required:
            raise RuntimeError(f"{name} missing from {architecture} runtime")
    return packages


def prune_nonportable_optional_packages(site_packages: Path) -> None:
    package_directories = [site_packages / "pyroscope", site_packages / "pyroscope_io"]
    dist_info = list(site_packages.glob("pyroscope_io-*.dist-info"))
    if len(dist_info) != 1:
        raise RuntimeError("expected exactly one pyroscope-io dist-info directory")
    for path in [*package_directories, dist_info[0]]:
        if not path.is_dir() or path.is_symlink() or path.parent != site_packages:
            raise RuntimeError(f"unsafe or missing optional Runtime package path: {path}")
        shutil.rmtree(path)


def verify_portable_macho_files(root: Path) -> None:
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in {".dylib", ".so"}:
            continue
        output = subprocess.check_output(["otool", "-L", str(path)], text=True)
        validate_macho_dependencies(path, output)


def validate_macho_dependencies(path: Path, output: str) -> None:
    for line in output.splitlines():
        if not line[:1].isspace():
            continue
        dependency = line.strip().partition(" (compatibility version")[0]
        if dependency.startswith("/") and not dependency.startswith(
            ALLOWED_MACHO_DEPENDENCY_PREFIXES
        ):
            raise RuntimeError(
                f"non-portable Mach-O dependency in {path}: {dependency}"
            )


def runtime_openssl_version(stage: Path, architecture: str) -> str:
    output = subprocess.check_output(
        runtime_command(
            stage,
            architecture,
            [
                "-c",
                "from cryptography.hazmat.backends.openssl.backend import backend; print(backend.openssl_version_text())",
            ],
        ),
        text=True,
        env=runtime_env(stage, architecture),
    ).strip()
    if not output.startswith("OpenSSL "):
        raise RuntimeError(f"invalid {architecture} Runtime OpenSSL version: {output}")
    return output


def write_wrappers(stage: Path) -> None:
    bin_dir = stage / "bin"
    bin_dir.mkdir()
    python = bin_dir / "python"
    python.write_text(
        """#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
ARCH=${CODEXTHRIFT_RUNTIME_ARCH:-$(uname -m)}
case "$ARCH" in
  arm64|aarch64) ARCH=arm64 ;;
  x86_64) ARCH=x86_64 ;;
  *) echo "Unsupported CodexThrift Runtime architecture: $ARCH" >&2; exit 78 ;;
esac
export CODEXTHRIFT_RUNTIME_ROOT="$ROOT"
export PYTHONHOME="$ROOT/$ARCH/python"
export PYTHONPATH="$ROOT/$ARCH/site-packages"
exec "$PYTHONHOME/bin/python3.13" "$@"
""",
        encoding="utf-8",
    )
    entry = stage / "runtime_entry.py"
    entry.write_text(
        """from __future__ import annotations
import sys
from importlib.metadata import distribution

name = sys.argv.pop(1)
entrypoint = next(item for item in distribution(name).entry_points if item.group == "console_scripts" and item.name == name)
sys.argv[0] = name
raise SystemExit(entrypoint.load()())
""",
        encoding="utf-8",
    )
    (bin_dir / "litellm").write_text(
        '#!/bin/sh\nset -eu\nROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)\nexec "$ROOT/bin/python" "$ROOT/runtime_entry.py" litellm "$@"\n',
        encoding="utf-8",
    )
    (bin_dir / "codexthriftd-python").write_text(
        '#!/bin/sh\nset -eu\nROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)\nexec "$ROOT/bin/python" -m codexthrift.daemon.main "$@"\n',
        encoding="utf-8",
    )
    for path in bin_dir.iterdir():
        path.chmod(0o755)


def runtime_env(stage: Path, architecture: str) -> dict[str, str]:
    env = os.environ.copy()
    env["CODEXTHRIFT_RUNTIME_ARCH"] = architecture
    return env


def runtime_command(stage: Path, architecture: str, arguments: list[str]) -> list[str]:
    command = [str(stage / "bin/python"), *arguments]
    if architecture == "x86_64":
        return ["arch", "-x86_64", *command]
    return command


def create_archive(stage: Path, output: Path) -> None:
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))
    with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as handle:
        tar_path = Path(handle.name)
    try:
        with tarfile.open(tar_path, "w", format=tarfile.PAX_FORMAT) as archive:
            archive.add(
                stage,
                arcname=stage.name,
                filter=lambda info: normalize_tar(info, epoch),
            )
        run(["zstd", "-10", "--threads=0", "--force", str(tar_path), "-o", str(output)])
        output.chmod(0o644)
    finally:
        tar_path.unlink(missing_ok=True)


def normalize_tar(info: tarfile.TarInfo, epoch: int) -> tarfile.TarInfo:
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "wheel"
    info.mtime = epoch
    return info


if __name__ == "__main__":
    raise SystemExit(main())
