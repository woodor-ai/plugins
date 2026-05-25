---
name: meeting
description: Register this session in the meeting-room directory with a chosen name, and install the monitor (start watching for incoming calls). Required before /talkto can be used to or from this session.
---

When user runs `/meeting <name>`:

1. **Validate name**: alphanumeric + hyphen only, no `--` substring, length 2-20
   - If invalid: refuse and tell user the rules
2. **Check directory**: read `~/.claude/plugins/data/agent-meeting/directory.json`. If `<name>` already exists with a different `pid` than this session, refuse (name taken).
3. **Register** (MUST be done under `flock` to prevent concurrent overwrite by other sessions running /meeting at the same time):

   Use a single Bash command of this exact shape — do NOT split into Read + Write tool calls, which races:

   ```bash
   (
     flock -x 9
     jq --arg name "<name>" --argjson entry '{"pid": <pid>, "cwd": "<cwd>", "started_at": "<now>"}' \
       '.[$name] = $entry' \
       ~/.claude/plugins/data/agent-meeting/directory.json > /tmp/dir.json.tmp \
       && mv /tmp/dir.json.tmp ~/.claude/plugins/data/agent-meeting/directory.json
   ) 9>/tmp/meeting-dir.lock
   ```

   This serializes concurrent registrations from multiple sessions.
4. **Create view dir**: `mkdir -p ~/.claude/plugins/data/agent-meeting/rooms/<name>`
5. **Install monitor**: invoke the Monitor tool with this exact command (substitute `<name>` literally). The `-L` on stat is CRITICAL — without it, symlink mtime never updates and the monitor never fires when peer writes the canonical file:

```zsh
zsh -c '
SELF="<name>"
DIR="$HOME/.claude/plugins/data/agent-meeting/rooms/<name>"
CANON="$HOME/.claude/plugins/data/agent-meeting/rooms/canonical"
LAST_FILE="/tmp/meeting-<name>.mtime"
[ -f "$LAST_FILE" ] || echo "0" > "$LAST_FILE"
echo "[meeting <name>] monitor started"
while true; do
  cur=$(stat -L -f %m $DIR/*.md 2>/dev/null | sort -n | tail -1)
  cur="${cur:-0}"
  last=$(cat "$LAST_FILE")
  if [ "$cur" != "$last" ]; then
    for room in $DIR/*.md; do
      [ -f "$room" ] || continue
      # Resolve symlink → canonical path
      if [ -L "$room" ]; then
        target=$(readlink "$room")
        case "$target" in /*) canonical="$target" ;; *) canonical="$DIR/$target" ;; esac
      else
        canonical="$room"
      fi
      if grep -qF "当前发言权: <name>" "$canonical" 2>/dev/null; then
        echo "RING peer=$(basename ${room%.md}) canonical=$canonical"
      fi
    done
    echo "$cur" > "$LAST_FILE"
  fi
  sleep 3
done
'
```

6. **Update terminal tab title**: send OSC 0 escape sequence directly to TTY (bypasses Claude's TUI), so the iTerm2 tab visibly shows the meeting name:

   ```bash
   printf '\033]0;%s\a' "<name>" > /dev/tty
   ```

7. **Confirm to user**: "Meeting registered as `<name>`. Tab renamed. You can now /talkto <peer> or receive calls."

## Behavior on incoming RING

When monitor emits `RING peer=<peer> canonical=<absolute-path>`:

1. Use the `canonical=<path>` value from the monitor output, OR compute it yourself:
   - `sorted = sort([self, peer])` (lexicographic)
   - `canonical = ~/.claude/plugins/data/agent-meeting/rooms/canonical/${sorted[0]}--${sorted[1]}.md`
2. **Read** the canonical path. The room file header contains the message protocol; follow it to compose your reply.
3. **Write** the entire updated file back using the canonical path. Do NOT write through the view symlink at `rooms/<self>/<peer>.md` — the Write tool will refuse with "Refusing to write through symlink".
4. After writing, the room's `当前发言权` line should read the peer's name (you flip it as part of the message).
