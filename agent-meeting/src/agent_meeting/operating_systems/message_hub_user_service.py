"""Cross-platform user-service management for the local ``am-msgd``."""

from __future__ import annotations

import os
import platform
import plistlib
import subprocess
import sys
from pathlib import Path


MACOS_LABEL = "com.tommy.agent-meeting.am-msgd"
WINDOWS_TASK_NAME = "agent-meeting-am-msgd"
LINUX_UNIT_NAME = "agent-meeting-am-msgd.service"


def _run(command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True)


def _macos_paths(meeting_home: Path) -> tuple[Path, str, str]:
    plist_path = (
        Path.home()
        / "Library"
        / "LaunchAgents"
        / f"{MACOS_LABEL}.plist"
    )
    domain = f"gui/{os.getuid()}"
    return plist_path, domain, f"{domain}/{MACOS_LABEL}"


def _macos_definition(
    meeting_home: Path,
    configuration_path: Path,
) -> bytes:
    command = meeting_home / "bin" / "am-msgd"
    log_path = meeting_home / "logs" / "am-msgd.log"
    return plistlib.dumps(
        {
            "Label": MACOS_LABEL,
            "ProgramArguments": [
                str(command),
                "serve",
                "--config",
                str(configuration_path),
            ],
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(log_path),
            "StandardErrorPath": str(log_path),
            "ProcessType": "Background",
        }
    )


def _ensure_macos_definition(
    meeting_home: Path,
    configuration_path: Path,
) -> tuple[str, str, bool]:
    plist_path, domain, target = _macos_paths(meeting_home)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    (meeting_home / "logs").mkdir(parents=True, exist_ok=True)
    expected = _macos_definition(meeting_home, configuration_path)
    changed = not plist_path.exists() or plist_path.read_bytes() != expected
    if changed:
        temporary = plist_path.with_name(
            f".{plist_path.name}.tmp.{os.getpid()}"
        )
        temporary.write_bytes(expected)
        os.replace(temporary, plist_path)
    return domain, target, changed


def _linux_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / LINUX_UNIT_NAME


def _ensure_linux_definition(
    meeting_home: Path,
    configuration_path: Path,
) -> None:
    unit_path = _linux_unit_path()
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    command = meeting_home / "bin" / "am-msgd"
    expected = (
        "[Unit]\n"
        "Description=agent-meeting local message hub\n"
        "After=network.target\n\n"
        "[Service]\n"
        f"ExecStart={command} serve --config {configuration_path}\n"
        "Restart=always\n"
        "RestartSec=2\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    if not unit_path.exists() or unit_path.read_text(encoding="utf-8") != expected:
        temporary = unit_path.with_name(
            f".{unit_path.name}.tmp.{os.getpid()}"
        )
        temporary.write_text(expected, encoding="utf-8")
        os.replace(temporary, unit_path)
        result = _run(["systemctl", "--user", "daemon-reload"])
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "systemctl daemon-reload failed")


def ensure_installed(
    meeting_home: Path,
    configuration_path: Path,
    *,
    system_name: str | None = None,
) -> None:
    system_name = system_name or platform.system()
    if system_name == "Darwin":
        _ensure_macos_definition(meeting_home, configuration_path)
        return
    if system_name == "Linux":
        _ensure_linux_definition(meeting_home, configuration_path)
        return
    if system_name == "Windows":
        # The existing Windows persistence installer owns Task Scheduler and
        # Startup artifacts. Its launch command is migrated separately.
        return
    raise RuntimeError(f"unsupported am-msgd service platform: {system_name}")


