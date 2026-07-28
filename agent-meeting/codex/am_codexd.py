#!/usr/bin/env python3
"""Compatibility module for the independent mycodex broker package."""

import sys
from pathlib import Path


_MYCODEX_SOURCE = (
    Path(__file__).resolve().parents[2] / "mycodex" / "src"
)
if not _MYCODEX_SOURCE.is_dir():
    raise RuntimeError("mycodex package source not found; rerun the installer")
sys.path.insert(0, str(_MYCODEX_SOURCE))

_IMPLEMENTATION = (
    _MYCODEX_SOURCE
    / "mycodex"
    / "codex_session_broker"
    / "broker_process.py"
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
