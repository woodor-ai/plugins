#!/usr/bin/env python3
"""Install the stable handoff-update command and expose it on the user PATH."""

from __future__ import annotations

import ctypes
import os
import shlex
import stat
import sys
from pathlib import Path


PATH_BLOCK_BEGIN = "# >>> handoff command >>>"
PATH_BLOCK_END = "# <<< handoff command <<<"


def _atomic_write(path: Path, content: bytes, *, mode: int | None = None) -> None:
    if path.exists() and path.read_bytes() == content:
        if mode is None or stat.S_IMODE(path.stat().st_mode) == mode:
            return
    existing_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    temporary.write_bytes(content)
    effective_mode = mode if mode is not None else existing_mode
    if effective_mode is not None:
        temporary.chmod(effective_mode)
    os.replace(temporary, path)


def _path_contains(current: str, entry: Path) -> bool:
    normalized = os.path.normcase(str(entry).rstrip("\\/"))
    return normalized in {
        os.path.normcase(part.strip().rstrip("\\/"))
        for part in current.split(os.pathsep)
        if part.strip()
    }


def _shell_rc_path(home: Path, shell: str) -> Path:
    if shell.endswith("bash"):
        return home / ".bashrc"
    if shell.endswith("zsh"):
        return home / ".zshrc"
    return home / ".profile"


def _ensure_posix_path(bin_directory: Path, *, home: Path, environ: dict[str, str]) -> None:
    if _path_contains(environ.get("PATH", ""), bin_directory):
        return
    rc_path = _shell_rc_path(home, environ.get("SHELL", ""))
    existing = rc_path.read_text(encoding="utf-8") if rc_path.exists() else ""
    block = (
        f"{PATH_BLOCK_BEGIN}\n"
        f"export PATH={shlex.quote(str(bin_directory))}:\"$PATH\"\n"
        f"{PATH_BLOCK_END}\n"
    )
    if block in existing:
        return
    if PATH_BLOCK_BEGIN in existing and PATH_BLOCK_END in existing:
        before, remainder = existing.split(PATH_BLOCK_BEGIN, 1)
        _old_block, after = remainder.split(PATH_BLOCK_END, 1)
        updated = before + block + after.lstrip("\n")
    else:
        updated = ((existing.rstrip("\n") + "\n\n") if existing.strip() else "") + block
    rc_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(rc_path, updated.encode("utf-8"))


def _ensure_windows_path(bin_directory: Path) -> None:
    import winreg

    entry = str(bin_directory)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ) as key:
        try:
            current, _kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current = ""
    if _path_contains(current or "", bin_directory):
        return
    updated = (current.rstrip(os.pathsep) + os.pathsep if current else "") + entry
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, updated)
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


def install_updater(
    *,
    source: Path,
    handoff_home: Path,
    home: Path,
    python_executable: Path,
    is_windows: bool,
    environ: dict[str, str],
    update_path: bool = True,
) -> Path:
    bin_directory = handoff_home / "bin"
    bin_directory.mkdir(parents=True, exist_ok=True)
    script = bin_directory / "handoff-update.py"
    _atomic_write(script, source.read_bytes())

    if is_windows:
        launcher = bin_directory / "handoff-update.cmd"
        windows_python = str(python_executable).replace("/", "\\")
        command = f'@echo off\r\n"{windows_python}" "%~dp0handoff-update.py" %*\r\n'
        _atomic_write(launcher, command.encode("utf-8"))
        if update_path:
            _ensure_windows_path(bin_directory)
    else:
        launcher = bin_directory / "handoff-update"
        command = (
            "#!/bin/sh\nexec "
            f"{shlex.quote(str(python_executable))} "
            f"{shlex.quote(str(script))} \"$@\"\n"
        )
        _atomic_write(
            launcher,
            command.encode("utf-8"),
            mode=stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR,
        )
        if update_path:
            _ensure_posix_path(bin_directory, home=home, environ=environ)
    return launcher


def main() -> None:
    home = Path.home()
    handoff_home = Path(os.environ.get("HANDOFF_HOME") or (home / ".handoff"))
    install_updater(
        source=Path(__file__).resolve().parent / "handoff-update.py",
        handoff_home=handoff_home,
        home=home,
        python_executable=Path(sys.executable),
        is_windows=sys.platform.startswith("win"),
        environ=dict(os.environ),
    )


if __name__ == "__main__":
    main()
