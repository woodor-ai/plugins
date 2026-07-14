#!/bin/sh
# mycodex: bridge a codex session into agent-meeting, or update woodor-ai/plugins.
#
#   mycodex --update                     pull (or clone) woodor-ai/plugins and
#                                         rerun the interactive installer; any
#                                         extra args are forwarded to it.
#   mycodex [<name>] [--port N] [--control-url URL] [--proj X] [--no-codex]
#                                         start (or resume) a bridged codex
#                                         session — needs agent-meeting installed
#                                         (run `mycodex --update` first).
#
# Single source of truth, copied verbatim (no per-install templating) into
# ~/.agent-meeting/bin/mycodex by both install-codex.py (root installer,
# unconditional — makes `--update` work even before agent-meeting is installed)
# and session-bootstrap.py (agent-meeting's own SessionStart hook — self-heals
# this file if bin/ is ever wiped and rebuilt). Fully self-locating: no absolute
# path is baked in, so the file is byte-identical everywhere it is copied.
set -e

CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
BIN_DIR="$(cd "$(dirname "$0")" && pwd)"
MEETING_HOME="${MEETING_HOME:-$(dirname "$BIN_DIR")}"
AM_CODEX_MEETING="$CODEX_HOME/plugins/agent-meeting/codex/codex-meeting.py"
VPY="$MEETING_HOME/venv/bin/python"

if [ "$1" = "--update" ]; then
    shift
    REPO_URL="https://github.com/woodor-ai/plugins"
    DEST="$CODEX_HOME/plugins-src"

    if ! command -v git >/dev/null 2>&1; then
        echo "ERROR: git not found. Install git and re-run." >&2
        exit 1
    fi
    PY=""
    for c in python3 python; do
        if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
    done
    if [ -z "$PY" ]; then
        echo "ERROR: python3 not found. Install Python 3.9+ and re-run." >&2
        exit 1
    fi

    if [ -d "$DEST/.git" ]; then
        echo "Updating $DEST ..."
        git -C "$DEST" pull --ff-only
    else
        echo "Cloning $REPO_URL to $DEST ..."
        git clone "$REPO_URL" "$DEST"
    fi

    echo ""
    echo "Running interactive installer ..."
    exec "$PY" "$DEST/install-codex.py" "$@"
fi

if [ ! -x "$VPY" ] || [ ! -f "$AM_CODEX_MEETING" ]; then
    echo "mycodex: agent-meeting is not installed — run 'mycodex --update' to install it, then retry." >&2
    exit 1
fi

exec "$VPY" "$AM_CODEX_MEETING" "$@"
