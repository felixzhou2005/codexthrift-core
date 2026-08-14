# CodexThrift Core

CodexThrift Core 是面向 Codex 与 LiteLLM 的开放、本地优先路由和可观测性核心。它提供 Runtime、路由、账户、配置、审计、诊断、CLI、MCP 和 daemon 能力，可在没有原生应用外壳的情况下独立运行 Core。

CodexThrift 是独立的非官方项目，与 OpenAI 不存在隶属、认可、赞助或官方合作关系。

## 公开源码基线

本仓库从 `0.2.0` 开始公开 Git 历史，只包含受支持的 Core 源码、测试、版本化 IPC schema、打包和公开发布工具。此前的私有开发历史、本地运行状态、凭据、用户数据、签名材料、构建产物和原生客户端源码均不在本仓库中。

原生客户端只能消费正式发布的 Core Artifact 和版本化本地 IPC 协议；Core 永远不会导入或依赖客户端私有源码。详见 `docs/REPOSITORY_BOUNDARY.md` 与 `docs/ARCHITECTURE.md`。

`codexthriftd` 是通过权限受限 Unix Domain Socket 提供 JSON-RPC 的本地写入者。Dashboard API 与 Store 仍作为本地分析和诊断的兼容适配层受支持。

正常 CLI 使用前先启动 `codexthriftd`。所有 CLI 命令默认通过 IPC；`--direct` 只用于明确的 Bootstrap 或恢复。协议说明见 `docs/IPC_PROTOCOL.md`。

开发验证：

```bash
uv sync --extra dev
uv run pytest
```

提交依赖变更前运行 `uv lock --check`。创建 tag 或公开发布前阅读 `docs/RELEASE.md`。

## 安全与许可

不要提交 OAuth token、访问 token、LiteLLM master key、Codex 认证状态、审计日志、运行时数据库、签名证书、更新签名密钥或用户配置备份。

本源码仓库使用 MIT 许可证。发布二进制 Runtime Artifact 前，维护者必须完成 `THIRD_PARTY_NOTICES.md` 和 `docs/RELEASE.md` 规定的依赖许可与归属审查。
