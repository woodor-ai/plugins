---
name: meeting
description: Register this session in the meeting-room directory with a chosen name, and install the monitor (start watching for incoming calls). Required before /talkto can be used to or from this session. Backed by SQLite (~/.agent-meeting/db/rooms.db) — all room state lives there, no more .md file fiddling.
argument-hint: [list | delete <peer> | <name>]
---

## Architecture (changed 2026-05-26)

Room storage moved from per-room markdown files to a single SQLite database at `~/.agent-meeting/db/rooms.db`. All reads and writes go through the `room` CLI at `~/.agent-meeting/bin/room`. This eliminates the entire class of bugs we were fighting: Edit/Write races, mtime check hacks, file size limits, manual archive discipline, monitor false positives.

You do NOT read or write canonical `.md` files anymore. The old `rooms/canonical/*.md` and view-symlink dirs are legacy/snapshot only — ignore them.

Session-level registration (`~/.agent-meeting/directory.json`) is unchanged.

## `/meeting` subcommand dispatch

The first word after `/meeting` decides what to do:

| Input | Action |
|---|---|
| `/meeting` (empty) | Show name picker (see "Picker" below) |
| `/meeting list` | Run `~/.agent-meeting/bin/room list` and **paste the TSV output verbatim into your reply as a markdown table** with columns Status / Name / Msgs. Do NOT just say "see above" or "如上" relying on the collapsed bash block — the user wants it visible in the main chat area without expanding. Status is `empty` / `online` / `historical`. |
| `/meeting delete <peer>` | Delete the room between this session's registered name and `<peer>` (hard delete: all messages purged). **Required**: this session must already be registered; ask user for explicit confirmation showing msg count before invoking `~/.agent-meeting/bin/room delete <self> <peer>`. |
| `/meeting <name>` | Register this session as `<name>` (see "On `/meeting <name>`" below) |

Reserved words `list` and `delete` cannot be used as session names — they go to the corresponding subcommand instead.

### Picker (when `/meeting` has no args)

1. Run `~/.agent-meeting/bin/room list` to get session-name candidates. Output is TSV: `<status>\t<name>\t<msgs>` where status is one of:
   - `empty` — registered before but monitor is gone (no live session owns it now) → **safe to take over**, has historical msg context.
   - `online` — registered AND monitor pid alive → picking would conflict with the running session (your registration would overwrite directory.json but their monitor keeps running).
   - `historical` — never in directory but appeared as sender in DB at some point → safe, fully fresh registration.
