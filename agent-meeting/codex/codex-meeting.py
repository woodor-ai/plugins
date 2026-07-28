#!/usr/bin/env python3
"""Compatibility entrypoint for the independent mycodex launcher package."""

import sys
from pathlib import Path


_MYCODEX_SOURCE = (
    Path(__file__).resolve().parents[2] / "mycodex" / "src"
)
_AGENT_MEETING_SOURCE = Path(__file__).resolve().parents[1] / "src"
if not _MYCODEX_SOURCE.is_dir():
    raise RuntimeError("mycodex package source not found; rerun the installer")
sys.path.insert(0, str(_MYCODEX_SOURCE))
if _AGENT_MEETING_SOURCE.is_dir():
    sys.path.insert(0, str(_AGENT_MEETING_SOURCE))

_IMPLEMENTATION = (
    _MYCODEX_SOURCE / "mycodex" / "launcher" / "codex_tui_session.py"
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
