"""Create the host Python environment and install required runtime packages."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def environment_python(venv_path: Path, *, is_windows: bool) -> Path:
    if is_windows:
        return venv_path / "Scripts" / "python.exe"
    return venv_path / "bin" / "python"


def ensure_python_environment(
    venv_path: Path,
    *,
    is_windows: bool,
    log,
) -> Path:
    python_executable = environment_python(
        venv_path,
        is_windows=is_windows,
    )
    if python_executable.exists():
        return python_executable
    log(f"creating venv at {venv_path}")
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv_path)],
        check=True,
        capture_output=True,
    )
    return python_executable


def ensure_python_dependency(
    python_executable: Path,
    package_name: str,
    *,
    import_name: str | None = None,
    log,
) -> None:
    import_name = import_name or package_name
    probe = subprocess.run(
        [str(python_executable), "-c", f"import {import_name}"],
        capture_output=True,
    )
    if probe.returncode == 0:
        return
    log(f"installing {package_name} into venv (one-time, ~10s)")
    subprocess.run(
        [
            str(python_executable),
            "-m",
            "pip",
            "install",
            "--quiet",
            package_name,
        ],
        check=True,
    )
