from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from release_lib import ROOT, run

ARCHITECTURES = ("arm64", "x86_64")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    source = ROOT / "packaging/launcher/codexthriftd.c"
    outputs: dict[str, Path] = {}
    for architecture in ARCHITECTURES:
        target = args.out_dir / f"codexthriftd-{architecture}-apple-darwin"
        run(
            [
                "xcrun",
                "--sdk",
                "macosx",
                "clang",
                "-Os",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-mmacosx-version-min=13.0",
                "-arch",
                architecture,
                str(source),
                "-o",
                str(target),
            ]
        )
        target.chmod(0o755)
        outputs[architecture] = target
    universal = args.out_dir / "codexthriftd-universal-apple-darwin"
    run(
        [
            "lipo",
            "-create",
            str(outputs["arm64"]),
            str(outputs["x86_64"]),
            "-output",
            str(universal),
        ]
    )
    universal.chmod(0o755)
    if shutil.which("codesign"):
        for path in (*outputs.values(), universal):
            run(["codesign", "--force", "--sign", "-", str(path)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
