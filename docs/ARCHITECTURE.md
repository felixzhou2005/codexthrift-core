# CodexThrift Core Architecture

## Repository Boundary

CodexThrift Core is the open, local-first routing and observability engine. It consumes LiteLLM as a dependency and manages a local Codex routing deployment; it is not a LiteLLM fork.

The public Core repository owns Python application logic, protocols, storage, security helpers, tests, and daemon APIs. Native macOS UI, signing, packaging, updater, Keychain integration, and App Store delivery belong to the private `codexthrift-macos` repository.

## Application Boundary

`RouterController` is a compatibility Facade. It preserves existing CLI, MCP, Dashboard, and JSON contracts while delegating behavior to services under `codexthrift.application`:

1. `RuntimeService`: environment checks, generated configurations, staged phase activation, runtime installation, and active phase/profile state.
2. `ProxyService`: LiteLLM process lifecycle, listener verification, authenticated health checks, and redacted failure output.
3. `CodexModeService`: routed/native Codex configuration, route preflight, client restart status, verification, and rollback.
4. `ProbeService`: subscription model probes, probe result validation, and probe-gated phase activation.
5. `OAuthService`: subscription OAuth process lifecycle and redacted device-flow status.
6. `QuotaService`: application boundary around Codex App Server account, quota, login, logout, and reset-credit operations.
7. `RoutingService`: reports, adaptive evaluation, shadow/canary settings, and task-history import.
8. `TaskService`: guarded repository task execution and task process lifecycle.
9. `DiagnosticsService`: setup guidance, aggregate status, logs, recent calls/turns, explanations, and feedback.

`ApplicationServices` creates one shared `ServiceContext` containing the resolved workspace paths, bind address, port, and temporary compatibility Facade reference. Services do not rediscover global workspace state.

## Infrastructure Boundary

Application services import their owning infrastructure modules directly:

- `storage.atomic_io`: atomic text/JSON writes, backups, JSON reads, timestamps, and log tails.
- `security.path_guard`: explicit confirmations and home-directory path restrictions.
- `security.redaction`: text and structured-value credential redaction.
- `platform.process`: command lookup, child environments, PID inspection, listener verification, detached process start, and guarded termination.
- `errors`: user-safe `ControlError` failures.

`util.py` remains a compatibility export layer for callers not yet migrated. New application services do not depend on it.

## Runtime Components

1. `codexthriftd`: the single local writer, exposing JSON-RPC 2.0 over a mode-`0600` Unix Domain Socket with protocol handshake, write serialization, and event long polling.
2. Python Core: routing configuration, classification, auditing, task verification, and local service control.
3. Dashboard compatibility API: a temporary FastAPI adapter on `127.0.0.1:4876` exposing redacted runtime state and indexed analytics for the existing desktop prototype.
4. Quota adapter: a native Codex App Server session with a process-local provider override and permission-checked local identity fallback.
5. Router service: LiteLLM Proxy on `127.0.0.1:4000`, supervised by the platform-specific application layer.
6. Local analytics store: SQLite normalized call records, logical turns, quota snapshots, and service events; raw JSONL remains immutable evidence.

## Data Rules

- Persist hashes and redacted metadata in the audit index. When local content capture is enabled, raw request and response bodies are written only as AES-GCM encrypted payloads; credentials are filtered before encryption.
- Treat logical tasks as the quality and cost unit; raw calls are supporting evidence.
- Keep requested, configured, observed, and verified reasoning effort as separate fields.
- Classify OAuth, rate limit, network, and local authentication failures separately from model-quality failures.
- Keep existing legacy runtime paths and identifiers stable until a dedicated compatibility migration phase supplies rollback and coexistence behavior.
