"""Cross-platform per-user service installation and lifecycle control."""

from __future__ import annotations

import os
import platform
import plistlib
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class UserServiceSpec:
    description: str
    command: tuple[str, ...]
    macos_label: str
    windows_task_name: str
    linux_unit_name: str
    log_path: Path
    process_type: str = "Background"
    environment: tuple[tuple[str, str], ...] = ()


def _run(command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True)


def _home(home: Path | None) -> Path:
    return Path.home() if home is None else home


def macos_plist_path(
    spec: UserServiceSpec,
    *,
    home: Path | None = None,
) -> Path:
    return _home(home) / "Library" / "LaunchAgents" / f"{spec.macos_label}.plist"


def macos_definition(spec: UserServiceSpec) -> bytes:
    payload = {
        "Label": spec.macos_label,
        "ProgramArguments": list(spec.command),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": spec.process_type,
        "StandardOutPath": str(spec.log_path),
        "StandardErrorPath": str(spec.log_path),
    }
    if spec.environment:
        payload["EnvironmentVariables"] = dict(spec.environment)
    return plistlib.dumps(payload)


def linux_unit_path(
    spec: UserServiceSpec,
    *,
    home: Path | None = None,
) -> Path:
    return _home(home) / ".config" / "systemd" / "user" / spec.linux_unit_name


def linux_definition(spec: UserServiceSpec) -> str:
    command = " ".join(shlex.quote(part) for part in spec.command)
    environment = "".join(
        f'Environment="{key}={value}"\n'
        for key, value in spec.environment
    )
    return (
        "[Unit]\n"
        f"Description={spec.description}\n"
        "After=network.target\n\n"
        "[Service]\n"
        f"ExecStart={command}\n"
        f"{environment}"
        "Restart=always\n"
        "RestartSec=2\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def windows_task_command(spec: UserServiceSpec) -> list[str]:
    action = subprocess.list2cmdline(list(spec.command))
    return [
        "schtasks",
        "/Create",
        "/TN",
        spec.windows_task_name,
        "/SC",
        "ONLOGON",
        "/TR",
        action,
        "/RL",
        "LIMITED",
        "/F",
    ]


def ensure_installed(
    spec: UserServiceSpec,
    *,
    system_name: str | None = None,
    home: Path | None = None,
) -> bool:
    system_name = system_name or platform.system()
    spec.log_path.parent.mkdir(parents=True, exist_ok=True)
    if system_name == "Darwin":
        path = macos_plist_path(spec, home=home)
        path.parent.mkdir(parents=True, exist_ok=True)
        expected = macos_definition(spec)
        changed = not path.exists() or path.read_bytes() != expected
        if changed:
            temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
            temporary.write_bytes(expected)
            os.replace(temporary, path)
        return changed
    if system_name == "Linux":
        path = linux_unit_path(spec, home=home)
        path.parent.mkdir(parents=True, exist_ok=True)
        expected = linux_definition(spec)
        changed = (
            not path.exists()
            or path.read_text(encoding="utf-8") != expected
        )
        if changed:
            temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
            temporary.write_text(expected, encoding="utf-8")
            os.replace(temporary, path)
            result = _run(["systemctl", "--user", "daemon-reload"])
            if result.returncode != 0:
                raise RuntimeError(
                    result.stderr.strip() or "systemctl daemon-reload failed"
                )
        return changed
    if system_name == "Windows":
        result = _run(windows_task_command(spec))
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        return True
    raise RuntimeError(f"unsupported user-service platform: {system_name}")


def _macos_bootstrap(domain: str, path: Path) -> subprocess.CompletedProcess:
    result = _run(["launchctl", "bootstrap", domain, str(path)])
    for _attempt in range(2):
        if result.returncode == 0:
            break
        time.sleep(0.1)
        result = _run(["launchctl", "bootstrap", domain, str(path)])
    return result


def is_installed(
    spec: UserServiceSpec,
    *,
    system_name: str | None = None,
    home: Path | None = None,
) -> bool:
    system_name = system_name or platform.system()
    if system_name == "Darwin":
        path = macos_plist_path(spec, home=home)
        try:
            payload = plistlib.loads(path.read_bytes())
        except (OSError, plistlib.InvalidFileException):
            return False
        return payload.get("ProgramArguments") == list(spec.command)
    if system_name == "Linux":
        path = linux_unit_path(spec, home=home)
        return (
            path.exists()
            and path.read_text(encoding="utf-8") == linux_definition(spec)
        )
    if system_name == "Windows":
        return _run(
            ["schtasks", "/Query", "/TN", spec.windows_task_name]
        ).returncode == 0
    return False


def start(
    spec: UserServiceSpec,
    *,
    system_name: str | None = None,
    home: Path | None = None,
) -> None:
    system_name = system_name or platform.system()
    definition_changed = ensure_installed(
        spec,
        system_name=system_name,
        home=home,
    )
    if system_name == "Darwin":
        path = macos_plist_path(spec, home=home)
        domain = f"gui/{os.getuid()}"
        target = f"{domain}/{spec.macos_label}"
        enabled = _run(["launchctl", "enable", target])
        if enabled.returncode != 0:
            raise RuntimeError(
                enabled.stderr.strip() or "launchctl enable failed"
            )
        listed = _run(["launchctl", "print", target])
        if listed.returncode == 0 and definition_changed:
            _run(["launchctl", "bootout", target])
            result = _macos_bootstrap(domain, path)
        elif listed.returncode == 0:
            result = _run(["launchctl", "kickstart", "-k", target])
        else:
            result = _macos_bootstrap(domain, path)
    elif system_name == "Linux":
        result = _run(
            ["systemctl", "--user", "enable", "--now", spec.linux_unit_name]
        )
    else:
        enabled = _run(
            ["schtasks", "/Change", "/TN", spec.windows_task_name, "/Enable"]
        )
        if enabled.returncode != 0:
            raise RuntimeError(enabled.stderr.strip() or enabled.stdout.strip())
        result = _run(["schtasks", "/Run", "/TN", spec.windows_task_name])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())


