"""Render local paths for command strings that a host hands to bash."""

from __future__ import annotations

import shlex
from pathlib import Path


def bash_argument(path: Path | str) -> str:
    """Quote one local path for a command string that bash will parse.

    Claude Code runs hook, status-line, and tool commands through bash on every
    platform. On Windows the MSYS runtime re-parses that command line before
    bash sees it, and it consumes a backslash as an escape unless the spawning
    process wrapped the whole string in quotes of its own -- which it only does
    when the string contains a space. A path written with backslashes therefore
    survives or is destroyed depending on whether it happens to contain a
    space, no matter how the command itself is quoted. POSIX separators remove
    that dependency, and Windows accepts them in every path it opens or runs.
    """
    return shlex.quote(Path(path).as_posix())
