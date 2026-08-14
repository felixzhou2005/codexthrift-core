# CodexThrift Runtime Lock

`requirements.lock` is generated from `uv.lock` with the `runtime` extra and excludes the local project wheel. Release builds install this hash-pinned dependency set into both fixed CPython architectures, then install the newly built CodexThrift wheel without resolving dependencies again.

The pinned LiteLLM package builds native Rust extensions on macOS. The release toolchain therefore requires both targets declared in `rust-toolchain.toml`; the build fails before dependency installation when either standard library is unavailable.

Regenerate after an intentional runtime dependency change:

```bash
uv lock
uv export --extra runtime --no-emit-project --format requirements-txt --output-file packaging/runtime/requirements.lock
```
