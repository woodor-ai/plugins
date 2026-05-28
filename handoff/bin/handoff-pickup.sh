#!/bin/bash
# handoff plugin: SessionStart auto-pickup.
# If <project>/.claude/handoff-pending.md exists, emit its content as
# additionalContext for the new session and atomically archive it under
# <project>/docs/handoff/archive/handoff-<timestamp>.md.
set -e

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"
HANDOFF="$PROJECT_DIR/.claude/handoff-pending.md"

[ -f "$HANDOFF" ] || exit 0

ARCHIVE_DIR="$PROJECT_DIR/docs/handoff/archive"
mkdir -p "$ARCHIVE_DIR"
TS=$(date +%Y-%m-%d-%H%M%S)
ARCHIVE_PATH="$ARCHIVE_DIR/handoff-$TS.md"

CONTENT=$(cat "$HANDOFF")
mv "$HANDOFF" "$ARCHIVE_PATH"

CTX=$(printf "## 上 session 交接（auto-loaded，已归档 → %s）\n\n%s" "$ARCHIVE_PATH" "$CONTENT")

jq -n --arg ctx "$CTX" '{
  hookSpecificOutput: {
    hookEventName: "SessionStart",
    additionalContext: $ctx
  }
}'
