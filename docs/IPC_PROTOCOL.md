# CodexThrift Local IPC Protocol

## Transport

Protocol `1.0` uses JSON-RPC 2.0 objects framed as one UTF-8 JSON object per line over a Unix Domain Socket. The default socket remains under the managed runtime root as `run/codexthrift.sock`; the run directory is mode `0700` and the socket is mode `0600` on POSIX systems. Frames larger than 1 MiB are rejected.

The legacy runtime root remains in use during the compatibility period. A later data-migration phase will move the default to `~/Library/Application Support/CodexThrift` without silently deleting the legacy root.

## Handshake

Clients must call `core.handshake` and provide `protocolMin` and `protocolMax`. Core returns its package version, selected protocol version, and feature identifiers. Incompatible ranges return JSON-RPC error `-32001`.

## Errors

- `-32700`: invalid JSON.
- `-32600`: invalid JSON-RPC request or oversized frame.
- `-32601`: unknown method.
- `-32602`: invalid params.
- `-32001`: incompatible protocol version.
- `-32004`: known method unavailable in this Core version.
- `-32010`: safe application/control error.

Credential values, OAuth payloads, source content, and unredacted logs must never appear in error data.

## Concurrency

`codexthriftd` owns a non-blocking single-instance file lock. All mutating RPC methods share one write coordinator, so concurrent CLI/App/MCP control requests cannot modify PID files, Codex configuration, or desired state simultaneously.

## Events

Protocol `1.0` exposes `events.subscribe` as bounded long polling. Clients provide the last observed sequence and an optional timeout up to 30 seconds. Core returns ordered events and `latestSequence`. Current base events are:

- `event.service.changed`
- `event.proxy.health`
- `event.account.updated`

## Dashboard Compatibility

Protocol `1.0` exposes `compat.dashboardRequest` for the official App migration. Params are `method` (`GET` or `POST`), `path` (`/healthz` or an existing `/v1/` Dashboard path), and optional JSON object `body`. Core executes the request in-process with an internal authorization token and returns the decoded JSON response as the RPC result.

This method preserves the frozen Dashboard data contract without reopening a loopback HTTP port or copying Dashboard, analytics, routing, credential, or persistence logic into the private App repository. It is a compatibility surface, not permission for arbitrary URLs or HTTP methods; invalid methods, paths, or Dashboard errors are mapped to the safe application error `-32010`.

## Desired State And Startup Recovery

Core persists the requested Proxy state in `data/desired-state.json` with mode `0600`. Successful `proxy.start` and `proxy.restart` calls record `running`; successful `proxy.stop` calls record `stopped`. Failed operations do not replace the last valid target. The requested `allow_unverified` value is retained only while the desired state is `running`.

At daemon startup, a background supervisor reads the file and restores LiteLLM only when `running` was explicitly requested. Recovery is considered successful only after the managed PID, loopback port, and authenticated health endpoint all pass. Invalid state fails closed, while runtime/OAuth/probe failures are reported without terminating the IPC daemon.

- `system.startupStatus` returns the in-memory startup recovery status and the validated desired-state snapshot.
- `system.restoreDesiredState` retries recovery and requires `confirm: "APPLY"`.
- Startup and manual recovery publish `event.service.changed` without exposing credentials or unredacted logs.

## CLI

All existing `codexthrift` commands map to daemon RPC by default. `--direct` is an explicit bootstrap/recovery escape hatch and must not be used by the official App. `codexthriftd --self-test` validates the local protocol implementation without starting the socket server.

## Schemas

The machine-readable request, response, and handshake schemas are in `schemas/`. Additive result fields are allowed only through a documented protocol-compatible change; breaking request or semantic changes require a new protocol version.
