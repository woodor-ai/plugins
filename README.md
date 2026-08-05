# Woodor Plugins

> The open-source toolkit for running AI agents at scale.

Four plugins that work as one system: keep your AI spend under control, and keep your agents working together instead of in isolation. Free, open-source, installed in seconds.

## Install

完整安装 `agent-meeting`（自动检测 Claude Code、Codex，二者都存在时同时安装）：

macOS / Linux：

```bash
curl -fsSL https://dl.omi-atlas.com/plugins | python3 -
```

Windows PowerShell：

```powershell
irm https://dl.omi-atlas.com/plugins | py -3 -
```

安装器固定下载已发布版本，并建立主机运行时、后台服务、PATH 和原生插件集成。
如需卸载，先运行 `am uninstall --dry-run` 查看范围，再运行 `am uninstall`；
无人值守环境可用 `am uninstall --yes`。

Claude Code：

```
/plugin marketplace add woodor-ai/plugins
/plugin install <plugin-name>@woodor
```

Codex：

```bash
codex plugin marketplace add woodor-ai/plugins
codex plugin add <plugin-name>@woodor
```

安装 `agent-meeting` 后启动一个新的 Codex 会话并调用 `$imagent`。首次使用会
自动安装 Windows、macOS 或 Linux 主机运行时；无需克隆仓库或手动进入 marketplace
缓存目录。自举完成后新开终端，直接运行 `amcodex <name>` 启动托管会话。

Claude Code 支持仓库中的全部插件；Codex 原生 marketplace 当前包含 `agent-meeting`、`handoff` 和 `init-agents`。

## The plugins

They split into two jobs — keeping your agents in sync, and keeping your costs in check.

### Manage your agents

#### [`agent-meeting`](./agent-meeting/) — connect agents across windows and machines
Your agents stop working in isolation. Sessions running in different windows — or on different machines — can message each other, chat as a group, and pull each other in to help. Built on mDNS discovery and a SQLite-backed daemon, so there's no server to set up.

```
/plugin install agent-meeting@woodor
```

#### [`init-agents`](./init-agents/) — cost-tiered subagents in one command
为 Claude Code 或 Codex 设置三个 project-local profile：`explore` 只读调查、`rd` 实现与验证、`planner` 规划与独立审查。模型和 reasoning 档位按宿主分别配置。

```
/plugin install init-agents@woodor
```

```bash
codex plugin add init-agents@woodor
```

### Manage your cost

#### [`handoff`](./handoff/) — never re-explain where you left off
At session end, write a short cue card — what's done, what's pending, what to do next. The next session picks it up automatically and archives it. No copy-pasting context to get going again.

```
/plugin install handoff@woodor
```

#### [`save-money`](./save-money/) — lower your bill without changing how you work
Three background hooks: auto-handoff and restart before you blow your budget, truncation of oversized tool output, and routing image reads to a cheaper subagent. All off by default, opt in per feature.

```
/plugin install save-money@woodor
```

## License

MIT — see [LICENSE](./LICENSE).
