# CodexThrift Repository Boundary

## Public Core

The public `codexthrift-core` repository owns:

- LiteLLM runtime configuration, probing, routing callbacks, health checks, and proxy lifecycle.
- Codex configuration switching, process detection, App Server integration, quota, reset credits, and compact operations.
- Routing policy, task aggregation, verification, cost and savings calculations, audit evidence, SQLite projections, diagnostics, redaction, and backups.
- `codexthriftd`, CLI, MCP adapters, IPC schemas, tests, compatibility fixtures, packaging, and public documentation.

## Private macOS Application

The private `codexthrift-macos` repository owns:

- React/Tauri UI, onboarding, menu bar, notifications, product navigation, and visualization.
- SMAppService integration, Core artifact verification, Apple signing, notarization, DMG creation, and updater publication.
- Brand assets, product analytics, crash reporting, commercial licensing, and future private product features.

## Temporary Compatibility Code

The initial Core migration retains `dashboard.py`, `dashboard_api.py`, `dashboard_store.py`, and `launchd.py`. These files are not declarations that the official UI belongs in Core. They currently hold daemon, data, service, and security-sensitive behavior that must remain open and testable while the architecture is decomposed.

## Preserved Legacy Identifiers

The initial package rename intentionally preserves existing data and compatibility identifiers, including RouterLLM runtime paths, SQLite filenames, LaunchAgent labels, encryption AAD values, audit field names, and provider ownership fields.

Changing those values before a migration layer exists could orphan encrypted payloads, configuration snapshots, service registrations, or local databases. They will be changed only with explicit compatibility and rollback tests.

## Dependency Rule

```text
codexthrift-macos -> released codexthrift-core artifact + public IPC schema
codexthrift-core  -X-> codexthrift-macos
```

The private repository must not contain a copy of `src/codexthrift` and must not import Core source directly.
