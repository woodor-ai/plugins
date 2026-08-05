"""Public ``am-update`` command for agent-meeting distributions."""

from __future__ import annotations

import argparse
import subprocess

from agent_meeting.installation import distribution_update


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="am-update",
        description="Update agent-meeting and its installed AI-platform integrations.",
    )
    parser.add_argument(
        "--target",
        action="append",
        choices=distribution_update.ALL_TARGETS,
        help="update this platform only; repeat to update more than one",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="show the active runtime and detected integrations without updating",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    meeting_home = distribution_update.default_meeting_home()
    detected = distribution_update.detect_targets()
    if args.check:
        version = distribution_update.active_runtime_version(meeting_home)
        print(f"active runtime: {version or 'none'}")
        print(f"installed targets: {', '.join(detected) or 'none'}")
        return 0

    targets = tuple(args.target or detected)
    if not targets:
        print("ERROR: no installed Claude Code or Codex integration was found")
        return 1
    try:
        print("Downloading the current agent-meeting installer...", flush=True)
        distribution_update.install_latest(
            meeting_home=meeting_home,
            targets=targets,
        )
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
