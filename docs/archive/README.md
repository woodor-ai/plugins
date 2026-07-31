# 历史文档归档

这里保存已经完成使命、被当前实现取代，或只适用于旧版本的架构、设计与契约文档。
这些文件用于追溯决策背景，不是当前行为规范；当前使用方式以各插件的 `README.md`、
`agent-meeting/docs/CLI_SURFACE.md`、代码和测试为准。

## Architecture

- [`agent-meeting-codex-tui-bridge.md`](architecture/agent-meeting-codex-tui-bridge.md) — 早期 Codex TUI 桥接调研。
- [`agent-meeting-runtime-architecture.md`](architecture/agent-meeting-runtime-architecture.md) — 0.17.1 阶段运行时架构快照。
- [`codex-adaptation-investigation.md`](architecture/codex-adaptation-investigation.md) — 早期 Codex 能力与适配调查。
- [`plugin-architecture-guidelines.md`](architecture/plugin-architecture-guidelines.md) — 历史跨宿主架构规范。
- [`plugins-codex-architecture-audit.md`](architecture/plugins-codex-architecture-audit.md) — 重构前 Codex 架构审计。

## Designs

- [`am-ctld-agent-lifecycle-control-design.md`](designs/am-ctld-agent-lifecycle-control-design.md) — 生命周期控制阶段性设计与落地记录。
- [`am-msgd-local-relay-multi-bind-design.md`](designs/am-msgd-local-relay-multi-bind-design.md) — 0.16.0 本地中转与多地址监听设计。
- [`init-agents-codex-agent-profile-analysis.zh-CN.md`](designs/init-agents-codex-agent-profile-analysis.zh-CN.md) — init-agents 的 Codex 适配决策背景。

## Contracts

- [`0.8.25-global-admin-identity.md`](contracts/0.8.25-global-admin-identity.md) — 全局身份契约。
- [`0.8.27-control-message-kind.md`](contracts/0.8.27-control-message-kind.md) — 已退出运行路径的控制消息契约。
- [`0.10.0-composite-key-identity.md`](contracts/0.10.0-composite-key-identity.md) — 复合键身份迁移方案。
- [`identity-remap-schema.md`](contracts/identity-remap-schema.md) — 一次性身份重映射格式。
- [`phase2-single-key-targets.md`](contracts/phase2-single-key-targets.md) — 复合键迁移阶段 2 落地记录。

`docs/handoff/archive/` 是 handoff 插件的运行时归档位置，有固定路径契约，因此不并入这里。
