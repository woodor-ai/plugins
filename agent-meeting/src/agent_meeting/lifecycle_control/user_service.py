"""Install the per-user ``am-ctld`` login service on macOS and Windows."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path


MACOS_LABEL = "ai.woodor.am-ctld"
WINDOWS_TASK_NAME = "woodor-am-ctld"


def _run(command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True)


def _ensure_macos(meeting_home: Path) -> None:
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    logs = meeting_home / "control"
    logs.mkdir(parents=True, exist_ok=True)
    plist_path = launch_agents / f"{MACOS_LABEL}.plist"
    definition = plistlib.dumps(
        {
            "Label": MACOS_LABEL,
            "ProgramArguments": [
                str(meeting_home / "bin" / "am-ctld"),
                "--meeting-home",
                str(meeting_home),
            ],
            "RunAtLoad": True,
            "KeepAlive": True,
            "ProcessType": "Interactive",
            "StandardOutPath": str(logs / "am-ctld.log"),
            "StandardErrorPath": str(logs / "am-ctld.log"),
            "EnvironmentVariables": {
                "MEETING_HOME": str(meeting_home),
            },
        }
    )
    if not plist_path.exists() or plist_path.read_bytes() != definition:
        plist_path.write_bytes(definition)
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{MACOS_LABEL}"
    _run(["launchctl", "bootout", target])
    result = _run(["launchctl", "bootstrap", domain, str(plist_path)])
    if result.returncode not in (0, 5):
        raise RuntimeError(result.stderr.strip() or "could not bootstrap am-ctld")
    _run(["launchctl", "kickstart", "-k", target])


def _macos_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{MACOS_LABEL}.plist"


def lifecycle_service_manages(meeting_home: Path) -> bool:
    if sys.platform == "darwin":
        path = _macos_plist_path()
        try:
            definition = plistlib.loads(path.read_bytes())
        except (OSError, plistlib.InvalidFileException):
            return False
        arguments = definition.get("ProgramArguments") or []
        return str(meeting_home) in arguments
    return False


def start_lifecycle_control_service(meeting_home: Path) -> bool:
    if not lifecycle_service_manages(meeting_home):
        return False
    if sys.platform == "darwin":
        domain = f"gui/{os.getuid()}"
        path = _macos_plist_path()
        result = _run(["launchctl", "bootstrap", domain, str(path)])
        if result.returncode not in (0, 5):
            raise RuntimeError(
                result.stderr.strip() or "could not bootstrap am-ctld"
            )
        result = _run(
            ["launchctl", "kickstart", "-k", f"{domain}/{MACOS_LABEL}"]
        )
        if result.returncode != 0:
            raise RuntimeError(
                result.stderr.strip() or "could not start am-ctld"
            )
        return True
    return False


def stop_lifecycle_control_service(meeting_home: Path) -> bool:
    if not lifecycle_service_manages(meeting_home):
        return False
    if sys.platform == "darwin":
        target = f"gui/{os.getuid()}/{MACOS_LABEL}"
        result = _run(["launchctl", "bootout", target])
        if result.returncode == 3:
            return False
        if result.returncode != 0:
            raise RuntimeError(
                result.stderr.strip() or "could not stop am-ctld"
            )
        return True
    return False


def _ensure_windows(meeting_home: Path) -> None:
    command = meeting_home / "bin" / "am-ctld.exe"
    task_command = f'"{command}" --meeting-home "{meeting_home}"'
    create = _run(
        [
            "schtasks",
            "/Create",
            "/TN",
            WINDOWS_TASK_NAME,
            "/SC",
            "ONLOGON",
            "/TR",
            task_command,
            "/RL",
            "LIMITED",
            "/F",
        ]
    )
    if create.returncode != 0:
        raise RuntimeError(create.stderr.strip() or create.stdout.strip())
    _run(["schtasks", "/Run", "/TN", WINDOWS_TASK_NAME])


def ensure_lifecycle_control_service(meeting_home: Path) -> None:
    if sys.platform == "darwin":
        _ensure_macos(meeting_home)
    elif sys.platform.startswith("win"):
        _ensure_windows(meeting_home)
