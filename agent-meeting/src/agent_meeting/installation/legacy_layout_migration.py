"""Idempotent migration of supported pre-0.15 agent-meeting artifacts."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from agent_meeting.ai_platforms.codex import user_configuration


LEGACY_CODEX_SESSION_START_MARKERS = (
    "codex-register.py",
    "codex-handoff-pickup.py",
    "handoff-pickup.py",
)
LEGACY_CODEX_POST_TOOL_USE_MARKERS = ("auto-handoff.py",)
LEGACY_LAUNCHD_SERVICES = (
    "com.tommy.agent-meeting",
    "com.tommy.agent-meeting.amctl",
)
LEGACY_WINDOWS_TASKS = (
    "agent-meeting-daemon",
    "agent-meeting-amctl",
)
LEGACY_RUNTIME_FILES = (
    "bin/amctl",
    "bin/amctl.cmd",
    "amctl.stopped",
)
LEGACY_WINDOWS_STARTUP_FILES = (
    "agent-meeting-daemon.cmd",
    "agent-meeting-amctl.cmd",
)


def remove_legacy_codex_hooks(config_path: Path) -> bool:
    session_start_removed = user_configuration.remove_owned_hooks(
        config_path,
        event_table="SessionStart",
        command_markers=LEGACY_CODEX_SESSION_START_MARKERS,
    )
    post_tool_use_removed = user_configuration.remove_owned_hooks(
        config_path,
        event_table="PostToolUse",
        command_markers=LEGACY_CODEX_POST_TOOL_USE_MARKERS,
    )
    return session_start_removed or post_tool_use_removed


def _unlink_exact(path: Path, removed: list[str]) -> None:
    try:
        path.unlink()
        removed.append(str(path))
    except FileNotFoundError:
        pass


def migrate_legacy_layout(
    *,
    meeting_home: Path,
    codex_home: Path,
    user_home: Path,
    platform_name: str,
    temp_dir: Path | None = None,
    run_command=subprocess.run,
) -> list[str]:
    """Remove exact legacy artifacts and return the paths/actions removed."""
    removed: list[str] = []
    temp_dir = temp_dir or Path(tempfile.gettempdir())

    for relative_path in LEGACY_RUNTIME_FILES:
        _unlink_exact(meeting_home / relative_path, removed)
    _unlink_exact(temp_dir / "amctl.pid", removed)

    codex_config = codex_home / "config.toml"
    if remove_legacy_codex_hooks(codex_config):
        removed.append(f"legacy Codex hooks in {codex_config}")

    if platform_name == "darwin":
        launch_agents = user_home / "Library" / "LaunchAgents"
        uid = os.getuid()
        for label in LEGACY_LAUNCHD_SERVICES:
            run_command(
                ["launchctl", "bootout", f"gui/{uid}/{label}"],
                capture_output=True,
            )
            _unlink_exact(launch_agents / f"{label}.plist", removed)
    elif platform_name.startswith("win"):
        startup_dir = (
            user_home
            / "AppData"
            / "Roaming"
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
            / "Startup"
        )
        for task_name in LEGACY_WINDOWS_TASKS:
            run_command(
                ["schtasks", "/Delete", "/TN", task_name, "/F"],
                capture_output=True,
            )
        for filename in LEGACY_WINDOWS_STARTUP_FILES:
            _unlink_exact(startup_dir / filename, removed)

    return removed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrate-agent-meeting-legacy-layout.py"
    )
    parser.add_argument("--meeting-home", type=Path)
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument("--user-home", type=Path)
    parser.add_argument(
        "--codex-hook-only",
        action="store_true",
        help="only remove the obsolete per-session Codex hook",
    )
    return parser


def main(argv=None) -> int:
    arguments = build_parser().parse_args(argv)
    user_home = arguments.user_home or Path.home()
    meeting_home = arguments.meeting_home or Path(
        os.environ.get("MEETING_HOME") or (user_home / ".agent-meeting")
    )
    codex_home = arguments.codex_home or Path(
        os.environ.get("CODEX_HOME") or (user_home / ".codex")
    )
    codex_config = codex_home / "config.toml"

    if arguments.codex_hook_only:
        changed = remove_legacy_codex_hooks(codex_config)
        if changed:
            print(
                "Removed legacy agent-meeting SessionStart hook from "
                f"{codex_config}"
            )
        else:
            print(
                "No legacy agent-meeting SessionStart hook found in "
                f"{codex_config}"
            )
        return 0

    removed = migrate_legacy_layout(
        meeting_home=meeting_home,
        codex_home=codex_home,
        user_home=user_home,
        platform_name=sys.platform,
    )
    if removed:
        print("Migrated legacy agent-meeting layout:")
        for item in removed:
            print(f"- {item}")
    else:
        print("No supported legacy agent-meeting artifacts found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
