# Core Release Process

## Build

The Runtime build compiles native LiteLLM extensions for both macOS architectures. Use the pinned rustup toolchain from `rust-toolchain.toml`, and install Rosetta when building on Apple Silicon.

```bash
rustup show
uv lock --check
python3 scripts/release/build_release.py
```

The command requires `HEAD` to match the requested source commit and the worktree to be clean. It builds into the ignored `packaging/out/core-v<version>` directory and validates every artifact before returning success. `--allow-dirty` exists only for local pipeline development and records `sourceTreeClean: false`; never publish those outputs.

`Core Verify` runs read-only dependency and test checks for pull requests and pushes to `main`. It cannot publish a release.

`Core Release` is manually dispatched from an existing tag only. Its `release_tag` input and the selected Git ref must both exactly equal `core-v<project version>`. The build job has read-only repository permission. A separate publish job receives the minimum write permissions required to attest artifacts and create the GitHub Release.

## Runtime Contract

The universal Runtime archive contains independent `arm64` and `x86_64` CPython 3.13.12 trees plus a hash-pinned site-packages directory for each architecture. The top-level wrappers select the correct tree using `CODEXTHRIFT_RUNTIME_ARCH` or the current machine architecture. No first-run package installation is required.

The native `codexthriftd` launcher locates the Runtime through `CODEXTHRIFT_RUNTIME_ROOT`, `Contents/Resources/codexthrift-runtime`, or an adjacent extracted Runtime directory. It sets the selected architecture and executes `codexthrift.daemon.main` from the bundled Core wheel.

## Required Artifacts

- wheel and sdist
- arm64, x86_64, and universal daemon launchers
- universal macOS Runtime archive
- runtime and release manifests
- SPDX 2.3 SBOM
- source archive and release notes
- `SHA256SUMS`

## Publication Gate

Before creating or dispatching a public release tag:

1. Run the public-release audit and resolve every secret, provenance, identity, and scope finding.
2. Verify the source commit is on protected `main` and has passed `Core Verify`.
3. Complete the dependency-license and attribution review for the exact lockfile and Runtime contents.
4. Enable GitHub private vulnerability reporting and protect the `public-release` environment with required reviewers.
5. Verify the public GitHub repository description, license display, default branch protection, and release visibility.

## Publication Boundary

Local builds use ad-hoc signatures only so macOS can inspect Mach-O metadata. The public Core pipeline may publish these artifacts and provenance. The private App pipeline must verify hashes, embed the selected artifacts, and apply final Developer ID signing, hardened runtime, notarization, and updater signing.
