# CodexThrift for macOS 0.2.0

The first signed direct-download release of the CodexThrift macOS App.

这是 CodexThrift macOS App 第一个经过签名、Apple 公证并可直接下载的版本。

## Download / 下载

- **App:** `0.2.0`
- **Bundled Core / 内置 Core:** `0.2.4`
- **Requirements / 系统要求:** macOS 13+
- **Architectures / 架构:** Apple Silicon (`arm64`) and Intel (`x86_64`)
- **Package / 安装包:** `CodexThrift_0.2.0_universal.dmg`

## Highlights / 主要功能

- Luna, Terra, and Sol routing with task-level explanations
- One-click native Codex recovery
- Logical-task and redacted API call audit
- Actual spend, provider-reported amount, and Standard API-equivalent cost kept separate
- Subscription usage and reset windows
- Safe SQLite reduction, retention cleanup, and compressed encrypted archives
- macOS background-service management
- Local-first data and credential boundaries

中文：Luna、Terra、Sol 智能路由与任务级解释；一键安全切回原生 Codex；逻辑任务与脱敏调用审计；订阅额度、成本分析、安全存储维护和 macOS 后台服务管理。

## Installation / 安装

Open the DMG, drag CodexThrift into Applications, launch it, and follow the in-app instructions if macOS requires background-service approval.

打开 DMG，将 CodexThrift 拖入“应用程序”，启动 App；如果 macOS 要求批准后台服务，请按照 App 内提示操作。

## Verification / 验证

The DMG is Developer ID signed, Apple notarized, and stapled. Verify downloaded files against the attached `SHA256SUMS` before installation.

DMG 已通过 Developer ID 签名、Apple 公证并附加公证票据。安装前可以使用随 Release 提供的 `SHA256SUMS` 校验下载文件。

```text
8862cc892e5edceabbc3720f589157d3f40bc795e6102f6578986da37b4f9c81  CodexThrift_0.2.0_universal.dmg
```

## Notes / 说明

- Automatic updates are not enabled in this release; future versions must be downloaded manually.
- The App requires an existing account with access to the Codex capabilities being used.
- CodexThrift is independent and is not affiliated with or endorsed by OpenAI.
- Core source is MIT licensed. The downloadable commercial macOS App and its private source are outside the Core MIT license.

中文：当前版本尚未启用自动更新；用户需要已有可使用相应 Codex 能力的账户；CodexThrift 是独立项目，与 OpenAI 没有关联；商业 macOS App 及其私有源码不属于 Core MIT 许可范围。
