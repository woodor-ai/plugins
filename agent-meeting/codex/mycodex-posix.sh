#!/bin/sh
# mycodex: bridge a Codex session into agent-meeting.
#
#   am-update                             update agent-meeting and installed
#                                         Claude Code/Codex integrations.
#   mycodex [<name>] [--control-url URL] [--proj X] [--global] [--no-codex]
#                                         start a brokered Codex
#                                         session — needs agent-meeting installed
#                                         (run `am-update` first).
#
# Single source of truth, copied verbatim (no per-install templating) into
# ~/.agent-meeting/bin/mycodex by both install-codex.py (root installer,
# unconditional — makes the launcher available after installation)
# and session-bootstrap.py (agent-meeting's own SessionStart hook — self-heals
# this file if bin/ is ever wiped and rebuilt). Fully self-locating: no absolute
# path is baked in, so the file is byte-identical everywhere it is copied.
set -e

CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
BIN_DIR="$(cd "$(dirname "$0")" && pwd)"
MEETING_HOME="${MEETING_HOME:-$(dirname "$BIN_DIR")}"
SOURCE_STAMP="$MEETING_HOME/.bin-plugin-root"
PLUGIN_BIN="$(sed -n '1p' "$SOURCE_STAMP" 2>/dev/null || true)"
PLUGIN_ROOT="$(dirname "$PLUGIN_BIN")"
AM_CODEX_MEETING="$PLUGIN_ROOT/codex/codex-meeting.py"
VPY="$MEETING_HOME/venv/bin/python"

if [ "${1:-}" = "--update" ]; then
    echo "mycodex --update has moved to am-update. Run: am-update" >&2
    exit 2
fi

if [ -z "$PLUGIN_BIN" ] || [ ! -x "$VPY" ] || [ ! -f "$AM_CODEX_MEETING" ]; then
    echo "mycodex: agent-meeting is not installed — run 'am-update' to install it, then retry." >&2
    exit 1
fi

exec "$VPY" "$AM_CODEX_MEETING" "$@"
