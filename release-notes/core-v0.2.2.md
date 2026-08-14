# CodexThrift Core v0.2.2

Universal Runtime build-infrastructure update.

## Fixed

- Builds the arm64 Runtime natively on Apple Silicon and the x86_64 Runtime natively on an Intel runner.
- Safely merges architecture-specific Runtime archives only after their source commit, Core version, Python version, manifests, and shared wrappers match.
- Keeps the Runtime lock-consistency gate introduced in 0.2.1.
- Packages LiteLLM 1.96.2 and cryptography 50.0.0 for both macOS architectures.

## Security

- Runtime archive extraction rejects absolute paths, traversal, and entries outside the expected Runtime root.
- Credentials, OAuth state, user payloads, local databases, logs, runtime state, and signing material remain excluded.

## Verification

Verify `SHA256SUMS`, `release-manifest.json`, the SPDX SBOM, build provenance, and the recorded source commit before consuming the Runtime.
