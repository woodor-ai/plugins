---
name: meeting
description: Register this session in the meeting-room directory with a chosen name, and install the monitor (start watching for incoming calls). Required before /talkto can be used to or from this session. Backed by SQLite (~/.agent-meeting/db/rooms.db) — all room state lives there, no more .md file fiddling.
argument-hint: [list | <name>]
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
| `/meeting list` | Run `~/.agent-meeting/bin/room list` and **paste the TSV output verbatim into your reply as a markdown table** with columns Status / Name / Msgs. Do NOT just say "see above" or "如上" relying on the collapsed bash block — the user wants it visible in the main chat area without expanding. Status is `online` / `stale` / `historical`. |
| `/meeting <name>` | Register this session as `<name>` (see "On `/meeting <name>`" below) |

Reserved word `list` cannot be used as a session name — it goes to the `list` subcommand instead.

### Picker (when `/meeting` has no args)

1. Run `~/.agent-meeting/bin/room list` to get session-name candidates. Output is TSV: `<status>\t<name>\t<msgs>` where status is one of:
   - `online` — registered AND monitor pid alive → picking would conflict with the running session (your registration would overwrite directory.json but their monitor keeps running).
   - `stale` — registered but monitor process gone (zombie entry) → safe to take over.
   - `historical` — never in directory but appeared as sender in DB at some point → safe, fully fresh registration.
2. Use the `AskUserQuestion` tool to let user pick. Build options from ALL candidates:
   - Each name becomes an option. Label = `<name>`; description format:
     - online: `(in use — will conflict) — <cwd>`
     - stale: `(stale, safe to take over) — <info>`
     - historical: `(historical, safe) — <info>`
   - "Other" is added automatically by AskUserQuestion for typing a brand-new name.
   - **Do not silently skip online names** — show them so user knows what's taken, with the warning label.
   - Cap at 4 options total (AskUserQuestion limit). Priority: stale → historical → online → Other. If still over, prefer most-recently-active.
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
4. **Compose your reply** (body string; keep ≤30 lines per the room norm).
5. **Send** the reply. Three body input modes — pick by content safety:

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
