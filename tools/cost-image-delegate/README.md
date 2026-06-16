# cost-image-delegate

Claude Code PreToolUse hook that intercepts the main agent's image Read calls
and denies them, prompting it to delegate to an explore subagent instead.

This is **hook 3/3** of the cost optimization suite:
- Hook 1: `cost-auto-handoff` (Stop) — handoff when context is nearly full
- Hook 2: `cost-truncate-output` (PostToolUse) — truncate large outputs in-context
- Hook 3: `cost-image-delegate` (PreToolUse) — delegate image reads to explore subagent  ← this one

## Why

When the main agent reads an image file, the image (base64-encoded) stays in
its context for every subsequent turn — each turn re-reads it, inflating cost
linearly. An explore subagent spins up a fresh temporary context, reads the
image there, returns a text description, then discards the image entirely.

## Mechanism

1. Fires on every `PreToolUse` event for the `Read` tool.
2. Checks `agent_id` in stdin: **present = subagent call → always allow**.
   This is the deadlock guard — without it, the explore subagent sent to read
   the image would itself be blocked.
3. Checks file extension against the image set (case-insensitive).
4. If main agent + image extension: **deny** with an explanation that tells the
   model to dispatch an explore subagent, plus how to disable the guard.
5. On any exception: **allow** (exit 0). The hook must never break a normal Read.

## Config

`~/.claude/cost-opt.json`
```json
{ "image_delegate": { "enabled": true } }
```

**Opt-in: default OFF.** The guard activates only when `image_delegate.enabled`
is explicitly `true`. Missing config, malformed JSON, a missing `image_delegate`
key, or an unset value all leave it **off** (the Read is allowed). This guard is
invasive — it blocks every main-agent image read globally — so it stays dormant
until explicitly enabled (e.g. via the PWA Save Money toggle) rather than
intercepting the moment it's installed.

To turn it on:
```json
{ "image_delegate": { "enabled": true } }
```

## Image extensions recognized

`.png` `.jpg` `.jpeg` `.gif` `.webp` `.bmp` `.svg` (case-insensitive)

## stdin schema (PreToolUse)

Common fields (always present):
- `session_id` — current session identifier
- `transcript_path` — path to conversation JSONL
- `cwd` — working directory
- `hook_event_name` — `"PreToolUse"`
- `tool_name` — `"Read"` (for Read calls)
- `tool_input.file_path` — path being read

Subagent-only fields (absent for main agent calls):
- `agent_id` — unique subagent identifier; **presence = subagent, absence = main agent**
- `agent_type` — agent name (e.g. `"Explore"`)

Source: https://code.claude.com/docs/en/hooks.md § Common input fields

## Installation

Do **not** copy the file — point settings.json directly at the repo source
so there is no stale copy to maintain.

Add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Read",
        "hooks": [
          {
            "type": "command",
            "command": "python3 /Users/tommyclaw/AIAgent/plugins/tools/cost-image-delegate/hook.py"
          }
        ]
      }
    ]
  }
}
```

`matcher: "Read"` ensures the hook process only spawns for Read tool calls;
other tool calls skip it entirely with no overhead.

## Running tests

```
python3 tools/cost-image-delegate/test_hook.py
```

All tests run in isolated temp directories; no live files are touched.
