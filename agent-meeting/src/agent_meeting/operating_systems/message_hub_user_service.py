"""User-service adapter for the local ``am-msgd`` process."""

from __future__ import annotations

import sys
from pathlib import Path

from agent_meeting.operating_systems import user_service


MACOS_LABEL = "com.tommy.agent-meeting.am-msgd"
WINDOWS_TASK_NAME = "agent-meeting-am-msgd"
LINUX_UNIT_NAME = "agent-meeting-am-msgd.service"


def _spec(
    meeting_home: Path,
    configuration_path: Path,
) -> user_service.UserServiceSpec:
    command_name = "am-msgd.exe" if sys.platform.startswith("win") else "am-msgd"
    return user_service.UserServiceSpec(
        description="agent-meeting local message hub",
        command=(
            str(meeting_home / "bin" / command_name),
            "serve",
            "--config",
            str(configuration_path),
        ),
        macos_label=MACOS_LABEL,
        windows_task_name=WINDOWS_TASK_NAME,
        linux_unit_name=LINUX_UNIT_NAME,
        log_path=meeting_home / "logs" / "am-msgd.log",
    )


def _macos_definition(
    meeting_home: Path,
    configuration_path: Path,
) -> bytes:
    return user_service.macos_definition(
        _spec(meeting_home, configuration_path)
    )


def ensure_installed(
    meeting_home: Path,
    configuration_path: Path,
    *,
    system_name: str | None = None,
) -> None:
    user_service.ensure_installed(
        _spec(meeting_home, configuration_path),
        system_name=system_name,
    )


def start(
    meeting_home: Path,
    configuration_path: Path,
    *,
    system_name: str | None = None,
) -> None:
    user_service.start(
        _spec(meeting_home, configuration_path),
        system_name=system_name,
    )


def stop(
    meeting_home: Path,
    *,
    system_name: str | None = None,
) -> None:
    user_service.stop(
        _spec(meeting_home, meeting_home / "am-msgd.json"),
        system_name=system_name,
    )


def restart(
    meeting_home: Path,
    configuration_path: Path,
    *,
    system_name: str | None = None,
) -> None:
    user_service.restart(
        _spec(meeting_home, configuration_path),
        system_name=system_name,
    )


def service_state(
    meeting_home: Path,
    *,
    system_name: str | None = None,
) -> str:
    return user_service.state(
        _spec(meeting_home, meeting_home / "am-msgd.json"),
        system_name=system_name,
    )
