"""Install and control the per-user ``am-ctld`` service."""

from __future__ import annotations

import sys
from pathlib import Path

from agent_meeting.operating_systems import user_service


MACOS_LABEL = "ai.woodor.am-ctld"
WINDOWS_TASK_NAME = "woodor-am-ctld"
LINUX_UNIT_NAME = "woodor-am-ctld.service"


def _spec(meeting_home: Path) -> user_service.UserServiceSpec:
    command_name = "am-ctld.exe" if sys.platform.startswith("win") else "am-ctld"
    return user_service.UserServiceSpec(
        description="agent-meeting lifecycle controller",
        command=(
            str(meeting_home / "bin" / command_name),
            "--meeting-home",
            str(meeting_home),
        ),
        macos_label=MACOS_LABEL,
        windows_task_name=WINDOWS_TASK_NAME,
        linux_unit_name=LINUX_UNIT_NAME,
        log_path=meeting_home / "control" / "am-ctld.log",
        process_type="Interactive",
        environment=(("MEETING_HOME", str(meeting_home)),),
    )


def lifecycle_service_manages(meeting_home: Path) -> bool:
    return user_service.is_installed(_spec(meeting_home))


def start_lifecycle_control_service(meeting_home: Path) -> bool:
    spec = _spec(meeting_home)
    if not user_service.is_installed(spec):
        return False
    user_service.start(spec)
    return True


def stop_lifecycle_control_service(meeting_home: Path) -> bool:
    spec = _spec(meeting_home)
    if not user_service.is_installed(spec):
        return False
    user_service.stop(spec)
    return True


def ensure_lifecycle_control_service(meeting_home: Path) -> None:
    user_service.restart(_spec(meeting_home))
