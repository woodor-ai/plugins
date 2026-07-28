"""Internal installer command for Codex-specific user configuration."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from mycodex.ai_platforms.codex import (
    agent_meeting_instructions,
    user_configuration,
)
from mycodex.installation import control_endpoint_selection


def _prompt(message: str, default: str = "") -> str:
    try:
        answer = input(
            f"{message} [{default}]: " if default else f"{message}: "
        ).strip()
    except EOFError:
        return default
    return answer or default


def _am_command(meeting_home: Path, *, is_windows: bool) -> Path:
    return meeting_home / "bin" / (
        "am.exe" if is_windows else "am"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--control-url", default="")
    parser.add_argument(
        "--enable-full-automation",
        action="store_true",
    )
    args = parser.parse_args(argv)
    is_windows = sys.platform.startswith("win")
    meeting_home = Path(
        os.environ.get("MEETING_HOME")
        or (Path.home() / ".agent-meeting")
    )
    codex_home = Path(
        os.environ.get("CODEX_HOME")
        or (Path.home() / ".codex")
    )
    am_command = _am_command(
        meeting_home,
        is_windows=is_windows,
    )
    discovered = control_endpoint_selection.discover_control(
        am_command
    )
    control_url = control_endpoint_selection.select_control(
        meeting_home=meeting_home,
        discovered=discovered,
        explicit=args.control_url.strip(),
        prompt=_prompt,
    )
    control_endpoint_selection.write_launcher_default(
        meeting_home,
        control_url,
    )

    instructions_control = (
        control_url or "http://<control-host>:8765"
    )
    first_install = (
        agent_meeting_instructions.install_agent_meeting_instructions(
            codex_home=codex_home,
            am_command=am_command,
            control_url=instructions_control,
            is_windows=is_windows,
        )
    )
    if is_windows:
        user_configuration.ensure_windows_unelevated_sandbox(
            codex_home
        )
        from mycodex.operating_systems.windows import user_command_path

        user_command_path.ensure_command_directory(meeting_home / "bin")
    else:
        from mycodex.operating_systems.macos import shell_command_path

        shell_command_path.ensure_command_directory(meeting_home / "bin")

    if args.enable_full_automation:
        user_configuration.enable_full_automation(codex_home)

    print("Codex user environment configured")
    print(f"  runtime commands: {meeting_home / 'bin'}")
    print(f"  AGENTS.md: {codex_home / 'AGENTS.md'}")
    print(f"  first install: {'yes' if first_install else 'no'}")
    if control_url:
        print(f"  control URL: {control_url}")
    else:
        print("  control URL: not set")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
