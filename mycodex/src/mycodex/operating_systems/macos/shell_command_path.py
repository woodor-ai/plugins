"""Expose the stable agent-meeting command directory to POSIX shells."""

from __future__ import annotations

import os
from pathlib import Path


PATH_MARKER = "# agent-meeting (mycodex on PATH)"


def shell_rc_path() -> Path:
    home = Path(os.environ.get("HOME") or Path.home())
    shell = os.environ.get("SHELL", "")
    return home / (".bashrc" if shell.endswith("bash") else ".zshrc")


def ensure_command_directory(bin_directory: Path) -> None:
    rc_path = shell_rc_path()
    text = (
        rc_path.read_text(encoding="utf-8")
        if rc_path.exists()
        else ""
    )
    entry = str(bin_directory)
    if PATH_MARKER in text or entry in text:
        return
    block = f'{PATH_MARKER}\nexport PATH="{entry}:$PATH"\n'
    updated = (
        (text.rstrip("\n") + "\n\n") if text.strip() else ""
    ) + block
    rc_path.parent.mkdir(parents=True, exist_ok=True)
    rc_path.write_text(updated, encoding="utf-8")


def remove_command_directory(bin_directory: Path) -> bool:
    """Remove the exact block written by :func:`ensure_command_directory`."""
    rc_path = shell_rc_path()
    if not rc_path.exists():
        return False
    text = rc_path.read_text(encoding="utf-8")
    block = (
        f'{PATH_MARKER}\n'
        f'export PATH="{bin_directory}:$PATH"\n'
    )
    if block not in text:
        return False
    updated = text.replace(block, "", 1)
    while "\n\n\n" in updated:
        updated = updated.replace("\n\n\n", "\n\n")
    rc_path.write_text(updated, encoding="utf-8")
    return True
