# handoff

> Pick up exactly where you left off — no re-explaining, no lost context.

Never re-explain where you left off. When a session ends, it leaves a short note — the current breakpoint, pending decisions, and next actions — and your next session picks up right where you stopped. The card is written by you or your agent and injected automatically into the next session before the first message lands.

Part of [Woodor Plugins](https://github.com/woodor-ai/plugins) — the open-source toolkit for running AI agents at scale.

## Install

```
/plugin marketplace add woodor-ai/plugins
/plugin install handoff@woodor
```

Compatible with Claude Code and Codex. Claude Code uses `/handoff`; Codex uses
`$handoff` or the built-in skills picker. Both hosts write the same three-section
card, while their pending-file directories remain host-specific.

## Update

After installing handoff 0.6.3 or newer, start one new Claude Code or Codex
session. Its SessionStart hook installs the stable `handoff-update` command
under `~/.handoff/bin` and adds that directory to the user `PATH`. Open a new
terminal after the first bootstrap, then update every installed host with:

```sh
handoff-update
```

The updater detects whether `handoff@woodor` is installed in Claude Code,
Codex, or both. It refreshes the Woodor marketplace and updates only those
installed integrations. It uses the public host CLIs and never reads a Git
checkout or plugin cache directly.

```sh
handoff-update --check
handoff-update --target claude-code
handoff-update --target codex
```

The command is installed without administrator privileges on macOS, Linux,
and Windows. Start a new Codex thread or restart Claude Code after an update so
the host loads the refreshed skill and hooks.

## How it works

**Step 1 — write the card.** At the end of a session, invoke the handoff skill. It uses the current conversation as its primary source and writes a compact cue card inside the real shell working directory: `.claude/handoff-pending.md` for Claude Code or `.codex/handoff-pending.md` for Codex. It reads project files only when the conversation does not establish the exact breakpoint or next action.

**Step 2 — automatic pickup.** The next time you open a session in that project, the `SessionStart` hook fires, reads the pending card, moves it to `docs/handoff/archive/handoff-<timestamp>.md`, and injects its content as `additionalContext` before your first message. No copy-paste. No manual re-loading. If there is no pending card the hook exits silently.

The archive rename is atomic — if two sessions start at the same instant, only one picks up the card. The other sees nothing and stays quiet.

## The handoff card

Cards are capped at **30 lines** and contain exactly three sections:

1. **Current breakpoint** — unfinished work, with narrow file, command, commit, or state-document pointers when useful.
2. **Pending user decisions** — only choices or external events that block progress.
3. **Next actions and leftover todos** — the first action is what the next session should do immediately; remaining actions follow one per line.

Empty sections contain `None` instead of being omitted. The card records session-specific deltas rather than restating project background, roadmaps, architecture documents, Git history, or diffs.

**Example card:**

```
# Handoff 2026-08-06 14:30 PDT

## 1. Current breakpoint
- Serializer encode/decode split is complete in commit abc1234.
- Streaming API wiring has not started; the relevant contract is in docs/serializer-migration.md §2.

## 2. Pending user decisions
- Choose JSON or MessagePack for the archive format; see PLAN.md §3.1.

## 3. Next actions and leftover todos
- Run `npm test -- --grep serializer`, then continue from src/serializer/decode.ts:88.
- Add the decode error-path test after the streaming API wiring passes.
```

When a new session picks up this card, it adds every action in section 3 to its task list before starting work. If section 3 says `None`, it creates no task.

## Hooks

`hooks.json` registers a `SessionStart` hook for `startup`, `resume`, `clear`,
and `compact`. The shared host adapter selects `.claude` or `.codex` from the
active plugin environment, then delegates to the common pickup implementation.

The pickup script resolves the project directory in priority order: `stdin.cwd` → `CLAUDE_PROJECT_DIR` → `os.getcwd()`. It creates `docs/handoff/archive/` if needed, then does an atomic `os.rename` to claim the pending file. A rename failure means another process already claimed it — the script exits silently rather than injecting twice.

## License

MIT — see [LICENSE](../LICENSE).
