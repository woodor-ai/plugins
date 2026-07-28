#!/usr/bin/env python3
"""Configure Codex to use an already-activated agent-meeting host runtime."""

from __future__ import annotations

import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "agent-meeting" / "src"))
sys.path.insert(0, str(REPOSITORY_ROOT / "mycodex" / "src"))

from mycodex.commands.configure_codex_user_environment_cli import (
    main as configure_codex_user_environment,
)


def main(argv=None) -> int:
    return configure_codex_user_environment(argv)


if __name__ == "__main__":
    raise SystemExit(main())
