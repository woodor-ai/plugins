#!/bin/bash
# handoff plugin: SessionStart auto-pickup.
# If <project>/.claude/handoff-pending.md exists, emit its content as
# additionalContext for the new session and atomically archive it under
# <project>/docs/handoff/archive/handoff-<timestamp>.md.
#
# Race-resistant: hooks.json registers 4 matchers (startup/resume/clear/compact).
# Multiple may fire concurrently for the same SessionStart event. The atomic
# `mv` is the claim — winner emits context, losers exit 0 silently. No `set -e`
# so a failed mv doesn't surface as a hook error to the user.

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"
HANDOFF="$PROJECT_DIR/.claude/handoff-pending.md"

[ -f "$HANDOFF" ] || exit 0

ARCHIVE_DIR="$PROJECT_DIR/docs/handoff/archive"
mkdir -p "$ARCHIVE_DIR" || exit 0
TS=$(date +%Y-%m-%d-%H%M%S)
ARCHIVE_PATH="$ARCHIVE_DIR/handoff-$TS.md"

# Read content BEFORE mv so even on race we have what we need.
CONTENT=$(cat "$HANDOFF" 2>/dev/null) || exit 0
[ -z "$CONTENT" ] && exit 0

# Atomic claim. If a parallel hook already moved the file, mv fails — exit 0
# silently so we don't double-emit and don't surface a spurious error.
mv "$HANDOFF" "$ARCHIVE_PATH" 2>/dev/null || exit 0

CTX=$(printf "## 上 session 交接（auto-loaded，已归档 → %s）\n\n%s" "$ARCHIVE_PATH" "$CONTENT")

jq -n --arg ctx "$CTX" '{
  hookSpecificOutput: {
    hookEventName: "SessionStart",
    additionalContext: $ctx
  }
}'
