# cost-auto-handoff

Claude Code Stop hook that watches context token usage and drops a trigger
file when a session exceeds its configured threshold. AMBridge (amp) polls
the trigger directory and issues /handoff + kill+respawn.

## Protocol contract

### Config (amp writes, hook reads)
`~/.claude/cost-opt.json`
```json
{ "auto_handoff": { "enabled": false,
    "thresholds_pct": { "opus": 60, "sonnet": 70, "haiku": 80 } } }
```
`thresholds_pct` maps model family → % of context window at which to trigger.

### Trigger files (hook writes, amp reads)
Directory: `~/.ambridge/handoff-triggers/`
Filename: `<agent-meeting-name>.json`
Written atomically (tmp + rename) so amp never reads a partial file.
```json
{ "agent": "plugins-wic", "reason": "auto-handoff",
  "context_tokens": 152000, "threshold_tokens": 120000, "ts": 1718500000 }
```

### Context token formula
`input_tokens + cache_creation_input_tokens + cache_read_input_tokens`
(from last assistant message's `usage` block in the session transcript JSONL)

## Window size assumptions (v1)

`WINDOW_TOKENS = {"opus": 1_000_000, "sonnet": 1_000_000, "haiku": 200_000}`

Reflects the actual deployment: opus and sonnet run the 1M-context beta;
haiku runs the 200k standard window. The effective window is NOT detectable
from transcript fields or Stop hook stdin (no `context_window` field exists),
so we assert it by family.

Erring large is the safe direction: if a session is not actually on 1M, it
auto-compacts near 200k and this (too-high) threshold simply never fires —
no false restart. The reverse (assuming 200k on a real 1M session) would
fire at ~120k for opus, far too early.

Edit the map in `hook.py` if the deployment changes (e.g. haiku gains a 1M
window, or a new family is added).

## Absolute fire floor (MIN_FIRE_TOKENS = 100 000)

In addition to the per-family pct threshold, the hook enforces an absolute
lower bound of **100 000 context tokens** before it will fire, regardless of
how low `thresholds_pct` is set.

**Why it exists — restart-loop guard.**  
Dedup tracks fires by `session_id`. A killed+respawned session gets a fresh
`session_id`, so the dedup flag from the previous run does not protect it. If
the pct threshold is set aggressively low (e.g. haiku 20% → 40k), a newly
spawned session's baseline context (system prompt + handoff card + tool
definitions + CLAUDE.md) can already exceed that threshold before the user
types a single message. That causes the very first Stop hook of the new session
to re-trigger → another kill+respawn → instant loop.

**When it matters.**  
Production thresholds are 600k (opus 60% × 1M) and 160k (haiku 80% × 200k),
both well above 100k, so the floor has zero effect in normal operation. It only
activates when someone sets a pct so low that the derived threshold falls below
100k — at which point the floor overrides it and the new session cannot
immediately self-trigger.

**Effect on trigger file.**  
`threshold_tokens` in the written JSON reflects the *effective* threshold
(i.e. `max(pct_derived, 100_000)`), so amp/debug tooling sees the actual
decision boundary, not the raw pct arithmetic.

## Dedup flag (v1)

To prevent the same session from firing multiple times (e.g. several Stop
hooks in quick succession near the threshold), the hook writes an empty flag
file at:

```
~/.cache/cost-auto-handoff/fired/<session_id>
```

on first trigger. Subsequent Stop hooks for the same `session_id` detect the
flag and exit immediately, skipping the meeting-list subprocess entirely.

If stdin has no `session_id`, dedup is skipped on the hook side (AMBridge's
own `_restarting` guard provides a second layer of protection in that case).

**v1 does not clean up fired flags.** They are empty files keyed by
session_id; the total volume is negligible.

## Installation

1. Copy or symlink `hook.py` to `~/.claude/hooks/cost-auto-handoff.py`
2. Make it executable: `chmod +x ~/.claude/hooks/cost-auto-handoff.py`
3. Add to `~/.claude/settings.json` (see snippet below)
4. Create `~/.claude/cost-opt.json` with desired thresholds (amp manages this)

### settings.json Stop hook registration snippet

```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python3 /Users/tommyclaw/.claude/hooks/cost-auto-handoff.py"
          }
        ]
      }
    ]
  }
}
```

## Running tests

```
python3 tools/cost-auto-handoff/test_hook.py
```

All tests run in isolated temp directories; no live files are touched.
