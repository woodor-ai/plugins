# cost-truncate-output

Claude Code PostToolUse hook that intercepts large text tool outputs and replaces
them with a head+pointer+tail summary before they reach the model. The full output
is saved to `/tmp` and a pointer line tells the model where to find it.

This is **hook 2/3** of the cost optimization suite:
- Hook 1: `cost-auto-handoff` (Stop) — handoff when context is nearly full
- Hook 2: `cost-truncate-output` (PostToolUse) — truncate large outputs in-context  ← this one
- Hook 3: (planned)

## Protocol contract

### Config (read-only by this hook)
`~/.claude/cost-opt.json`
```json
{ "text_truncate": { "enabled": true, "threshold_tokens": 25000 } }
```

**Default behavior when config is missing or malformed: enabled=true, threshold=25000.**
This is the opposite of hook 1 (which defaults off) because truncation is a non-destructive
guard — a false negative (not truncating when you should) wastes context; there is no
operational downside to truncating by default.

Only an explicit `"enabled": false` disables the hook.

### Token-to-character approximation
The hook has no access to a tokenizer at runtime. It approximates:

```
1 token ≈ 4 characters  (CHARS_PER_TOKEN = 4)
```

25 000 token threshold → 100 000 character threshold.
Head kept: 5 000 tokens (20 000 chars). Tail kept: 3 000 tokens (12 000 chars).
The approximation errs toward truncating slightly earlier than necessary, which
is safe for this use case.

## Image output: iron rule

**Any output that contains image data is always passed through unchanged.**

Detection logic:
- `Bash` tool: `tool_response.isImage == true`
- `Read` and other tools: `tool_response` is a list of content blocks containing
  a block with `"type": "image"`

Truncating base64 image data would silently corrupt it. When in doubt, pass through.

## Supported tools

| Tool | Output structure | Truncation target |
|------|-----------------|-------------------|
| `Bash` | `{stdout, stderr, interrupted, isImage}` | `stdout` field only; other fields preserved |
| `Read` | plain string (line-number-prefixed text) | the entire string |
| All others | unknown | **always passed through** |

Unknown/unrecognized tool output is passed through without modification. It is safer to
let a large MCP tool output through than to reconstruct an unknown schema incorrectly.

## What the model sees

When truncation fires, the model receives something like:
```
<first 20000 chars of output>

[输出过大已截断（原始 250000 字符 ≈ 62500 token），完整内容存于 /tmp/cost-truncate-Bash-abc123.txt，需要某段时读该文件]

<last 12000 chars of output>
```

The pointer tells the model exactly where to read if it needs a section it did not see.

## Error handling

Any exception inside the hook exits with code 0 and a one-line stderr message.
The original tool output is preserved. The hook must never break a tool call.

## Installation

1. Copy or symlink `hook.py` to `~/.claude/hooks/cost-truncate-output.py`
2. Make it executable: `chmod +x ~/.claude/hooks/cost-truncate-output.py`
3. Add the PostToolUse registration to `~/.claude/settings.json` (snippet below)
4. Ensure `~/.claude/cost-opt.json` has a `text_truncate` block (or rely on defaults)

### settings.json PostToolUse registration snippet

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python3 /Users/tommyclaw/.claude/hooks/cost-truncate-output.py"
          }
        ]
      }
    ]
  }
}
```

`matcher: ""` means the hook fires for every tool. The hook itself skips quickly (exit 0)
for images and small outputs, so the overhead is minimal.

## Running tests

```
python3 tools/cost-truncate-output/test_hook.py
```

All tests run in isolated temp directories; no live files are touched.
