"""Expose stable mycodex commands through the Windows user PATH."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path


def _path_contains(current_path: str, entry: str) -> bool:
    normalized = os.path.normcase(entry.rstrip("\\/"))
    return normalized in {
        os.path.normcase(part.strip().rstrip("\\/"))
        for part in current_path.split(os.pathsep)
        if part.strip()
    }


def _broadcast_environment_change() -> None:
    result = ctypes.c_size_t()
    ctypes.windll.user32.SendMessageTimeoutW(
        0xFFFF,
        0x001A,
        0,
        "Environment",
        0x0002,
        5000,
        ctypes.byref(result),
    )


def ensure_command_directory(bin_directory: Path) -> None:
    import winreg

    entry = str(bin_directory)
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        "Environment",
        0,
        winreg.KEY_READ,
    ) as key:
        try:
            current, _kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current = ""
    if _path_contains(current or "", entry):
        return
    updated = (
        current.rstrip(os.pathsep) + os.pathsep
        if current
        else ""
    ) + entry
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        "Environment",
        0,
        winreg.KEY_SET_VALUE,
    ) as key:
        winreg.SetValueEx(
            key,
            "Path",
            0,
            winreg.REG_EXPAND_SZ,
            updated,
        )
    _broadcast_environment_change()
