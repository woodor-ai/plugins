#!/bin/sh
# Bootstrap: clone or update woodor-ai/plugins then run the interactive installer.
# Usage (one-liner):
#   curl -fsSL https://raw.githubusercontent.com/woodor-ai/plugins/main/install-codex-plugins.sh | bash
#
# After the first install, run `mycodex --update` locally instead of re-pasting
# this one-liner — install-codex.py drops a `mycodex` command (unconditionally)
# whose --update branch does this same clone-or-pull + rerun-installer dance.
set -e

REPO_URL="https://github.com/woodor-ai/plugins"
DEST="$HOME/.codex/plugins-src"

# dependency checks
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

# clone or update
if [ -d "$DEST/.git" ]; then
    echo "Updating $DEST ..."
    git -C "$DEST" pull --ff-only
else
    echo "Cloning $REPO_URL to $DEST ..."
    git clone "$REPO_URL" "$DEST"
fi

echo ""
echo "Running interactive installer ..."
"$PY" "$DEST/install-codex.py" "$@"
