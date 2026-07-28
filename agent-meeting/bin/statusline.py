#!/usr/bin/env python3
"""Compatibility entrypoint for the packaged Claude agent status line."""

import sys
from pathlib import Path


def _resolve_source_root() -> Path:
    local_source = Path(__file__).resolve().parent.parent / "src"
    if local_source.is_dir():
        return local_source
    activation = Path(__file__).resolve().parent.parent / ".bin-plugin-root"
    if activation.is_file():
        lines = activation.read_text(encoding="utf-8").splitlines()
        if lines:
            source = Path(lines[0]).parent / "src"
            if source.is_dir():
                return source
    raise RuntimeError("agent-meeting package source not found")


_SOURCE_ROOT = _resolve_source_root()
sys.path.insert(0, str(_SOURCE_ROOT))
_IMPLEMENTATION = (
    _SOURCE_ROOT
    / "agent_meeting"
    / "ai_platforms"
    / "claude_code"
    / "meeting_status_line_process.py"
)
exec(
    compile(
        _IMPLEMENTATION.read_text(encoding="utf-8"),
        str(_IMPLEMENTATION),
        "exec",
    ),
    globals(),
    globals(),
)
