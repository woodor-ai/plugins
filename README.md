# Woodor Plugins

> The open-source toolkit for running AI agents at scale.

Five plugin packages cover agent coordination, session continuity, project
profiles, cost controls, and legacy project scaffolding. Host support and
distribution differ by plugin, so use the installation path listed below.

## Install

Complete `agent-meeting` installation automatically detects Claude Code and
Codex and installs both integrations when both clients are present.

macOS / Linux：

```bash
curl -fsSL https://dl.omi-atlas.com/am | python3 -
```

Windows PowerShell：

```powershell
irm https://dl.omi-atlas.com/am | py -3 -
```

The installer downloads an immutable versioned bundle from R2, then creates the
host runtime, user services, PATH entry, and Claude Code/Codex skills. Install
and update never clone a repository or depend on a marketplace checkout. Use
`am-update` for later releases. Preview removal with `am uninstall --dry-run`.

Install the other Claude Code plugins through the Woodor marketplace:

```
/plugin marketplace add woodor-ai/plugins
/plugin install <plugin-name>@woodor
```

Codex marketplace packages currently include `agent-meeting`, `handoff`, and
`init-agents`:

```bash
codex plugin marketplace add woodor-ai/plugins
codex plugin add <plugin-name>@woodor
```

After complete `agent-meeting` installation, open a new terminal and run
`amcodex --name NAME` for a managed Codex session. No repository clone or manual
marketplace-cache access is required.

## The plugins

They split into two jobs — keeping your agents in sync, and keeping your costs in check.

### Manage your agents

#### [`agent-meeting`](./agent-meeting/) — connect agents across windows and machines
Your agents stop working in isolation. Sessions running in different windows — or on different machines — can message each other, chat as a group, and pull each other in to help. Built on mDNS discovery and a SQLite-backed daemon, so there's no server to set up.

Use the complete installer above. Claude Code exposes `/imagent` and `/talkto`;
Codex exposes `$imagent` and `$talkto`.

#### [`init-agents`](./init-agents/) — cost-tiered subagents in one command
Install three project-local profiles for Codex or Claude Code: `explore` for
read-only investigation, `rd` for bounded implementation, and `planner` for
high-impact planning and review.

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

Codex users install the same plugin with `codex plugin add handoff@woodor`.

#### [`save-money`](./save-money/) — lower your bill without changing how you work
Four Claude Code hooks cover auto-handoff, oversized-output truncation, image
delegation, and main-agent edit delegation. The first three are opt-in; edit
delegation is opt-out.

```
/plugin install save-money@woodor
```

### Create projects

#### [`init-proj`](./init-proj/) — legacy AMBridge project wrapper

This Claude Code-only wrapper calls the private AMBridge project-creation
command. It is not standalone and Windows director launch is not supported.

## Documentation

Start with [`docs/INDEX.md`](./docs/INDEX.md). Release and R2 publishing rules
are in [`docs/RELEASE.md`](./docs/RELEASE.md).

## License

MIT — see [LICENSE](./LICENSE).
