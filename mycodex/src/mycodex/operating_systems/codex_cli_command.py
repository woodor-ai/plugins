"""Resolve Codex CLI invocations into directly executable OS commands."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path


def _npm_native_executable(batch_file: str) -> str | None:
    npm_root = Path(batch_file).parent
    package_root = npm_root / "node_modules" / "@openai" / "codex"
    candidates = sorted(
        package_root.glob(
            "node_modules/@openai/codex-win32-*/vendor/*/bin/codex.exe"
        )
    )
    return str(candidates[0]) if candidates else None


def resolve(
    arguments: list[str],
    *,
    platform_name: str | None = None,
    which=shutil.which,
) -> list[str]:
    """Return a command that ``subprocess`` can execute without a shell."""
    platform_name = sys.platform if platform_name is None else platform_name
    if not platform_name.startswith("win"):
        return ["codex", *arguments]

    executable = which("codex.exe")
    if executable:
        return [executable, *arguments]
    batch_file = which("codex.cmd")
    if batch_file:
        native_executable = _npm_native_executable(batch_file)
        if native_executable:
            return [native_executable, *arguments]
        raise FileNotFoundError(
            "Codex npm launcher was found, but its native Windows executable "
            "is missing; reinstall @openai/codex"
        )
    raise FileNotFoundError(
        "Codex CLI was not found as codex.exe or codex.cmd on PATH"
    )
