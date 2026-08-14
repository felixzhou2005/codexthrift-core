# CodexThrift Core v0.2.0

Initial public-source release.

## Included

- Open Core runtime, routing, account, configuration, audit, diagnostics, CLI, MCP, daemon, tests, IPC schemas, and packaging.
- Safe Native/LiteLLM switching, Codex client lifecycle management, and Session continuity and migration services.
- Progressive dashboard read models plus validated storage-retention, archive-compression, and maintenance workflows.
- A clean public Git history with one root commit and no imported private repository history.
- Public release tooling, a low-privilege verification workflow, and a manually triggered release workflow.

## Excluded

- Credentials, OAuth state, user payloads, local databases, logs, runtime state, signing material, and build outputs.
- Native client source and prior private development, migration, and release-history material.

## Verification

Verify the published `SHA256SUMS`, inspect `release-manifest.json`, review the SPDX SBOM, and confirm the source commit recorded in the manifest before consuming a binary Runtime artifact.
