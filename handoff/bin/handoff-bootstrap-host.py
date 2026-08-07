#!/usr/bin/env python3
"""Install the updater and migrate retired host-specific configuration."""

import os
import runpy
import sys
from pathlib import Path


def main() -> None:
    bin_directory = Path(__file__).resolve().parent
    if os.environ.get("PLUGIN_ROOT"):
        runpy.run_path(
            str(bin_directory / "handoff-codex-migrate.py"),
            run_name="__main__",
        )
    else:
        runpy.run_path(
            str(bin_directory / "handoff-bootstrap.py"),
            run_name="__main__",
        )

    try:
        runpy.run_path(
            str(bin_directory / "handoff-updater-install.py"),
            run_name="__main__",
        )
    except OSError as exc:
        print(f"handoff: unable to install handoff-update: {exc}", file=sys.stderr)

if __name__ == "__main__":
    main()
