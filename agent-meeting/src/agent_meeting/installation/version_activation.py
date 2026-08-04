"""Atomically activate one immutable agent-meeting host runtime version."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path


PUBLIC_COMMANDS = (
    "am",
    "am-ctl",
    "am-msgd",
    "am-update",
    "amclaude",
    "amcodex",
    "am-codexd",
)
RUNTIME_COMMANDS = PUBLIC_COMMANDS + (
    "am-ctld",
    "am-session-monitor",
    "am-statusline",
    "am-claude-session-start",
)
OBSOLETE_COPIED_RUNTIME_FILES = (
    "am_common.py",
    "meeting_common.py",
    "monitor.py",
    "session-bootstrap.py",
    "statusline.py",
    "supervisor.py",
)


def runtime_command_path(
    runtime_dir: Path,
    command: str,
    *,
    is_windows: bool,
) -> Path:
    if is_windows:
        return runtime_dir / "venv" / "Scripts" / f"{command}.exe"
    return runtime_dir / "venv" / "bin" / command


def _atomic_write_json(path: Path, payload: dict) -> None:
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _activate_posix_command(source: Path, destination: Path) -> None:
    temporary = destination.with_name(
        f".{destination.name}.tmp.{os.getpid()}"
    )
    try:
        temporary.unlink()
    except FileNotFoundError:
        pass
    temporary.symlink_to(source)
    os.replace(temporary, destination)


def _activate_windows_command(source: Path, destination: Path) -> None:
    temporary = destination.with_name(
        f".{destination.name}.tmp.{os.getpid()}"
    )
    shutil.copy2(source, temporary)
    os.replace(temporary, destination)


def activate_runtime(
    *,
    meeting_home: Path,
    version: str,
    is_windows: bool | None = None,
) -> dict:
    is_windows = (
        sys.platform.startswith("win")
        if is_windows is None
        else is_windows
    )
    runtime_dir = meeting_home / "runtimes" / version
    if not runtime_dir.is_dir():
        raise FileNotFoundError(f"runtime version is not installed: {runtime_dir}")
    if (runtime_dir / ".installing").exists():
        raise RuntimeError(
            f"runtime version installation is incomplete: {runtime_dir}"
        )

    sources = {
        command: runtime_command_path(
            runtime_dir,
            command,
            is_windows=is_windows,
        )
        for command in RUNTIME_COMMANDS
    }
    missing = [str(path) for path in sources.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            "runtime is missing public command entrypoints: "
            + ", ".join(missing)
        )

    bin_dir = meeting_home / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for command, source in sources.items():
        destination = bin_dir / (
            f"{command}.exe" if is_windows else command
        )
        if is_windows:
            _activate_windows_command(source, destination)
        else:
            _activate_posix_command(source, destination)

    payload = {
        "version": version,
        "runtime": str(runtime_dir),
        "commands": {
            command: str(path)
            for command, path in sources.items()
        },
    }
    _atomic_write_json(meeting_home / "active-runtime.json", payload)
    obsolete_commands = (
        (
            "meeting.exe",
            "mycodex.exe",
            "lnk.exe",
            "am-configure-codex-user-environment.exe",
            "am-message-hub-supervisor.exe",
        )
        if is_windows
        else (
            "meeting",
            "mycodex",
            "lnk",
            "am-configure-codex-user-environment",
            "am-message-hub-supervisor",
        )
    )
    for command in obsolete_commands:
        try:
            (bin_dir / command).unlink()
        except FileNotFoundError:
            pass
    for filename in OBSOLETE_COPIED_RUNTIME_FILES:
        try:
            (bin_dir / filename).unlink()
        except FileNotFoundError:
            pass
    shutil.rmtree(bin_dir / "__pycache__", ignore_errors=True)
    return payload
