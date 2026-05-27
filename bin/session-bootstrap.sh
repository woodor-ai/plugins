#!/bin/bash
# SessionStart hook for the agent-meeting plugin.
# - Ensures the data dir + SQLite db exist
# - Symlinks plugin bin/ into the data dir so SKILL.md can reference a stable path
# - Emits JSON with hookSpecificOutput.additionalContext to be injected
set -e

DATA_DIR="$HOME/.claude/meeting"
DIRECTORY="$DATA_DIR/directory.json"
DB="$DATA_DIR/db/rooms.db"
BIN_LINK="$DATA_DIR/bin"

mkdir -p "$DATA_DIR" "$DATA_DIR/db"

[ -f "$DIRECTORY" ] || echo '{}' > "$DIRECTORY"

# Symlink plugin's bin/ into data dir → SKILL.md can hardcode ~/.claude/meeting/bin/room
# regardless of where the plugin source lives. Update on every startup in case plugin
# was reinstalled/moved.
if [ -n "$CLAUDE_PLUGIN_ROOT" ] && [ -d "$CLAUDE_PLUGIN_ROOT/bin" ]; then
  ln -sfn "$CLAUDE_PLUGIN_ROOT/bin" "$BIN_LINK"
fi

# Initialize SQLite schema (idempotent) so the very first /meeting call works.
if [ -x "$BIN_LINK/room" ]; then
  "$BIN_LINK/room" init >/dev/null 2>&1 || true
fi

# Online peers = directory entries whose monitor pid file shows a live process.
PEERS=""
if [ -x "$BIN_LINK/room" ]; then
  # candidates output: <status>\t<name>\t<info>. Pull just online.
  PEERS=$("$BIN_LINK/room" candidates 2>/dev/null | awk -F'\t' '$1=="online" {print $2}' | paste -sd, - | sed 's/,/, /g')
fi
[ -z "$PEERS" ] && PEERS="(none online)"

CONTEXT=$(cat <<EOF
📞 Meeting-room system is active.

This session has NO meeting name yet — you cannot make or receive calls until registered.

**MANDATORY first action**: if the user's first prompt is NOT \`/meeting <name>\`, do NOT proceed with their task. Instead reply:

> 📞 Please name this session first via \`/meeting <a-short-name>\` (lowercase, alphanumeric + hyphen, 2–20 chars). After naming, your phone will be active and I'll continue with your request.

Only after the user runs \`/meeting <name>\` may you proceed with normal tasks.

Backend: SQLite at ~/.claude/meeting/db/rooms.db (CLI: ~/.claude/meeting/bin/room).
Online peers: $PEERS
EOF
)

jq -n --arg ctx "$CONTEXT" '{
  hookSpecificOutput: {
    hookEventName: "SessionStart",
    additionalContext: $ctx
  }
}'
