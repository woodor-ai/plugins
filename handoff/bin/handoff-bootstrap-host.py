#!/usr/bin/env python3
"""Install the updater for both hosts and run Claude-only legacy cleanup."""

import os
import runpy
import sys
from pathlib import Path


def main() -> None:
    bin_directory = Path(__file__).resolve().parent
    try:
        runpy.run_path(
            str(bin_directory / "handoff-updater-install.py"),
            run_name="__main__",
        )
    except OSError as exc:
        print(f"handoff: unable to install handoff-update: {exc}", file=sys.stderr)

    if not os.environ.get("PLUGIN_ROOT"):
        runpy.run_path(
            str(bin_directory / "handoff-bootstrap.py"),
            run_name="__main__",
        )


if __name__ == "__main__":
    main()
