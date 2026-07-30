"""tmux-backed terminal control."""

from __future__ import annotations

import shutil
import subprocess

from .base import TerminalAdapter, TerminalCapabilities


class TmuxTerminalAdapter(TerminalAdapter):
    def _target(self, handle: dict) -> tuple[str, str]:
        executable = shutil.which("tmux")
        pane = handle.get("pane")
        if not executable or not pane:
            raise ValueError("tmux terminal handle is unavailable")
        return executable, str(pane)

    def capabilities(self, handle: dict) -> TerminalCapabilities:
        available = bool(shutil.which("tmux") and handle.get("pane"))
        return TerminalCapabilities(
            can_send_text=available,
            can_interrupt=available,
            can_restart_in_place=available,
            can_resolve_window=available,
        )

    def send_text(self, handle: dict, text: str) -> bool:
        if "\n" in text or "\r" in text:
            raise ValueError("terminal action text must be one line")
        executable, pane = self._target(handle)
        result = subprocess.run(
            [executable, "send-keys", "-t", pane, "-l", text],
            capture_output=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0:
            return False
        enter = subprocess.run(
            [executable, "send-keys", "-t", pane, "Enter"],
            capture_output=True,
            timeout=5,
            check=False,
        )
        return enter.returncode == 0

    def send_interrupt(self, handle: dict, count: int = 2) -> bool:
        if count < 1:
            raise ValueError("interrupt count must be positive")
        executable, pane = self._target(handle)
        for _ in range(count):
            result = subprocess.run(
                [executable, "send-keys", "-t", pane, "C-c"],
                capture_output=True,
                timeout=5,
                check=False,
            )
            if result.returncode != 0:
                return False
        return True

    def restart_in_place(self, handle: dict) -> bool:
        # Restart recipes remain owned by the wrapper. tmux only guarantees that
        # the pane can receive input and interrupts.
        return False
