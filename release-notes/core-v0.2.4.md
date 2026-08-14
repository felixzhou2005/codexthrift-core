# CodexThrift Core v0.2.4

Universal Runtime portability, build-infrastructure, and storage-observability reliability update.

## Fixed

- Builds the arm64 Runtime natively on Apple Silicon and the x86_64 Runtime natively on an Intel runner.
- Statically links OpenSSL when building cryptography for macOS Intel, where upstream wheels are no longer published.
- Excludes LiteLLM's optional Pyroscope profiler because its upstream macOS wheels reference an unbundled absolute build-machine library.
- Safely merges architecture-specific Runtime archives only after their source commit, Core version, Python version, manifests, key dependency versions, and shared wrappers match.
- Handles SQLite WAL/SHM sidecar removal while collecting storage statistics without hiding unrelated filesystem errors.
- Keeps the Runtime lock-consistency gate introduced in 0.2.1.
- Packages LiteLLM 1.96.2 and cryptography 50.0.0 for both macOS architectures.

## Security

- Rejects Runtime Mach-O files that link to non-system absolute build-machine paths.
- Records the actual OpenSSL version for each architecture in the Runtime manifest and SPDX SBOM.
- Runtime archive extraction rejects absolute paths, traversal, and entries outside the expected Runtime root.
- Credentials, OAuth state, user payloads, local databases, logs, runtime state, and signing material remain excluded.

## Verification

Verify `SHA256SUMS`, `release-manifest.json`, the SPDX SBOM, build provenance, and the recorded source commit before consuming the Runtime.
