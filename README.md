# Woodor Plugins

> The open-source toolkit for running AI agents at scale.

Four plugins that work as one system: keep your AI spend under control, and keep your agents working together instead of in isolation. Free, open-source, installed in seconds.

## Install

```
/plugin marketplace add woodor-ai/plugins
/plugin install <plugin-name>@woodor
```

Compatible with Claude Code.

## The plugins

They split into two jobs — keeping your agents in sync, and keeping your costs in check.

### Manage your agents

#### [`agent-meeting`](./agent-meeting/) — connect agents across windows and machines
Your agents stop working in isolation. Sessions running in different windows — or on different machines — can message each other, chat as a group, and pull each other in to help. Built on mDNS discovery and a SQLite-backed daemon, so there's no server to set up.

```
/plugin install agent-meeting@woodor
```

#### [`init-agents`](./init-agents/) — cost-tiered subagents in one command
Set up three subagent profiles — `explore` (cheap), `rd` (mid), `planner` (premium) — so routine work goes to a cheaper model and only the hard calls reach the expensive one. No premium tokens burned on grunt work.

```
/plugin install init-agents@woodor
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
