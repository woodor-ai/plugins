#!/usr/bin/env python3
"""Compatibility loader for the packaged Claude Code SessionStart adapter."""

from __future__ import annotations

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
            activated_source = Path(lines[0]).parent / "src"
            if activated_source.is_dir():
                return activated_source
    raise RuntimeError(
        "agent-meeting package source not found; "
        "rerun the agent-meeting installer"
    )


_SOURCE_ROOT = _resolve_source_root()
sys.path.insert(0, str(_SOURCE_ROOT))
_IMPLEMENTATION = (
    _SOURCE_ROOT
    / "agent_meeting"
    / "ai_platforms"
    / "claude_code"
    / "session_start_bootstrap.py"
)
exec(
    compile(
        _IMPLEMENTATION.read_text(encoding="utf-8"),
        str(_IMPLEMENTATION),
        "exec",
    ),
    globals(),
)
