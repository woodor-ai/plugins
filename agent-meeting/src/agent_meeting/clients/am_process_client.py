"""Subprocess invocation for the public ``am`` command."""

from __future__ import annotations

import os
import subprocess
import sys


def run_am_cli(
    cli_path,
    *args,
    python=None,
    host=None,
    cwd=None,
    timeout=15,
    env=None,
):
    cli_path = os.fspath(cli_path)
    is_posix_shell_wrapper = False
    if python and not sys.platform.startswith("win"):
        try:
            with open(cli_path, "rb") as executable:
                first_line = executable.readline(200)
            is_posix_shell_wrapper = (
                first_line.startswith(b"#!") and b"sh" in first_line
            )
        except OSError:
            pass

    command = [] if is_posix_shell_wrapper else ([str(python)] if python else [])
    command += [cli_path, *args]
    if host:
        command += ["--host", host]
    process_options = (
        {"creationflags": 0x08000000} if sys.platform.startswith("win") else {}
    )
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
        env=env if env is not None else os.environ.copy(),
        **process_options,
    )
