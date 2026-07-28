"""Set the console title for a foreground mycodex session on Windows."""

from __future__ import annotations


def set_title(title: str) -> None:
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleTitleW(title)
    except Exception:
        pass
