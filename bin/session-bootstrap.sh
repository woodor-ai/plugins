#!/bin/bash
# SessionStart hook for the agent-meeting plugin.
# - Ensures runtime data dirs exist
# - Copies room-header template from plugin (read-only) to data dir on first run
# - Emits JSON with hookSpecificOutput.additionalContext to be injected
set -e

DATA_DIR="$HOME/.claude/plugins/data/agent-meeting"
DIRECTORY="$DATA_DIR/directory.json"
TEMPLATE_DST="$DATA_DIR/templates/room-header.md"

mkdir -p \
  "$DATA_DIR" \
  "$DATA_DIR/rooms/canonical" \
  "$DATA_DIR/rooms/archive" \
  "$DATA_DIR/templates"

[ -f "$DIRECTORY" ] || echo '{}' > "$DIRECTORY"

# Copy template from plugin source if available; never overwrite a user-customized copy.
if [ -n "$CLAUDE_PLUGIN_ROOT" ] && [ -f "$CLAUDE_PLUGIN_ROOT/templates/room-header.md" ]; then
  cp -n "$CLAUDE_PLUGIN_ROOT/templates/room-header.md" "$TEMPLATE_DST" 2>/dev/null || true
fi

PEERS=$(jq -r 'keys | join(", ")' "$DIRECTORY" 2>/dev/null)
[ -z "$PEERS" ] && PEERS="(none online)"

CONTEXT=$(cat <<EOF
📞 Meeting-room system is active.

This session has NO meeting name yet — you cannot make or receive calls until registered.

**MANDATORY first action**: if the user's first prompt is NOT \`/meeting <name>\`, do NOT proceed with their task. Instead reply:

> 📞 Please name this session first via \`/meeting <a-short-name>\` (lowercase, alphanumeric + hyphen, 2–20 chars). After naming, your phone will be active and I'll continue with your request.

Only after the user runs \`/meeting <name>\` may you proceed with normal tasks.

Data dir: ~/.claude/plugins/data/agent-meeting/
Online peers: $PEERS
EOF
)

# Emit JSON for SessionStart hook
jq -n --arg ctx "$CONTEXT" '{
  hookSpecificOutput: {
    hookEventName: "SessionStart",
    additionalContext: $ctx
  }
}'
