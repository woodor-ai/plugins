#!/usr/bin/env python3
"""Register this repository with Claude Code and install agent-meeting."""

import os
import shutil
import subprocess
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    claude = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if not claude:
        print("ERROR: claude CLI not found", file=sys.stderr)
        return 1
    updated = subprocess.run(
        [claude, "plugin", "marketplace", "update", "woodor"]
    )
    if updated.returncode != 0:
        added = subprocess.run(
            [
                claude,
                "plugin",
                "marketplace",
                "add",
                str(REPOSITORY_ROOT),
            ]
        )
        if added.returncode != 0:
            return added.returncode
    return subprocess.run(
        [claude, "plugin", "install", "agent-meeting@woodor"]
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
