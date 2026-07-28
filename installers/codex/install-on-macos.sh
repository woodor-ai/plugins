#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)

python3 "$REPO_ROOT/installers/shared/install-agent-meeting-package.py"
python3 "$REPO_ROOT/installers/shared/migrate-agent-meeting-legacy-layout.py"
MEETING_HOME_DIR=${MEETING_HOME:-"$HOME/.agent-meeting"}
"$MEETING_HOME_DIR/bin/am-configure-codex-user-environment" "$@"
python3 "$REPO_ROOT/installers/shared/register-codex-marketplace.py"
