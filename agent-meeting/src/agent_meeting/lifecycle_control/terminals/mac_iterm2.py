"""iTerm2 Automation adapter.

The adapter deliberately uses iTerm2's AppleScript session API so the first
action follows normal macOS Automation/TCC prompting. It never edits TCC or
iTerm2 preferences.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

from .base import TerminalAdapter, TerminalCapabilities


_WRITE_SCRIPT = """
on run argv
  set targetId to item 1 of argv
  set inputText to item 2 of argv
  tell application "iTerm2"
    repeat with targetWindow in windows
      repeat with targetTab in tabs of targetWindow
        repeat with targetSession in sessions of targetTab
          if unique ID of targetSession is targetId then
            tell targetSession to write text inputText
            return "ok"
          end if
        end repeat
      end repeat
    end repeat
  end tell
  return "not-found"
end run
""".strip()


class Iterm2TerminalAdapter(TerminalAdapter):
    @staticmethod
    def _session_id(handle: dict) -> str:
        value = str(handle.get("session_id") or "")
        # ITERM_SESSION_ID has historically used "w0t0p0:UUID" while the
        # AppleScript session unique ID is the UUID portion.
        return value.rsplit(":", 1)[-1]

    def capabilities(self, handle: dict) -> TerminalCapabilities:
        available = bool(
            sys.platform == "darwin"
            and shutil.which("osascript")
            and self._session_id(handle)
        )
        return TerminalCapabilities(
            can_send_text=available,
            can_interrupt=False,
            can_restart_in_place=False,
            can_resolve_window=available,
            requires_user_permission=available,
        )

    def send_text(self, handle: dict, text: str) -> bool:
        if "\n" in text or "\r" in text:
            raise ValueError("terminal action text must be one line")
        executable = shutil.which("osascript")
        session_id = self._session_id(handle)
        if not executable or not session_id or sys.platform != "darwin":
            return False
        result = subprocess.run(
            [executable, "-e", _WRITE_SCRIPT, session_id, text],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return result.returncode == 0 and result.stdout.strip() == "ok"

    def send_interrupt(self, handle: dict, count: int = 2) -> bool:
        return False

    def restart_in_place(self, handle: dict) -> bool:
        return False
