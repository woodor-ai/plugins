"""Safe cross-platform process-liveness probes."""

from __future__ import annotations

import os
import sys


def is_windows_process_alive(process_id: int) -> bool:
    """Probe a Windows PID without using ``os.kill(pid, 0)``.

    Python maps signal zero to ``TerminateProcess`` on Windows, so the POSIX
    probe would be destructive there.
    """
    if process_id <= 0:
        return False
    import ctypes

    process_query_limited_information = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(
        process_query_limited_information,
        False,
        process_id,
    )
    if not handle:
        return False
    exit_code = ctypes.c_ulong(0)
    ctypes.windll.kernel32.GetExitCodeProcess(
        handle,
        ctypes.byref(exit_code),
    )
    ctypes.windll.kernel32.CloseHandle(handle)
    return exit_code.value == 259


def is_process_alive(process_id: int) -> bool:
    """Return whether a PID is alive on the current operating system."""
    if sys.platform.startswith("win"):
        return is_windows_process_alive(process_id)
    try:
        os.kill(process_id, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
