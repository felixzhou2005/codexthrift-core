# CodexThrift Core v0.2.1

Security and release-consistency update.

## Fixed

- Regenerated the dedicated universal macOS Runtime lock from the reviewed `uv.lock`.
- Updated the packaged Runtime to LiteLLM 1.96.2 and cryptography 50.0.0.
- Added a release-build gate that rejects a stale `packaging/runtime/requirements.lock` before any artifacts are produced.

## Included

- Open Core runtime, routing, account, configuration, audit, diagnostics, CLI, MCP, daemon, tests, IPC schemas, and packaging.
- Safe Native/LiteLLM switching, Codex client lifecycle management, and Session continuity and migration services.
- Progressive dashboard read models plus validated storage-retention, archive-compression, and maintenance workflows.

## Excluded

- Credentials, OAuth state, user payloads, local databases, logs, runtime state, signing material, and build outputs.
- Native client source and prior private development, migration, and release-history material.

## Verification

Verify the published `SHA256SUMS`, inspect `release-manifest.json`, review the SPDX SBOM and build provenance, and confirm the source commit recorded in the manifest before consuming a binary Runtime artifact.
