#!/usr/bin/env python3
"""Run the installed Claude SessionStart hook when the runtime exists."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def session_start_command() -> Path:
    meeting_home = Path(
        os.environ.get("MEETING_HOME")
        or (Path.home() / ".agent-meeting")
    )
    suffix = ".exe" if sys.platform.startswith("win") else ""
    return meeting_home / "bin" / f"am-claude-session-start{suffix}"


def main() -> int:
    command = session_start_command()
    if not command.is_file():
        print(json.dumps({}))
        return 0
    return subprocess.call([str(command)])


if __name__ == "__main__":
    raise SystemExit(main())
