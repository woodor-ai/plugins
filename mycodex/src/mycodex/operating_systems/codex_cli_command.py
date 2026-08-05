"""Resolve Codex CLI invocations into directly executable OS commands."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys


def resolve(
    arguments: list[str],
    *,
    platform_name: str | None = None,
    environment: dict[str, str] | None = None,
    which=shutil.which,
) -> list[str]:
    """Return a command that ``subprocess`` can execute without a shell."""
    platform_name = sys.platform if platform_name is None else platform_name
    if not platform_name.startswith("win"):
        return ["codex", *arguments]

    environment = os.environ if environment is None else environment
    executable = which("codex.exe")
    if executable:
        return [executable, *arguments]
    batch_file = which("codex.cmd")
    if batch_file:
        command_processor = (
            environment.get("COMSPEC")
            or which("cmd.exe")
            or "cmd.exe"
        )
        batch_command = subprocess.list2cmdline([batch_file, *arguments])
        return [command_processor, "/d", "/s", "/c", batch_command]
    raise FileNotFoundError(
        "Codex CLI was not found as codex.exe or codex.cmd on PATH"
    )
