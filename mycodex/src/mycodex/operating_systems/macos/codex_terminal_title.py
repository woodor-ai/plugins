"""Set the terminal title for a foreground mycodex session on macOS."""

from __future__ import annotations


def set_title(title: str) -> None:
    try:
        with open(
            "/dev/tty",
            "w",
            encoding="ascii",
            errors="replace",
        ) as terminal:
            terminal.write(f"\033]0;{title}\a")
            terminal.flush()
    except OSError:
        pass
