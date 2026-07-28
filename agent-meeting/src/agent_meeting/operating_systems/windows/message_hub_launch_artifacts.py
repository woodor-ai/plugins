"""Build argv-preserving Windows launch artifacts for the message hub."""

from __future__ import annotations

from pathlib import Path


def supervisor_task_action(
    pythonw_executable: Path,
    supervisor_script: Path,
    *,
    standalone: bool = False,
) -> str:
    if standalone:
        return f'"{supervisor_script}"'
    return f'"{pythonw_executable}" "{supervisor_script}"'


def startup_launcher_text(
    pythonw_executable: Path,
    supervisor_script: Path,
    *,
    standalone: bool = False,
) -> str:
    command = (
        f'"{supervisor_script}"'
        if standalone
        else f'"{pythonw_executable}" "{supervisor_script}"'
    )
    return (
        '@echo off\n'
        f'start "" {command}\n'
    )


def create_minute_task_command(
    *,
    task_name: str,
    task_action: str,
    interval_minutes: int = 2,
) -> list[str]:
    return [
        "schtasks",
        "/Create",
        "/TN",
        task_name,
        "/SC",
        "MINUTE",
        "/MO",
        str(interval_minutes),
        "/F",
        "/TR",
        task_action,
    ]