def start(
    meeting_home: Path,
    configuration_path: Path,
    *,
    system_name: str | None = None,
) -> None:
    system_name = system_name or platform.system()
    ensure_installed(
        meeting_home,
        configuration_path,
        system_name=system_name,
    )
    if system_name == "Darwin":
        plist_path, domain, target = _macos_paths(meeting_home)
        enable = _run(["launchctl", "enable", target])
        if enable.returncode != 0:
            raise RuntimeError(
                enable.stderr.strip() or "launchctl enable failed"
            )
        result = _run(["launchctl", "print", target])
        if result.returncode != 0:
            result = _run(["launchctl", "bootstrap", domain, str(plist_path)])
        else:
            result = _run(["launchctl", "kickstart", target])
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "launchctl start failed")
        return
    if system_name == "Linux":
        result = _run(
            ["systemctl", "--user", "enable", "--now", LINUX_UNIT_NAME]
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "systemctl start failed")
        return
    if system_name == "Windows":
        sentinel = meeting_home / "am-msgd.stopped"
        try:
            sentinel.unlink()
        except FileNotFoundError:
            pass
        result = _run(["schtasks", "/Run", "/TN", WINDOWS_TASK_NAME])
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "schtasks start failed")
        return


def stop(
    meeting_home: Path,
    *,
    system_name: str | None = None,
) -> None:
    system_name = system_name or platform.system()
    if system_name == "Darwin":
        _, _, target = _macos_paths(meeting_home)
        result = _run(["launchctl", "bootout", target])
        if result.returncode != 0 and "Could not find service" not in result.stderr:
            raise RuntimeError(result.stderr.strip() or "launchctl stop failed")
        result = _run(["launchctl", "disable", target])
        if result.returncode != 0:
            raise RuntimeError(
                result.stderr.strip() or "launchctl disable failed"
            )
        return
    if system_name == "Linux":
        result = _run(
            ["systemctl", "--user", "disable", "--now", LINUX_UNIT_NAME]
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "systemctl stop failed")
        return
    if system_name == "Windows":
        (meeting_home / "am-msgd.stopped").write_text(
            "stopped by am-msgd stop\n",
            encoding="utf-8",
        )
        _run(["schtasks", "/End", "/TN", WINDOWS_TASK_NAME])
        return
    raise RuntimeError(f"unsupported am-msgd service platform: {system_name}")


def restart(
    meeting_home: Path,
    configuration_path: Path,
    *,
    system_name: str | None = None,
) -> None:
    system_name = system_name or platform.system()
    if system_name == "Darwin":
        domain, target, changed = _ensure_macos_definition(
            meeting_home,
            configuration_path,
        )
        plist_path, _, _ = _macos_paths(meeting_home)
        enable = _run(["launchctl", "enable", target])
        if enable.returncode != 0:
            raise RuntimeError(
                enable.stderr.strip() or "launchctl enable failed"
            )
        if changed:
            _run(["launchctl", "bootout", target])
            result = _run(
                ["launchctl", "bootstrap", domain, str(plist_path)]
            )
            if result.returncode != 0:
                raise RuntimeError(
                    result.stderr.strip() or "launchctl reload failed"
                )
            return
        result = _run(["launchctl", "kickstart", "-k", target])
        if result.returncode != 0:
            start(
                meeting_home,
                configuration_path,
                system_name=system_name,
            )
        return
    ensure_installed(
        meeting_home,
        configuration_path,
        system_name=system_name,
    )
    if system_name == "Linux":
        result = _run(["systemctl", "--user", "restart", LINUX_UNIT_NAME])
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "systemctl restart failed")
        return
    if system_name == "Windows":
        stop(meeting_home, system_name=system_name)
        start(
            meeting_home,
            configuration_path,
            system_name=system_name,
        )
        return


def service_state(
    meeting_home: Path,
    *,
    system_name: str | None = None,
) -> str:
    system_name = system_name or platform.system()
    if system_name == "Darwin":
        _, _, target = _macos_paths(meeting_home)
        return "registered" if _run(["launchctl", "print", target]).returncode == 0 else "not-registered"
    if system_name == "Linux":
        result = _run(["systemctl", "--user", "is-active", LINUX_UNIT_NAME])
        return result.stdout.strip() or "inactive"
    if system_name == "Windows":
        result = _run(["schtasks", "/Query", "/TN", WINDOWS_TASK_NAME])
        return "registered" if result.returncode == 0 else "not-registered"
    return "unsupported"