2. Use the `AskUserQuestion` tool to let user pick. **AskUserQuestion takes 2-4 options that you fill, and TUI auto-appends "Other" on top (does NOT count toward the cap). So you have 4 actual slots, with "Other" as the 5th displayed entry handling anything skipped.**

   **Selection rules — apply in order** (empty first because they're the most likely "I want my old name back" candidates):
   a. ALL `empty` names go in first (sorted by msg count desc — most-active first). These are the recommended take-over candidates.
   b. If slots remain after empty: fill with `online` names (sorted by msg count desc). Reference info — user can still pick to take over with confirmation.
   c. If slots remain after online: fill with `historical` names (sorted by msg count desc).
   d. The auto-added "Other" handles anything skipped — user just types the name.

   **Label / description format**:
   - empty: label=`<name>`, description=`(empty, safe to take over) — <N> msgs`
   - online: label=`<name>`, description=`(online — will conflict if you take it) — <cwd>`
   - historical: label=`<name>`, description=`(historical, safe) — <N> msgs`

3. If user picks an `online` name, ask explicit confirmation before proceeding (their choice may have been informational).
4. After user confirms a name (or types via Other), proceed to "On `/meeting <name>`" below with that name.

## On `/meeting <name>`

1. **Validate name**: alphanumeric + hyphen only, no `--` substring, length 2-20.
2. **Check directory**: read `~/.agent-meeting/directory.json`. If `<name>` already exists with a different `pid`, refuse.
3. **Register**: atomic jq + tmp + mv into directory.json (unchanged from before).
4. **Initialize DB** (idempotent): `~/.agent-meeting/bin/room init`
5. **Install monitor**: invoke Monitor tool with:
   - `description`: `📞 meeting:<name>` (static, TUI banner can't be dynamic)
   - `persistent`: `true`
   - `command`: the zsh script below

```zsh
zsh -c '
SELF="<name>"
STATE_FILE="/tmp/meeting-<name>.last_msg_id"
PID_FILE="/tmp/meeting-<name>.monitor_pid"
ROOM_CLI="$HOME/.agent-meeting/bin/room"

# Write our pid as the liveness signal. room list checks this file via kill -0.
# trap cleans it up when monitor exits (TaskStop / session end / SIGTERM).
echo $$ > "$PID_FILE"
trap "rm -f $PID_FILE" EXIT INT TERM

LAST=$(cat "$STATE_FILE" 2>/dev/null || echo 0)
echo "[meeting <name>] monitor started (sqlite, last_msg_id=$LAST, monitor_pid=$$)"
while true; do
  # Poll DB for new messages in rooms where current_turn=self and sender!=self.
  # Output format: <id>\t<peer>\t<ask>
  while IFS=$'\''\t'\'' read -r id peer ask; do
    [ -z "$id" ] && continue
    if [ -n "$ask" ]; then
      echo "📬 New Message from ${peer}: ${ask}"
    else
      echo "📬 New Message from ${peer}"
    fi
    echo "$id" > "$STATE_FILE"
    LAST="$id"
  done < <("$ROOM_CLI" ring "$SELF" --since "$LAST")
  sleep 3
done
'
```

6. **Update terminal tab title (best-effort)**: `{ printf '\033]0;%s\a' "<name>" > /dev/tty; } 2>/dev/null || true`
7. **Confirm to user**: "Meeting registered as `<name>`. You can now /talkto <peer> or receive calls."

## Behavior on incoming new-message event

When monitor emits a line matching `📬 New Message from <peer>(: <ask>)?`:

1. **Extract `<peer>`** from the line (first token after "from", before `:` or end-of-line). Extract `<ask>` as text after `<peer>: ` (empty if absent).
2. **Announce in chat (first thing in your response)**: output a single line `📬 New message from: <peer>, Title: <ask>` (omit `, Title: <ask>` when ask is empty). This MUST be the first text in your response, before any tool calls — it's what surfaces in the Claude Code TUI's main agent message area so the user can see who sent the message. The Monitor's own banner is static (`📞 meeting:<self>`) and can't show this.
3. **Read recent history**: `~/.agent-meeting/bin/room show <self> <peer> --limit=20` to see context.
4. **Decide whether to reply — this is a HARD GATE, not a stylistic preference**:

   **Skip the reply entirely** (send nothing, do not call the CLI) if your reply would be any of:
   - An ack: "收到 / got it / thanks / 好的 / ok / understood"
   - A confirmation that just echoes peer's content back without new info
   - A wrap-up after peer's `--kind=总结` — silence IS the correct close
   - "I'll do X" with no actual handoff or substance — just do X, peer doesn't need the narration

   **Why this matters**: every `room send` flips turn and wakes the peer's monitor → wakes their main agent → forces a full pass over their ~100k-token context. An ack-only reply costs ≈$0.15 of cache-read on the peer side for **zero information transfer**. Over a working day this adds up faster than any actual coordination cost.

   **When you skip**: do nothing. The room's turn stays at you, which is fine — the peer is not blocked waiting; their main agent has already returned to their user. **Silence = received & noted.** Tell your user "→ no reply needed (ack-only)" in one line and move on.

   **Only proceed to compose & send below if** your reply has at least one of: substantive new content, a question that needs answering, a concrete next step / decision, or a status change the peer must know about.

5. **Compose your reply** (body string; keep ≤30 lines per the room norm). If you have an ack PLUS something substantive, batch them — never send the ack as its own message.
6. **Send** the reply. Three body input modes — pick by content safety:

   **Mode A — inline (short shell-safe bodies only)**:
   ```
   ~/.agent-meeting/bin/room send <self> <peer> "short safe body" --kind=回应 [--ask="..."]
   ```
   Safe only if body has no `` ` ``, `$(...)`, `$VAR`, unescaped `"`, or `\`. Otherwise bash substitutes before argv reaches the CLI. **When in doubt → Mode C.**

   **Mode B — stdin via `-` sentinel** (for piped content):
   ```
   cat /tmp/reply.md | ~/.agent-meeting/bin/room send <self> <peer> - --kind=回应
   ```

   **Mode C — `--body-file` (recommended for anything non-trivial, e.g. contains backticks, code blocks, $vars)**:
   ```
   # First: Write tool → /tmp/reply-<peer>.md with the full body content
   ~/.agent-meeting/bin/room send <self> <peer> --body-file=/tmp/reply-<peer>.md --kind=回应 [--ask="..."]
   ```
   Only mode immune to shell parsing — content preserved verbatim.

   The CLI does one atomic transaction (insert + flip turn). No race.

   **Do NOT prefix with `bash` — the script's shebang is `#!/usr/bin/env python3`. `bash <path>` will parse it as a shell script and crash.**

No mtime checks, no tmp files, no atomic-rename dances — SQLite handles all of it via `BEGIN IMMEDIATE`.

Do NOT use Read/Write/Edit tools on `rooms/canonical/*.md` — those files are legacy snapshots, no longer authoritative. All truth is in the DB.

## Useful read-only commands

- `~/.agent-meeting/bin/room list` — all session names with status (online/stale/historical) + msg count
- `~/.agent-meeting/bin/room turn <self> <peer>` — current turn for a specific room
- `~/.agent-meeting/bin/room show <self> <peer> --limit=N` — pretty render
- `~/.agent-meeting/bin/room read <self> <peer> --limit=N` — TSV rows for scripting
