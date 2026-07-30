"""Capture a non-secret terminal handle for lifecycle capability routing."""

from __future__ import annotations

import os


def current_terminal_handle() -> dict:
    tmux_pane = os.environ.get("TMUX_PANE")
    if tmux_pane:
        return {
            "type": "tmux",
            "pane": tmux_pane,
        }

    iterm_session = os.environ.get("ITERM_SESSION_ID")
    if iterm_session:
        return {
            "type": "iterm2",
            "session_id": iterm_session,
        }

    windows_terminal = os.environ.get("WT_SESSION")
    if windows_terminal:
        return {
            "type": "windows-terminal",
            "session_id": windows_terminal,
            "conpty_owned": False,
        }

    return {
        "type": "tty",
        "tty": None,
    }
