# CodexThrift Core

CodexThrift Core is an open, local-first routing and observability engine for Codex and LiteLLM. It provides the runtime, routing, account, configuration, audit, diagnostics, CLI, MCP, and daemon capabilities needed to operate the Core without a native application shell.

CodexThrift is an independent, unofficial project. It is not affiliated with, endorsed by, or sponsored by OpenAI.

## Public Source Baseline

This repository starts its public history at `0.2.0`. It contains the supported Core source, tests, versioned IPC schemas, packaging, and public release tooling. It intentionally excludes prior private development history, local runtime state, credentials, user data, signing material, build outputs, and native-client source.

Native clients consume formally released Core artifacts and versioned local IPC contracts. Core never imports or requires client-private source code. See `docs/REPOSITORY_BOUNDARY.md` and `docs/ARCHITECTURE.md`.

`codexthriftd` is the local JSON-RPC writer over a permission-restricted Unix domain socket. The Dashboard API and store remain supported compatibility adapters for local analytics and diagnostics.

## Commands

```text
codexthrift
codexthrift-mcp
codexthrift-mcp-readonly
codexthrift-key
codexthriftd
codexthrift-dashboard
```

Start `codexthriftd` before normal CLI use. Every CLI command uses IPC by default; pass `--direct` only for explicit bootstrap or recovery. See `docs/IPC_PROTOCOL.md`.

## Development

```bash
uv sync --extra dev
uv run pytest
```

Run `uv lock --check` before submitting a dependency change. Read `docs/RELEASE.md` before creating a tag or public release.

## Safety

Never commit OAuth tokens, access tokens, refresh tokens, LiteLLM master keys, Codex authentication state, copied audit logs, runtime databases, signing certificates, updater keys, or user-specific configuration backups.

This source repository is MIT licensed. Before publishing binary Runtime artifacts, maintainers must complete the dependency-license and attribution gate described in `THIRD_PARTY_NOTICES.md` and `docs/RELEASE.md`.
