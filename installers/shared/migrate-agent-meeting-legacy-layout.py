#!/usr/bin/env python3
"""Public compatibility migration entrypoint for agent-meeting upgrades."""

import sys
from pathlib import Path


_SOURCE_ROOT = (
    Path(__file__).resolve().parents[2] / "agent-meeting" / "src"
)
sys.path.insert(0, str(_SOURCE_ROOT))

from agent_meeting.installation.legacy_layout_migration import main


if __name__ == "__main__":
    raise SystemExit(main())
