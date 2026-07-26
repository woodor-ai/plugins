#!/usr/bin/env python3
"""Codex entry point for handoff pickup.

The shared pickup implementation remains compatible with Claude Code, whose
cards live in .claude/.  Codex cards deliberately live in .codex/ so the two
runtimes do not claim or overwrite one another's pending handoff card.
"""

import os
import runpy
from pathlib import Path

os.environ["HANDOFF_DIR"] = ".codex"
PICKUP = Path(__file__).resolve().parent.parent / "bin" / "handoff-pickup.py"
runpy.run_path(str(PICKUP), run_name="__main__")
