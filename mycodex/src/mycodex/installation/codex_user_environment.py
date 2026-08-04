"""Apply Codex user configuration during agent-meeting installation."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

from agent_meeting.clients import hub_discovery
from agent_meeting.clients.am_process_client import run_am_cli
from agent_meeting.ai_platforms.codex import user_configuration

from mycodex.ai_platforms.codex import agent_meeting_instructions


def _am_command(meeting_home: Path, *, is_windows: bool) -> Path:
    return meeting_home / "bin" / (
        "am.exe" if is_windows else "am"
    )


def _discover_control(am_command: Path) -> str:
    control = hub_discovery.discover_control(
        lambda *args: run_am_cli(am_command, *args, timeout=10)
    )
    return str(control.get("base_url") or "")


def _pin_control(am_command: Path, control_url: str) -> None:
    result = run_am_cli(
        am_command,
        "host",
        control_url,
        timeout=10,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            detail or f"failed to save am-msgd host: {control_url}"
        )


def configure_codex_user_environment(
    *,
    meeting_home: Path,
    codex_home: Path,
    explicit_control: str = "",
    enable_full_automation: bool = False,
    is_windows: bool | None = None,
    prompt: Callable[[str, str], str] | None = None,
) -> dict:
    is_windows = (
        sys.platform.startswith("win")
        if is_windows is None
        else is_windows
    )
    am_command = _am_command(
        meeting_home,
        is_windows=is_windows,
    )
    control_url = explicit_control.strip()
    if control_url:
        _pin_control(am_command, control_url)
    else:
        control_url = _discover_control(am_command)
        if not control_url and prompt is not None:
            control_url = prompt(
                "am-msgd URL (http://x.x.x.x:8765)",
                "",
            )
            if control_url:
                _pin_control(am_command, control_url)

    first_install = (
        agent_meeting_instructions.install_agent_meeting_instructions(
            codex_home=codex_home,
            am_command=am_command,
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

    if enable_full_automation:
        user_configuration.enable_full_automation(codex_home)

    return {
        "control_url": control_url,
        "first_install": first_install,
    }
