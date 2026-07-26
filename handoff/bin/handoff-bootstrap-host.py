#!/usr/bin/env python3
"""Run Claude-only bootstrap work; native Codex plugins need no global bootstrap."""

import os
import runpy
from pathlib import Path

if not os.environ.get("PLUGIN_ROOT"):
    runpy.run_path(str(Path(__file__).resolve().parent / "handoff-bootstrap.py"), run_name="__main__")
