from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

from release_lib import (
    ROOT,
    git_commit,
    project_version,
    release_artifact_names,
    run,
    run_output,
    sha256,
    write_json,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, default=ROOT / "packaging/out")
    parser.add_argument(
        "--python-cache", type=Path, default=Path.home() / ".cache/codexthrift/python"
    )
    parser.add_argument("--source-commit", default=git_commit())
    parser.add_argument("--release-tag", default="")
    parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args()
    version = project_version()
    source_tree_clean = verify_source_state(args.source_commit, args.allow_dirty)
    verify_release_tag(version, args.release_tag)
    release = args.out_dir / f"core-v{version}"
    shutil.rmtree(release, ignore_errors=True)
    release.mkdir(parents=True)
    environment = os.environ.copy()
    environment.setdefault("SOURCE_DATE_EPOCH", git_timestamp(args.source_commit))

    run(["uv", "build", "--out-dir", str(release)], env=environment)
    (release / ".gitignore").unlink(missing_ok=True)
    run(
        ["python3", "scripts/release/build_launchers.py", "--out-dir", str(release)],
        env=environment,
    )
    wheel = release / f"codexthrift-{version}-py3-none-any.whl"
    run(
        [
            "python3",
            "scripts/release/build_runtime.py",
            "--wheel",
            str(wheel),
            "--out-dir",
            str(release),
            "--python-cache",
            str(args.python_cache),
            "--source-commit",
            args.source_commit,
        ],
        env=environment,
    )
    source = release / f"codexthrift-{version}-source.tar.gz"
    run(
        [
            "git",
            "archive",
            "--format=tar.gz",
            f"--prefix=codexthrift-{version}/",
            "--output",
            str(source),
            args.source_commit,
        ]
    )
    shutil.copy2(
        ROOT / f"release-notes/core-v{version}.md", release / "RELEASE_NOTES.md"
    )
    runtime = json.loads(
        (release / "runtime-manifest.json").read_text(encoding="utf-8")
    )
    manifest = {
        "schemaVersion": 1,
        "releaseTag": f"core-v{version}",
        "coreVersion": version,
        "protocolVersion": "1.0",
        "sourceCommit": args.source_commit,
        "sourceTreeClean": source_tree_clean,
        "pythonVersion": runtime["pythonVersion"],
        "litellmVersion": package_version(runtime, "litellm"),
        "artifacts": sorted(release_artifact_names(version)),
    }
    write_json(release / "release-manifest.json", manifest)
    run(
        ["python3", "scripts/release/generate_sbom.py", "--release-dir", str(release)],
        env=environment,
    )
    write_checksums(release)
    run(["python3", "scripts/release/verify_release.py", "--release-dir", str(release)])
    print(release)
    return 0


def verify_source_state(source_commit: str, allow_dirty: bool) -> bool:
    head = git_commit()
    if source_commit != head:
        raise RuntimeError(f"source commit {source_commit} does not match HEAD {head}")
    status = run_output(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=ROOT
    )
    if status and not allow_dirty:
        raise RuntimeError("release builds require a clean worktree")
    return not status


def verify_release_tag(version: str, release_tag: str) -> None:
    expected = f"core-v{version}"
    if release_tag and release_tag != expected:
        raise RuntimeError(f"release tag {release_tag} does not match {expected}")


def git_timestamp(commit: str) -> str:
    import subprocess

    return subprocess.check_output(
        ["git", "show", "-s", "--format=%ct", commit],
        cwd=ROOT,
        text=True,
    ).strip()


def package_version(runtime: dict[str, object], name: str) -> str:
    architectures = runtime["architectures"]
    assert isinstance(architectures, dict)
    for entries in architectures.values():
        assert isinstance(entries, list)
        for entry in entries:
            if isinstance(entry, dict) and str(entry.get("name", "")).lower() == name:
                return str(entry["version"])
    raise RuntimeError(f"{name} missing from runtime manifest")


def write_checksums(release: Path) -> None:
    lines = []
    for name in sorted(release_artifact_names(project_version()) - {"SHA256SUMS"}):
        path = release / name
        if not path.is_file():
            raise RuntimeError(f"cannot checksum missing release artifact: {name}")
        lines.append(f"{sha256(path)}  {name}")
    (release / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
