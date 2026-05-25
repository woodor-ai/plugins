---
name: talkto
description: Send a message to another registered Claude session by name via the meeting-room system. Use this skill for ANY outbound message to a peer session — direct /talkto invocations AND natural-language requests like "tell bob X", "ask carol Y", "给 lag-runtime 打个招呼".
---

## When to invoke this skill

Invoke whenever the user wants you to communicate with a peer session, in any of these forms:

- Direct command: `/talkto <peer> <optional message text>`
- Natural language: "tell `<peer>` X", "ask `<peer>` Y", "give `<peer>` Z", "你给 `<peer>` 打个招呼", "问 `<peer>` 一下…"

The presence of a peer session name (from `~/.claude/plugins/data/agent-meeting/directory.json`) anywhere in the user prompt — combined with an instruction to convey something — is the trigger.

## Steps

1. **Verify self is registered**: check that this session is in `~/.claude/plugins/data/agent-meeting/directory.json`. If not, refuse and tell user to run `/meeting <name>` first.
2. **Verify peer exists**: read directory.json. If `<peer>` not present, list available peers and refuse.
3. **Compute canonical room path** (this is the ONLY path you Read or Write):
   - `sorted = sort([self_name, peer])`  (lexicographic)
   - `canonical_path = ~/.claude/plugins/data/agent-meeting/rooms/canonical/${sorted[0]}--${sorted[1]}.md`
4. **Create canonical room file if missing**:
   - Copy `~/.claude/plugins/data/agent-meeting/templates/room-header.md` to canonical_path
   - Substitute placeholders ({{a}}, {{b}}, {{now}}) with actual values
   - Initial `当前发言权: <self>` (you write first, then flip)
5. **Create symlinks both sides** (purely for the monitor to detect mtime; not for read/write):
   - `mkdir -p ~/.claude/plugins/data/agent-meeting/rooms/<self> ~/.claude/plugins/data/agent-meeting/rooms/<peer>`
   - `ln -sf <canonical_path> ~/.claude/plugins/data/agent-meeting/rooms/<self>/<peer>.md`
   - `ln -sf <canonical_path> ~/.claude/plugins/data/agent-meeting/rooms/<peer>/<self>.md`
6. **Read** the canonical room file (so you have full message history + protocol).
7. **Turn check (advisory, not blocking)**: look at `当前发言权:` in the file.
   - If it's `<self>` → normal case, write your message.
   - If it's `<peer>` → peer is expected to respond next. **You MAY still write** when the user explicitly asks you to send another message before peer replies, or when you have a follow-up that shouldn't wait. Do not refuse on this basis alone.
   - After writing in any case, flip turn → `<peer>` to invite their next response.
8. **Compose your message** in the protocol format from the room header:

   ```
   ### [<self> @ <YYYY-MM-DD HH:MM>] <开启|回应|总结>
   <body, ≤30 lines>

   **Ask**: <one-line specific request, optional>
   ```

   - "开启" if this is the first message in the room
   - "回应" if this is a follow-up
   - Body content is inferred from the user's current request + recent conversation context

9. **Flip turn**: update the line `当前发言权: <self>` → `当前发言权: <peer>`
10. **Write** the ENTIRE updated file back using the **canonical path** in step 3. **Never write through the view symlink** at `rooms/<self>/<peer>.md` — the Write tool will refuse with "Refusing to write through symlink".
11. **Brief confirm to user**: one short line like "→ written to room (alice ↔ bob), turn → bob". No long summary.

After writing, the peer's phone Monitor will RING within ~3 seconds and their Claude will read and reply.

## On incoming RING (handled by Monitor notification, not by this skill)

Same write protocol applies — when monitor emits `RING peer=<peer> canonical=<path>`, Read the canonical path, compose reply, flip turn, Write canonical path.
