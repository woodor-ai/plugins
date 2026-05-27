---
name: talkto
description: Send a message to another registered Claude session by name via the meeting-room system. Use this skill for ANY outbound message to a peer session — direct /talkto invocations AND natural-language requests like "tell bob X", "ask carol Y", "给 lag-runtime 打个招呼".
---

## When to invoke this skill

Invoke whenever the user wants you to communicate with a peer session, in any of these forms:

- Direct command: `/talkto <peer> <optional message text>`
- Natural language: "tell `<peer>` X", "ask `<peer>` Y", "give `<peer>` Z", "你给 `<peer>` 打个招呼", "问 `<peer>` 一下…"

The presence of a peer session name (from `~/.agent-meeting/directory.json`) anywhere in the user prompt — combined with an instruction to convey something — is the trigger.

## Architecture (changed 2026-05-26)

Room state lives in SQLite at `~/.agent-meeting/db/rooms.db`, accessed via the `room` CLI at `~/.agent-meeting/bin/room`. There are no canonical `.md` files to read or write anymore. All writes are atomic transactions (insert message + flip turn in one BEGIN IMMEDIATE). No mtime checks. No tmp files. No Edit/Write tool on room files.

## Steps

1. **Verify self is registered**: read `~/.agent-meeting/directory.json`, check that this session's name is present. If not, refuse and tell user to run `/meeting <name>` first.
2. **Verify peer exists in directory**: if `<peer>` not present, list available peers and refuse.
3. **Read recent room history (optional but recommended)**: `~/.agent-meeting/bin/room show <self> <peer> --limit=20`. Skip if you already have full context.
4. **Turn check (advisory, not blocking)**: `~/.agent-meeting/bin/room turn <self> <peer>`.
   - If output is `<self>` → normal case, send your message.
   - If output is `<peer>` → peer is expected to respond next. You MAY still send when the user explicitly asks for a follow-up or you have a non-deferrable addition. Don't refuse on this basis alone.
   - The room may not exist yet — that's fine, `room send` will create it on first message.
5. **Compose your message body** (markdown, ≤30 lines is the soft norm).
6. **Send via the CLI** (one atomic transaction inserts msg + flips turn). Three body modes — pick by content:

   **Mode A — inline (short, no shell-special chars)**:
   ```
   ~/.agent-meeting/bin/room send <self> <peer> "short safe body" --kind=回应 [--ask="..."]
   ```
   Unsafe if body has `` ` ``, `$(...)`, `$VAR`, unescaped `"`. → Use Mode C instead.

   **Mode B — stdin via `-`**:
   ```
   cat /tmp/body.md | ~/.agent-meeting/bin/room send <self> <peer> - --kind=回应
   ```

   **Mode C — `--body-file` (recommended for bodies with code blocks, backticks, $vars)**:
   ```
   # Write tool → /tmp/talkto-body.md with the full body content
   ~/.agent-meeting/bin/room send <self> <peer> --body-file=/tmp/talkto-body.md --kind=开启|回应|总结 [--ask="..."]
   ```

   `--kind=开启` for first message, `回应` for follow-up, `总结` for wrap-up.
   The CLI prints `sent: room=<name> msg_id=<N> turn→<peer>` on success.

   **Never prefix the command with `bash`** — the script's shebang is `#!/usr/bin/env python3`. `bash <path>` will crash with shell parse errors.
7. **Brief confirm to user**: one short line like "→ sent to lag-rct (msg #42, turn → lag-rct)". No long summary.

After sending, the peer's monitor will detect the new message within ~3 seconds (it polls `room ring`) and their Claude will compose a reply.

## On incoming RING (handled by meeting skill's monitor, not by this skill)

See `meeting` skill's "Behavior on incoming new-message event" section — same `room` CLI is used for the reply.

## What NOT to do

- Do NOT Read or Write `~/.agent-meeting/rooms/canonical/*.md` directly. Those files are legacy snapshots from before the SQLite migration; they're stale. All truth lives in the DB.
- Do NOT use the Edit or Write tools on any room file. Use only the `room` CLI for room state.
- Do NOT compose multi-step shell sequences that stat/lock/rename — the CLI's single-call `send` handles all of that atomically.
