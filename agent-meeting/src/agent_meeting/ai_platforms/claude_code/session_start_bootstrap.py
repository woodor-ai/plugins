"""Emit Claude Code SessionStart context from the installed host runtime."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from agent_meeting.ai_platforms.claude_code import (
    meeting_status_line,
    session_start_context,
)


def _meeting_home() -> Path:
    return Path(
        os.environ.get("MEETING_HOME")
        or (Path.home() / ".agent-meeting")
    )


def _claude_settings_path() -> Path:
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    return (
        Path(config_dir) / "settings.json"
        if config_dir
        else Path.home() / ".claude" / "settings.json"
    )


def _load_config(meeting_home: Path) -> dict:
    try:
        return json.loads(
            (meeting_home / "config.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return {}


def _log(message: str) -> None:
    meeting_home = _meeting_home()
    log_path = meeting_home / "logs" / "session-start.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(message + "\n")
    except OSError:
        pass


def main() -> None:
    try:
        meeting_home = _meeting_home()
        bin_dir = meeting_home / "bin"
        is_windows = sys.platform.startswith("win")
        suffix = ".exe" if is_windows else ""
        statusline = bin_dir / f"am-statusline{suffix}"
        meeting_status_line.install_meeting_status_line(
            settings_path=_claude_settings_path(),
            statusline_command=statusline,
            meeting_home=meeting_home,
            log=_log,
        )
        print(
            session_start_context.serialize_session_start_payload(
                config=_load_config(meeting_home),
                database_path=meeting_home / "db" / "rooms.db",
                am_command=bin_dir / f"am{suffix}",
                monitor_script=bin_dir / f"am-session-monitor{suffix}",
                python_executable=Path(sys.executable),
                is_windows=is_windows,
                is_codex_thread=bool(
                    os.environ.get("CODEX_THREAD_ID")
                    or os.environ.get("AGENT_MEETING_CODEX_RUNTIME")
                ),
                standalone_commands=True,
            )
        )
    except Exception as error:
        _log(f"SessionStart failed: {error}")
        print(json.dumps({}))


if __name__ == "__main__":
    main()
