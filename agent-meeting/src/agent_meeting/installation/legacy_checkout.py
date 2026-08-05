"""Remove the updater-owned Git checkout from releases before 0.18.17."""

from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path


def legacy_checkout(meeting_home: Path) -> Path:
    return meeting_home / "updates" / "plugins"


def _retry_readonly_removal(function, path: str, exc_info) -> None:
    error = exc_info[1]
    if (
        not sys.platform.startswith("win")
        or not isinstance(error, PermissionError)
    ):
        raise error
    os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    function(path)


def remove_legacy_checkout(
    meeting_home: Path,
    *,
    suppress_errors: bool = False,
) -> bool:
    """Delete the legacy checkout, including read-only Windows Git objects."""
    checkout = legacy_checkout(meeting_home)
    try:
        shutil.rmtree(checkout, onerror=_retry_readonly_removal)
    except FileNotFoundError:
        return False
    except OSError as error:
        if not suppress_errors:
            raise
        print(
            f"WARNING: could not remove legacy update checkout {checkout}: "
            f"{error}",
            file=sys.stderr,
        )
        return False
    try:
        checkout.parent.rmdir()
    except OSError:
        pass
    return True
