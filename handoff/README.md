# handoff

> Pick up exactly where you left off — no re-explaining, no lost context.

Never re-explain where you left off. When a session ends, it leaves a short note — what's done, what's pending, what to do next — and your next session picks up right where you stopped. The card is written by you (or your agent) in under a minute and injected automatically into the next session before the first message lands.

Part of [Woodor Plugins](https://github.com/woodor-ai/plugins) — the open-source toolkit for running AI agents at scale.

## Install

```
/plugin marketplace add woodor-ai/plugins
/plugin install handoff@woodor
```

Compatible with Claude Code.

## How it works

**Step 1 — write the card.** At the end of a session, call `/handoff`. The skill writes a compact cue card to `.claude/handoff-pending.md` inside your current working directory (the real shell `pwd`, not the git root — so it works correctly across multiple agent worktrees pointing at the same repo).

**Step 2 — automatic pickup.** The next time you open a session in that project, the `SessionStart` hook fires, reads the pending card, moves it to `docs/handoff/archive/handoff-<timestamp>.md`, and injects its content as `additionalContext` before your first message. No copy-paste. No manual re-loading. If there is no pending card the hook exits silently.

The archive rename is atomic — if two sessions start at the same instant, only one picks up the card. The other sees nothing and stays quiet.

## The handoff card

Cards are capped at **50 lines**. If the draft exceeds that, the skill tells you to compress before writing. Three sections, always in this order:

1. **In-flight** — what was being worked on when the session ended.
2. **Pending decisions** — anything blocked on a user choice or external event.
3. **First step** — one concrete, actionable thing the next session should do immediately (a command, a file to read, a subagent to dispatch).

Empty sections get a placeholder line rather than being omitted. Cards must not copy project-state documents verbatim — use pointers (`see PLAN.md §2`, `see commit abc1234`) to stay under the line limit and avoid stale duplication.

**Example card:**

```
# Handoff 2026-06-17 14:30

## In-flight
Refactoring the session-state serializer. Branch: feat/serializer-v2.
Last commit: abc1234 — split encode/decode into separate modules.

## Pending decisions
Decide whether the archive format should be JSON or MessagePack (see PLAN.md §3.1).

## First step
Run `npm test -- --grep serializer` to confirm the split didn't break existing tests,
then open src/serializer/decode.ts and continue from TODO on line 88.
```

## Hooks

`hooks.json` registers a single `SessionStart` hook that fires on four matchers: `startup`, `resume`, `clear`, and `compact`. All four call `python3 ${CLAUDE_PLUGIN_ROOT}/bin/handoff-pickup.py` (with `py -3` and `python` as fallbacks for Windows and older PATH setups).

The pickup script resolves the project directory in priority order: `stdin.cwd` → `CLAUDE_PROJECT_DIR` → `os.getcwd()`. It creates `docs/handoff/archive/` if needed, then does an atomic `os.rename` to claim the pending file. A rename failure means another process already claimed it — the script exits silently rather than injecting twice.

## License

MIT — see [LICENSE](../LICENSE).
