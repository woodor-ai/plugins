# save-money

> Cut your AI bill without touching your workflow.

Lower AI bills, without changing how you work. It quietly steps in before you blow your budget, trims runaway output, sends image reads to a cheaper subagent, and keeps the main agent from doing file edits itself — running in the background so you don't have to think about it.

Part of [Woodor Plugins](https://github.com/woodor-ai/plugins) — the open-source toolkit for running AI agents at scale.

## Install

```
/plugin marketplace add woodor-ai/plugins
/plugin install save-money@woodor
```

Compatible with Claude Code. Hooks register automatically via `hooks/hooks.json` after install. All behavior is controlled by `~/.claude/cost-opt.json` (written by the AMBridge PWA Save Money page).

## What it does

### auto-handoff keeps your session from burning the whole window

When context tokens approach the model's window limit, the Stop hook writes a trigger file that hands the session off and restarts it — picking up exactly where you left off via the handoff plugin. You keep working; the context stays lean.

### text truncate stops oversized output from stacking up

After a `Bash` or `Read` call returns more than the configured threshold, the PostToolUse hook replaces the response with a head snippet, a pointer line with the path to the full content (saved in your system temp directory), and a tail snippet. The full output is still accessible; it just doesn't sit in the main context getting re-read every turn.

### image delegate routes image reads to a cheaper subagent

When the main agent tries to `Read` an image file, the PreToolUse hook blocks it and asks you to dispatch an `explore` subagent instead. The subagent reads the image and returns a text summary — so the raw pixel data never lands in the main context. Subagents are detected via the `agent_id` field in stdin and are always allowed through.

### edit delegate keeps the main agent from touching files directly

When the main agent tries to `Edit` or `Write` a file, the PreToolUse hook blocks it and asks you to dispatch an `rd` (or `explore`) subagent instead — the main agent is the most expensive model in the session, and having it do a one-line edit itself is a double waste (TDP §3.1). Subagents are detected via the `agent_id` field in stdin and are always allowed through, which avoids a deadlock where a dispatched subagent couldn't edit files either.

Unlike the other two hooks, this one is **on by default** (opt-out, not opt-in): a missing config file, a missing `edit_delegate` key, or a missing `enabled` key are all treated as enabled. Only an explicit `"enabled": false` turns it off. There's also a one-shot escape hatch — set `CLAUDE_ALLOW_MAIN_EDIT=1` in the environment to let the main agent edit directly for that call.

## Configuration

Everything is controlled by `~/.claude/cost-opt.json`, written by the AMBridge PWA Save Money page. **`auto_handoff`, `text_truncate`, and `image_delegate` are off by default** (opt-in — a missing key, a `null` value, or an `enabled` set to anything other than `true` is treated as disabled; only an explicit `"enabled": true` activates them). **`edit_delegate` is on by default** (opt-out — only an explicit `"enabled": false` disables it).

Full example:

```json
{
  "auto_handoff": {
    "enabled": true,
    "thresholds_pct": {
      "opus": 60,
      "sonnet": 70,
      "haiku": 80
    }
  },
  "text_truncate": {
    "enabled": true,
    "threshold_tokens": 25000
  },
  "image_delegate": {
    "enabled": true
  },
  "edit_delegate": {
    "enabled": true
  }
}
```

| Feature | Default | How to change | Key defaults |
|---|---|---|---|
| `auto_handoff` | OFF | `"enabled": true` to turn on | opus/sonnet window = 1,000,000 tokens; haiku = 200,000; absolute floor = 100,000 tokens (prevents restart loops); per-session dedup via `~/.cache/cost-auto-handoff/fired/<session_id>`; default thresholds 60% / 70% / 80% |
| `text_truncate` | OFF | `"enabled": true` to turn on | threshold = 25,000 tokens (~100k chars); head = 5,000 tokens; tail = 3,000 tokens; image output always passes through unchanged |
| `image_delegate` | OFF | `"enabled": true` to turn on | intercepts `.png .jpg .jpeg .gif .webp .bmp .svg`; `amb-shot` screenshots in the temp directory are allowlisted (AMBridge self-checks); subagents always pass through |
| `edit_delegate` | ON | `"enabled": false` to turn off | intercepts `Edit` and `Write`; subagents always pass through; `CLAUDE_ALLOW_MAIN_EDIT=1` bypasses per-call |

**Why explicit-true-only matters (opt-in hooks):** `auto_handoff`, `text_truncate`, and `image_delegate` intercept real tool calls. A misconfigured or partially written JSON file should never accidentally activate them. If any key is absent or malformed, the hook exits silently — the conservative default is always off.

**Why `edit_delegate` inverts this:** TDP §3.1 (main agent delegates execution to subagents) is a standing rule, not an experiment to opt into — so this hook defaults on, and a misconfigured or missing JSON file should never accidentally turn it *off*. Only an explicit `"enabled": false` disables it.

## How it relates to handoff

auto-handoff's cost savings depend on the handoff plugin to carry session state across the restart — install both together for it to work end to end.

## License

MIT — see [LICENSE](../LICENSE).
