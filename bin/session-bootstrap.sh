#!/bin/bash
# SessionStart hook for the agent-meeting plugin.
# Agent-neutral: works under both Claude Code and Codex CLI.
# - Both agents provide CLAUDE_PLUGIN_ROOT (Codex provides it as a compatibility alias)
# - Data lives at ~/.agent-meeting/ regardless of host agent
set -e

DATA_DIR="$HOME/.agent-meeting"
DIRECTORY="$DATA_DIR/directory.json"
DB="$DATA_DIR/db/rooms.db"
BIN_LINK="$DATA_DIR/bin"

# Detect plugin install root. CLAUDE_PLUGIN_ROOT works on Claude Code natively
# and on Codex via compatibility alias. PLUGIN_ROOT is Codex's native name.
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$PLUGIN_ROOT}"

mkdir -p "$DATA_DIR" "$DATA_DIR/db"

[ -f "$DIRECTORY" ] || echo '{}' > "$DIRECTORY"

# Symlink plugin's bin/ into the data dir → SKILL.md can hardcode
# ~/.agent-meeting/bin/room regardless of where the plugin source lives.
# Refresh every startup in case plugin was reinstalled/moved.
if [ -n "$PLUGIN_ROOT" ] && [ -d "$PLUGIN_ROOT/bin" ]; then
  ln -sfn "$PLUGIN_ROOT/bin" "$BIN_LINK"
fi

# Initialize SQLite schema (idempotent) so the very first /meeting call works.
if [ -x "$BIN_LINK/room" ]; then
  "$BIN_LINK/room" init >/dev/null 2>&1 || true
fi

# Online peers = directory entries whose monitor pid file shows a live process.
PEERS=""
if [ -x "$BIN_LINK/room" ]; then
  PEERS=$("$BIN_LINK/room" candidates 2>/dev/null | awk -F'\t' '$1=="online" {print $2}' | paste -sd, - | sed 's/,/, /g')
fi
[ -z "$PEERS" ] && PEERS="(none online)"

CONTEXT=$(cat <<EOF
📞 Meeting-room system is active.

This session has NO meeting name yet — you cannot make or receive calls until registered.

**MANDATORY first action**: if the user's first prompt is NOT \`/meeting <name>\`, do NOT proceed with their task. Instead reply:

> 📞 Please name this session first via \`/meeting <a-short-name>\` (lowercase, alphanumeric + hyphen, 2–20 chars). After naming, your phone will be active and I'll continue with your request.

Only after the user runs \`/meeting <name>\` may you proceed with normal tasks.

Backend: SQLite at ~/.agent-meeting/db/rooms.db (CLI: ~/.agent-meeting/bin/room).
Online peers: $PEERS
EOF
)

# Claude Code SessionStart hook output format. Codex SessionStart consumes the
# same shape (hookSpecificOutput.additionalContext is honored for context injection).
jq -n --arg ctx "$CONTEXT" '{
  hookSpecificOutput: {
    hookEventName: "SessionStart",
    additionalContext: $ctx
  }
}'
