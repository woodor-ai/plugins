#!/usr/bin/env python3
"""Activate an already-installed immutable host-runtime version."""

import argparse
import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "agent-meeting" / "src"))

from agent_meeting.installation.version_activation import activate_runtime


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("version")
    parser.add_argument(
        "--meeting-home",
        type=Path,
        default=Path.home() / ".agent-meeting",
    )
    args = parser.parse_args(argv)
    payload = activate_runtime(
        meeting_home=args.meeting_home,
        version=args.version,
    )
    print(
        f"activated agent-meeting runtime {payload['version']} "
        f"at {payload['runtime']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
