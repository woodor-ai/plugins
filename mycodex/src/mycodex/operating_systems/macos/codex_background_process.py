"""macOS process-launch policy for Codex broker components."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def legacy_runtime_python(meeting_home: Path) -> str:
    candidate = meeting_home / "venv" / "bin" / "python"
    return str(candidate if candidate.exists() else Path(sys.executable))


def detached_popen_options(log_file) -> dict:
    return {
        "stdin": subprocess.DEVNULL,
        "stdout": log_file,
        "stderr": subprocess.STDOUT,
        "start_new_session": True,
    }
