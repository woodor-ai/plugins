"""Public mycodex command: update the distribution or launch one Codex lease."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from mycodex.launcher import codex_tui_session


REPOSITORY_URL = "https://github.com/woodor-ai/plugins"


def _platform_installer_command(
    checkout: Path,
    arguments: list[str],
    *,
    is_windows: bool | None = None,
) -> list[str]:
    """Return the OS-specific Codex installer command for this checkout."""
    is_windows = (
        sys.platform.startswith("win")
        if is_windows is None
        else is_windows
    )
    if is_windows:
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if not powershell:
            raise RuntimeError(
                "PowerShell not found; cannot run the Windows installer"
            )
        return [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(
                checkout
                / "installers"
                / "codex"
                / "install-on-windows.ps1"
            ),
            *arguments,
        ]
    return [
        "/bin/sh",
        str(
            checkout
            / "installers"
            / "codex"
            / "install-on-macos.sh"
        ),
        *arguments,
    ]


def _update_distribution(arguments: list[str]) -> int:
    git = shutil.which("git")
    if not git:
        print("ERROR: git not found. Install git and re-run.", file=sys.stderr)
        return 1

    codex_home = Path(
        os.environ.get("CODEX_HOME") or (Path.home() / ".codex")
    )
    checkout = codex_home / "plugins-src"
    if (checkout / ".git").is_dir():
        print(f"Updating {checkout} ...")
        command = [git, "-C", str(checkout), "pull", "--ff-only"]
    else:
        print(f"Cloning {REPOSITORY_URL} to {checkout} ...")
        command = [git, "clone", REPOSITORY_URL, str(checkout)]
    result = subprocess.run(command)
    if result.returncode != 0:
        return result.returncode

    print("\nInstalling the versioned Codex host runtime ...")
    try:
        installer_command = _platform_installer_command(
            checkout,
            arguments,
        )
    except RuntimeError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return subprocess.run(installer_command).returncode


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] in (["update"], ["--update"]):
        return _update_distribution(arguments[1:])
    return codex_tui_session.main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
