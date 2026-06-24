# init-proj

Scaffold a brand-new Tommy-standard project in one shot.

`/init-proj <name>` creates `~/AIAgent/<name>/` and fills it with the full baseline:

- a fresh `git init`
- the three tiered subagent profiles in `.claude/agents/` — `explore` (Haiku 4.5, read-only lookup), `rd` (Sonnet 4.6, coding), `planner` (Opus 4.8, strategy)
- a `.gitignore` that keeps `.claude/settings.local.json` (the local API key) out of git
- an **interactively-entered project API key** that never touches the conversation transcript (you type it via a `!`-prefixed `read -rs`, it lands straight in `settings.local.json`)
- a launched **agent-meeting director** session (Mac: iTerm2 + tmux, mirroring how amp launches agents; Windows: TODO)

If any step after directory creation fails, the freshly-created directory is rolled back — no half-built projects left behind.

## init-proj vs init-agents

- **init-agents** — add the three agent profiles to an **existing** repo.
- **init-proj** — stand up a **new** project from nothing (directory + git + agents + key + director). It inlines the same three agent templates as one of its steps.

## Security note

The API-key step is deliberately driven by *you*, not the agent: a `!`-prefixed shell command reads the key silently (`read -rs`), writes it to `.claude/settings.local.json`, and unsets the variable. The agent never sees, echoes, or reads back the key. `.gitignore` keeps that file out of version control.