def stop(
    spec: UserServiceSpec,
    *,
    system_name: str | None = None,
) -> None:
    system_name = system_name or platform.system()
    if system_name == "Darwin":
        target = f"gui/{os.getuid()}/{spec.macos_label}"
        result = _run(["launchctl", "bootout", target])
        if (
            result.returncode not in (0, 3)
            and "Could not find service" not in result.stderr
        ):
            raise RuntimeError(result.stderr.strip() or "launchctl stop failed")
        disabled = _run(["launchctl", "disable", target])
        if disabled.returncode != 0:
            raise RuntimeError(
                disabled.stderr.strip() or "launchctl disable failed"
            )
        return
    if system_name == "Linux":
        result = _run(
            ["systemctl", "--user", "disable", "--now", spec.linux_unit_name]
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "systemctl stop failed")
        return
    if system_name == "Windows":
        _run(["schtasks", "/End", "/TN", spec.windows_task_name])
        result = _run(
            ["schtasks", "/Change", "/TN", spec.windows_task_name, "/Disable"]
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        return
    raise RuntimeError(f"unsupported user-service platform: {system_name}")


def restart(
    spec: UserServiceSpec,
    *,
    system_name: str | None = None,
    home: Path | None = None,
) -> None:
    system_name = system_name or platform.system()
    definition_changed = ensure_installed(
        spec,
        system_name=system_name,
        home=home,
    )
    if system_name == "Darwin":
        path = macos_plist_path(spec, home=home)
        domain = f"gui/{os.getuid()}"
        target = f"{domain}/{spec.macos_label}"
        enabled = _run(["launchctl", "enable", target])
        if enabled.returncode != 0:
            raise RuntimeError(
                enabled.stderr.strip() or "launchctl enable failed"
            )
        listed = _run(["launchctl", "print", target])
        if listed.returncode == 0 and definition_changed:
            _run(["launchctl", "bootout", target])
            result = _macos_bootstrap(domain, path)
        elif listed.returncode == 0:
            result = _run(["launchctl", "kickstart", "-k", target])
        else:
            result = _macos_bootstrap(domain, path)
    elif system_name == "Linux":
        enabled = _run(
            ["systemctl", "--user", "enable", spec.linux_unit_name]
        )
        if enabled.returncode != 0:
            raise RuntimeError(
                enabled.stderr.strip() or "systemctl enable failed"
            )
        result = _run(["systemctl", "--user", "restart", spec.linux_unit_name])
    else:
        _run(["schtasks", "/End", "/TN", spec.windows_task_name])
        enabled = _run(
            ["schtasks", "/Change", "/TN", spec.windows_task_name, "/Enable"]
        )
        if enabled.returncode != 0:
            raise RuntimeError(enabled.stderr.strip() or enabled.stdout.strip())
        result = _run(["schtasks", "/Run", "/TN", spec.windows_task_name])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())


def state(
    spec: UserServiceSpec,
    *,
    system_name: str | None = None,
) -> str:
    system_name = system_name or platform.system()
    if system_name == "Darwin":
        target = f"gui/{os.getuid()}/{spec.macos_label}"
        registered = _run(["launchctl", "print", target]).returncode == 0
        return "registered" if registered else "not-registered"
    if system_name == "Linux":
        result = _run(["systemctl", "--user", "is-active", spec.linux_unit_name])
        return result.stdout.strip() or "inactive"
    if system_name == "Windows":
        result = _run(["schtasks", "/Query", "/TN", spec.windows_task_name])
        return "registered" if result.returncode == 0 else "not-registered"
    return "unsupported"
