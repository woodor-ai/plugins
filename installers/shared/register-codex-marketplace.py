#!/usr/bin/env python3
"""Register this repository with Codex and install agent-meeting."""

import os
import shutil
import subprocess
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    codex = os.environ.get("CODEX_BIN") or shutil.which("codex")
    if not codex:
        print("ERROR: codex CLI not found", file=sys.stderr)
        return 1
    updated = subprocess.run(
        [codex, "plugin", "marketplace", "upgrade", "woodor"]
    )
    if updated.returncode != 0:
        added = subprocess.run(
            [
                codex,
                "plugin",
                "marketplace",
                "add",
                str(REPOSITORY_ROOT),
            ]
        )
        if added.returncode != 0:
            return added.returncode
    return subprocess.run(
        [codex, "plugin", "add", "agent-meeting@woodor"]
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
