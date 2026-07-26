#!/usr/bin/env python3
"""Run the shared pickup script with a storage directory for the active host."""

import os
import runpy
from pathlib import Path

if os.environ.get("PLUGIN_ROOT"):
    os.environ["HANDOFF_DIR"] = ".codex"

runpy.run_path(str(Path(__file__).resolve().parent / "handoff-pickup.py"), run_name="__main__")
