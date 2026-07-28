"""Manage the no-admin Windows lifecycle for the central message hub.

The Windows adapter deliberately owns Startup-folder and Task Scheduler
details. AI-platform hooks only decide whether this host should enable the
message hub and delegate that decision here.
"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import message_hub_launch_artifacts


MESSAGE_HUB_TASK_NAME = "agent-meeting-am-msgd"
PRE_MESSAGE_HUB_TASK_NAME = "agent-meeting-daemon"
LEGACY_AMCTL_TASK_NAME = "agent-meeting-amctl"


@dataclass(frozen=True)
class MessageHubPersistencePaths:
    startup_directory: Path
    startup_command: Path
    pre_message_hub_startup_command: Path
    legacy_amctl_startup_command: Path
    task_action_sentinel: Path
    message_hub_stop_sentinel: Path
    legacy_amctl_stop_sentinel: Path
    message_hub_pid_file: Path
    legacy_amctl_pid_file: Path
    supervisor_pid_file: Path


@dataclass(frozen=True)
class MessageHubControlPaths:
    message_hub_stop_sentinel: Path
    message_hub_pid_file: Path
    supervisor_pid_file: Path


def _unlink_if_present(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def _kill_process_from_pid_file(pid_file: Path) -> None:
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
        subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)],
            capture_output=True,
        )
    except Exception:
        pass
    _unlink_if_present(pid_file)


def ensure_message_hub_persistence(
    *,
    paths: MessageHubPersistencePaths,
    pythonw_executable: Path,
    supervisor_command: Path,
    stop_bootstrap_message_hub: Callable[[], None],
    launch_supervisor: Callable[[Path, Path], None],
    log: Callable[[str], None],
) -> None:
    """Install or refresh the no-admin message-hub persistence layers."""
    standalone_supervisor = supervisor_command.suffix.lower() == ".exe"
    legacy_task_exists = subprocess.run(
        ["schtasks", "/Query", "/TN", LEGACY_AMCTL_TASK_NAME],
        capture_output=True,
    ).returncode == 0
    legacy_amctl_present = (
        legacy_task_exists
        or paths.legacy_amctl_startup_command.exists()
        or paths.legacy_amctl_pid_file.exists()
        or paths.legacy_amctl_stop_sentinel.exists()
    )
    if legacy_amctl_present:
        try:
            paths.legacy_amctl_stop_sentinel.write_text(
                str(int(time.time())),
                encoding="utf-8",
            )
        except Exception:
            pass
        for pid_file in (
            paths.legacy_amctl_pid_file,
            paths.supervisor_pid_file,
        ):
            _kill_process_from_pid_file(pid_file)

    for old_task in (
        PRE_MESSAGE_HUB_TASK_NAME,
        LEGACY_AMCTL_TASK_NAME,
    ):
        subprocess.run(
            ["schtasks", "/Delete", "/TN", old_task, "/F"],
            capture_output=True,
        )
    for old_startup in (
        paths.pre_message_hub_startup_command,
        paths.legacy_amctl_startup_command,
    ):
        _unlink_if_present(old_startup)
    _unlink_if_present(paths.legacy_amctl_stop_sentinel)

    task_action = message_hub_launch_artifacts.supervisor_task_action(
        pythonw_executable,
        supervisor_command,
        standalone=standalone_supervisor,
    )

    _unlink_if_present(paths.message_hub_stop_sentinel)
    stop_bootstrap_message_hub()

    startup_text = message_hub_launch_artifacts.startup_launcher_text(
        pythonw_executable,
        supervisor_command,
        standalone=standalone_supervisor,
    )
    try:
        paths.startup_directory.mkdir(parents=True, exist_ok=True)
        current_text = (
            paths.startup_command.read_text(encoding="utf-8")
            if paths.startup_command.exists()
            else None
        )
        if current_text != startup_text:
            paths.startup_command.write_text(
                startup_text,
                encoding="utf-8",
            )
            log(f"installed Startup launcher: {paths.startup_command}")
    except Exception as error:
        log(f"startup launcher install failed: {error}")

    existing_action = (
        paths.task_action_sentinel.read_text(encoding="utf-8").strip()
        if paths.task_action_sentinel.exists()
        else ""
    )
    registered = subprocess.run(
        ["schtasks", "/Query", "/TN", MESSAGE_HUB_TASK_NAME],
        capture_output=True,
    ).returncode == 0
    if not (registered and existing_action == task_action):
        result = subprocess.run(
            message_hub_launch_artifacts.create_minute_task_command(
                task_name=MESSAGE_HUB_TASK_NAME,
                task_action=task_action,
            ),
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            paths.task_action_sentinel.write_text(
                task_action,
                encoding="utf-8",
            )
            log(
                "installed MINUTE resurrector task: "
                f"{MESSAGE_HUB_TASK_NAME}"
            )
        else:
            detail = (result.stderr or result.stdout).strip()
            log(
                "MINUTE task create failed "
                f"(Startup launcher still active): {detail}"
            )

    launch_supervisor(pythonw_executable, supervisor_command)


def remove_message_hub_persistence(
    *,
    paths: MessageHubPersistencePaths,
    log: Callable[[str], None],
) -> None:
    """Remove persistence and stop the Windows message-hub processes."""
    removed = _unlink_if_present(paths.startup_command)
    task_registered = subprocess.run(
        ["schtasks", "/Query", "/TN", MESSAGE_HUB_TASK_NAME],
        capture_output=True,
    ).returncode == 0
    if task_registered:
        subprocess.run(
            [
                "schtasks",
                "/Delete",
                "/TN",
                MESSAGE_HUB_TASK_NAME,
                "/F",
            ],
            capture_output=True,
        )
        removed = True

    _unlink_if_present(paths.task_action_sentinel)
    try:
        paths.message_hub_stop_sentinel.write_text(
            str(int(time.time())),
            encoding="utf-8",
        )
    except Exception:
        pass

    for pid_file in (
        paths.message_hub_pid_file,
        paths.supervisor_pid_file,
    ):
        _kill_process_from_pid_file(pid_file)

    if removed:
        log("removed Windows persistence (not a host)")


def control_message_hub_persistence(
    action: str,
    *,
    paths: MessageHubControlPaths,
    process_is_alive: Callable[[int], bool],
    health_probe: Callable[[], dict | None],
) -> None:
    """Implement ``meeting am-msgd`` status/stop/restart on Windows."""
    if action == "status":
        query = subprocess.run(
            [
                "schtasks",
                "/Query",
                "/TN",
                MESSAGE_HUB_TASK_NAME,
                "/FO",
                "LIST",
            ],
            capture_output=True,
            text=True,
        )
        if query.returncode != 0:
            print(
                "logon task: not registered "
                f"({MESSAGE_HUB_TASK_NAME})"
            )
        else:
            for line in query.stdout.splitlines():
                line = line.strip()
                if line.startswith(
                    ("TaskName:", "Status:", "Next Run Time:")
                ):
                    print(line)
        supervisor_pid = _read_pid(paths.supervisor_pid_file)
        message_hub_pid = _read_pid(paths.message_hub_pid_file)
        print(
            "supervisor: "
            + (
                f"running pid={supervisor_pid}"
                if process_is_alive(supervisor_pid)
                else "not running"
            )
        )
        print(
            "central am-msgd proc: "
            + (
                f"running pid={message_hub_pid}"
                if process_is_alive(message_hub_pid)
                else "not running"
            )
        )
        health = health_probe()
        print(
            "central am-msgd /health: "
            + (
                f"ok, version {health.get('version', '?')}"
                if health
                else "unreachable"
            )
        )
        if paths.message_hub_stop_sentinel.exists():
            print(
                "stop sentinel: PRESENT "
                "(supervisor will not relaunch)"
            )
        return

    if action == "stop":
        try:
            paths.message_hub_stop_sentinel.write_text(
                str(int(time.time())),
                encoding="utf-8",
            )
        except Exception as error:
            print(
                f"could not write stop sentinel: {error}",
                file=sys.stderr,
            )
        subprocess.run(
            [
                "schtasks",
                "/End",
                "/TN",
                MESSAGE_HUB_TASK_NAME,
            ],
            capture_output=True,
        )
        for pid_file in (
            paths.message_hub_pid_file,
            paths.supervisor_pid_file,
        ):
            pid = _read_pid(pid_file)
            if pid > 0:
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)],
                    capture_output=True,
                )
        print(
            f"central am-msgd stopped: {MESSAGE_HUB_TASK_NAME} "
            "(stop sentinel set)"
        )
        print(
            "(note: next Claude SessionStart with is_host=true clears "
            "the sentinel and reinstalls + restarts it)"
        )
        return

    if action == "restart":
        _unlink_if_present(paths.message_hub_stop_sentinel)
        supervisor_pid = _read_pid(paths.supervisor_pid_file)
        if not process_is_alive(supervisor_pid):
            subprocess.run(
                [
                    "schtasks",
                    "/Run",
                    "/TN",
                    MESSAGE_HUB_TASK_NAME,
                ],
                capture_output=True,
            )
        message_hub_pid = _read_pid(paths.message_hub_pid_file)
        if message_hub_pid > 0:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(message_hub_pid)],
                capture_output=True,
            )
        print(
            f"central am-msgd restarting: {MESSAGE_HUB_TASK_NAME} "
            "(supervisor will relaunch it)"
        )


def _read_pid(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except Exception:
        return 0
