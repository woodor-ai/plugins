"""Public ``meeting am-msgd`` lifecycle command implementation.

This command layer records the user's host-role choice and delegates persistent
service control to the selected operating-system adapter. It deliberately does
not own launchd or Task Scheduler mechanics.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from agent_meeting.operating_systems.macos import message_hub_launch_agent
from agent_meeting.operating_systems.windows import message_hub_persistence


MACOS_LAUNCH_AGENT_LABEL = "com.tommy.agent-meeting.am-msgd"
WINDOWS_MESSAGE_HUB_TASK_NAME = "agent-meeting-am-msgd"


@dataclass(frozen=True)
class MessageHubLifecyclePaths:
    meeting_home: Path
    config_path: Path
    plugin_root: Path

    @property
    def stable_session_start_command(self) -> Path:
        suffix = ".exe" if sys.platform.startswith("win") else ""
        return (
            self.meeting_home
            / "bin"
            / f"am-claude-session-start{suffix}"
        )

    @property
    def legacy_session_start_script(self) -> Path:
        return self.meeting_home / "bin" / "session-bootstrap.py"

    @property
    def stable_message_hub_command(self) -> Path:
        suffix = ".exe" if sys.platform.startswith("win") else ""
        return self.meeting_home / "bin" / f"am-msgd{suffix}"

    @property
    def source_message_hub_command(self) -> Path:
        return self.plugin_root / "bin" / "am-msgd"


def _write_host_role(paths: MessageHubLifecyclePaths) -> None:
    try:
        config = json.loads(paths.config_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        config = {}
    config["is_host"] = True
    paths.config_path.parent.mkdir(parents=True, exist_ok=True)
    paths.config_path.write_text(
        json.dumps(config, indent=2) + "\n",
        encoding="utf-8",
    )


def _run_session_start_reconciliation(
    paths: MessageHubLifecyclePaths,
) -> bool:
    environment = os.environ.copy()
    environment["CLAUDE_PLUGIN_ROOT"] = str(paths.plugin_root)
    if paths.stable_session_start_command.is_file():
        command = [str(paths.stable_session_start_command)]
    elif paths.legacy_session_start_script.is_file():
        command = [
            sys.executable,
            str(paths.legacy_session_start_script),
        ]
    else:
        return False

    result = subprocess.run(
        command,
        env=environment,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
    return result.returncode == 0


def _launch_session_bound_message_hub(
    paths: MessageHubLifecyclePaths,
    *,
    system_name: str,
    port: int,
) -> None:
    command = paths.stable_message_hub_command
    if not command.is_file():
        command = paths.source_message_hub_command
    if not command.is_file():
        raise SystemExit(f"central am-msgd command not found: {command}")

    log_path = Path(tempfile.gettempdir()) / "am-msgd.log"
    popen_arguments = {
        "stdout": None,
        "stderr": subprocess.STDOUT,
        "close_fds": True,
    }
    if system_name == "Windows":
        popen_arguments["creationflags"] = 0x00000008 | 0x00000200
    else:
        popen_arguments["start_new_session"] = True
    with log_path.open("a", encoding="utf-8") as log_file:
        popen_arguments["stdout"] = log_file
        subprocess.Popen(
            [str(command), "--port", str(port)],
            **popen_arguments,
        )


def start_message_hub(
    paths: MessageHubLifecyclePaths,
    *,
    system_name: str,
    port: int,
) -> None:
    _write_host_role(paths)
    if not _run_session_start_reconciliation(paths):
        _launch_session_bound_message_hub(
            paths,
            system_name=system_name,
            port=port,
        )
    print(
        "central am-msgd started: this machine is now the "
        "session/message hub"
    )


def run_message_hub_lifecycle_command(
    args,
    *,
    paths: MessageHubLifecyclePaths,
    port: int,
    windows_process_is_alive: Callable[[int], bool],
    health_probe: Callable[[], dict | None],
    system_name: str | None = None,
) -> None:
    """Run one public message-hub lifecycle action."""
    system_name = system_name or platform.system()
    if not args.action:
        start_message_hub(
            paths,
            system_name=system_name,
            port=port,
        )
        return

    if system_name == "Windows":
        message_hub_persistence.control_message_hub_persistence(
            args.action,
            paths=message_hub_persistence.MessageHubControlPaths(
                message_hub_stop_sentinel=(
                    paths.meeting_home / "am-msgd.stopped"
                ),
                message_hub_pid_file=(
                    Path(tempfile.gettempdir()) / "am-msgd.pid"
                ),
                supervisor_pid_file=(
                    Path(tempfile.gettempdir())
                    / "meeting-supervisor.pid"
                ),
            ),
            process_is_alive=windows_process_is_alive,
            health_probe=health_probe,
        )
        return

    if system_name != "Darwin":
        print(
            "central am-msgd management is not supported on this platform "
            "(Linux am-msgd is session-bound)",
            file=sys.stderr,
        )
        raise SystemExit(1)

    try:
        config = json.loads(paths.config_path.read_text(encoding="utf-8"))
    except Exception:
        config = {}
    message_hub_launch_agent.control_message_hub_launch_agent(
        args.action,
        label=MACOS_LAUNCH_AGENT_LABEL,
        plist_path=(
            Path.home()
            / "Library"
            / "LaunchAgents"
            / f"{MACOS_LAUNCH_AGENT_LABEL}.plist"
        ),
        port=port,
        host_is_enabled=bool(config.get("is_host")),
        health_probe=health_probe,
    )
