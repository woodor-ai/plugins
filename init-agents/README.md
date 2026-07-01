# init-agents

> Set up cost-tiered subagents in one command so routine work never burns premium tokens.

Stop overpaying for simple work. This sets up your project so routine tasks go to a cheaper model and only the hard architectural calls go to the expensive one — automatically, with one command. You get the right answer without burning premium tokens on grunt work.

Part of [Woodor Plugins](https://github.com/woodor-ai/plugins) — the open-source toolkit for running AI agents at scale.

## Install

```bash
/plugin marketplace add woodor-ai/plugins
/plugin install init-agents@woodor
```

Compatible with Claude Code.

## What it does

Running `/init-agents` creates three subagent profile files under `<cwd>/.claude/agents/` — one per tier. These are project-local files (not global), so each project gets its own configuration without affecting other workspaces.

If any of the three files already exist, the skill reads the current content and shows it to you before asking whether to overwrite, skip, or merge. Nothing is replaced silently. If your working directory doesn't look like a project root (no `.git`, `package.json`, `Cargo.toml`, or `pyproject.toml`), the skill asks for confirmation before writing anything.

The built-in `Explore`, `Plan`, and `general-purpose` agents are unaffected — they still exist, but after running `/init-agents` you use the three named profiles instead.

## The three tiers

| Profile | Model | Reasoning | Tools | Purpose |
|---------|-------|-----------|-------|---------|
| `explore` | `claude-haiku-4-5-20251001` | — | Bash, Read, Glob, Grep, WebFetch, WebSearch | Read-only fact finding: locate code, grep patterns, fetch URLs, list files. Cannot write or edit. |
| `rd` | `claude-sonnet-5` | xhigh | Read, Edit, Write, Bash, Glob, Grep | Write code, edit files, run builds and tests, fix bounded bugs. Design must already be decided. |
| `planner` | `claude-opus-4-8` | high | Read, Glob, Grep, WebFetch, WebSearch, Bash, TodoWrite | Architecture decisions, trade-off evaluation, cross-subsystem root cause, PR scope planning. |

Dispatch rule: if `explore` can answer the question, don't call `rd`. If the design is already decided, use `rd` — don't call `planner`. Only call `planner` when the question is genuinely "which direction should we go."

## How to use them

When your main agent needs to dispatch a subagent, set `subagent_type` to one of the three profile names:

```
subagent_type: "explore"   # look something up
subagent_type: "rd"        # write or fix code
subagent_type: "planner"   # decide architecture
```

The main agent remains responsible for all dispatch decisions. None of the three profiles can spawn further subagents — escalation always flows back up to the main agent first.

## License

MIT — see [LICENSE](../LICENSE).
