---
name: meeting
description: Register this session in the meeting-room directory with a chosen name, and install the monitor (start watching for incoming calls). Required before /talkto can be used to or from this session.
---

When user runs `/meeting <name>`:

1. **Validate name**: alphanumeric + hyphen only, no `--` substring, length 2-20
   - If invalid: refuse and tell user the rules
2. **Check directory**: read `~/.claude/plugins/data/agent-meeting/directory.json`. If `<name>` already exists with a different `pid` than this session, refuse (name taken).
3. **Register**: write the directory entry atomically via `jq → tmp → mv`. `mv` within the same filesystem is atomic at the inode level, so even if another session races, only the last writer wins for the entire file (no torn JSON). Do NOT split into Read + Write tool calls — that races on the read side. Use this single Bash command:

   ```bash
   jq --arg name "<name>" --argjson entry '{"pid": <pid>, "cwd": "<cwd>", "started_at": "<now>"}' \
     '.[$name] = $entry' \
     ~/.claude/plugins/data/agent-meeting/directory.json > /tmp/dir.json.tmp \
     && mv /tmp/dir.json.tmp ~/.claude/plugins/data/agent-meeting/directory.json
   ```

   Note: this is not a hard mutex — if two sessions register simultaneously, one entry can be lost (read-modify-write race between the two `jq` reads). The window is small (milliseconds) and the use case is 1–5 long-lived sessions, so this is accepted. `flock` is intentionally not used because macOS does not ship it.
4. **Create view dir**: `mkdir -p ~/.claude/plugins/data/agent-meeting/rooms/<name>`
5. **Install monitor**: invoke the Monitor tool with these exact arguments:

   - `description`: `📞 meeting:<name>` — static label shown as the notification title; the dynamic per-event content goes in the stdout line below.
   - `persistent`: `true`
   - `command`: see zsh script below.

   Notes on why each piece matters:
   - `setopt NULL_GLOB` — without it, zsh's default NOMATCH makes empty `*.md` globs print `no matches found` to stderr that even `2>/dev/null` on the command can't suppress (zsh emits the error before the redirection takes effect).
   - `stat -L` — follows symlinks so a write through the canonical path bumps mtime as seen via the view-symlink.
   - **Per-room mtime tracking** — using a single global watermark would cause every room whose `当前发言权` already points at `<name>` to spuriously fire any time *any other* room's mtime advances. Track each room separately and only fire when that specific room's mtime increased.
   - **Event line format** — emits `📬 New Message from <peer>: <ask-body>` (or the bare form `📬 New Message from <peer>` when the latest message has no `**Ask**:` line). This is the user-visible notification body; the agent extracts `<peer>` from it and computes the canonical path itself.
   - **STATE_FILE pre-fill on fresh start** — when the state file is empty (first /meeting after a state-file delete or initial registration), seed it with the current mtimes of all existing rooms. Without this, the first scan iteration sees every existing room as "changed from 0" and emits a ghost notification per room whose turn already points at self.

```zsh
zsh -c '
setopt NULL_GLOB
SELF="<name>"
DIR="$HOME/.claude/plugins/data/agent-meeting/rooms/<name>"
STATE_FILE="/tmp/meeting-<name>.mtimes"
[ -f "$STATE_FILE" ] || : > "$STATE_FILE"
if [ ! -s "$STATE_FILE" ]; then
  for room in $DIR/*.md; do
    [ -e "$room" ] || continue
    peer=$(basename "${room%.md}")
    cur=$(stat -L -f %m "$room" 2>/dev/null)
    cur="${cur:-0}"
    echo "${peer}=${cur}" >> "$STATE_FILE"
  done
fi
echo "[meeting <name>] monitor started"
while true; do
  for room in $DIR/*.md; do
    [ -e "$room" ] || continue
    peer=$(basename "${room%.md}")
    cur=$(stat -L -f %m "$room" 2>/dev/null)
    cur="${cur:-0}"
    last=$(grep -F "${peer}=" "$STATE_FILE" 2>/dev/null | tail -1 | cut -d= -f2)
    last="${last:-0}"
    if [ "$cur" != "$last" ]; then
      if [ -L "$room" ]; then
        target=$(readlink "$room")
        case "$target" in /*) canonical="$target" ;; *) canonical="$DIR/$target" ;; esac
      else
        canonical="$room"
      fi
      if grep -qF "当前发言权: <name>" "$canonical" 2>/dev/null; then
        ask=$(grep -F "**Ask**:" "$canonical" 2>/dev/null | tail -1 | sed -e "s/^[[:space:]]*\*\*Ask\*\*:[[:space:]]*//")
        if [ -n "$ask" ]; then
          echo "📬 New Message from ${peer}: ${ask}"
        else
          echo "📬 New Message from ${peer}"
        fi
      fi
      grep -vF "${peer}=" "$STATE_FILE" > "${STATE_FILE}.tmp" 2>/dev/null || : > "${STATE_FILE}.tmp"
      echo "${peer}=${cur}" >> "${STATE_FILE}.tmp"
      mv "${STATE_FILE}.tmp" "$STATE_FILE"
    fi
  done
  sleep 3
done
'
```

6. **Update terminal tab title (best-effort)**: send OSC 0 escape sequence directly to TTY (bypasses Claude's TUI) so iTerm2 shows the meeting name on the tab. `/dev/tty` is not always writable (non-interactive harnesses, piped sessions), so silently skip on failure — DO NOT report the failure to the user, it is harmless cosmetic.

   ```bash
   { printf '\033]0;%s\a' "<name>" > /dev/tty; } 2>/dev/null || true
   ```

7. **Confirm to user**: "Meeting registered as `<name>`. You can now /talkto <peer> or receive calls." (Do not mention tab rename — it is best-effort and silent.)

## Behavior on incoming new-message event

When monitor emits a line matching `📬 New Message from <peer>(: <ask>)?`:

1. **Extract `<peer>`** from the line (first token after "from", before `:` or end-of-line).
2. **Compute canonical path** (monitor no longer includes it in the event line):
   - `sorted = sort([self, peer])` (lexicographic)
   - `canonical = ~/.claude/plugins/data/agent-meeting/rooms/canonical/${sorted[0]}--${sorted[1]}.md`
3. **Read** the canonical path. The room file header contains the message protocol; follow it to compose your reply.
4. **Write** the entire updated file back using the canonical path. Do NOT write through the view symlink at `rooms/<self>/<peer>.md` — the Write tool will refuse with "Refusing to write through symlink".
5. After writing, the room's `当前发言权` line should read the peer's name (you flip it as part of the message).
