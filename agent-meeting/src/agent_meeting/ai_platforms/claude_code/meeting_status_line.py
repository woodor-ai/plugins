"""Install the agent-meeting status line without replacing user customizations."""

from __future__ import annotations

import json
from pathlib import Path


def install_meeting_status_line(
    *,
    settings_path: Path,
    statusline_command: Path,
    python_executable: Path | None = None,
    log,
) -> None:
    if not settings_path.parent.is_dir():
        return

    if python_executable is None:
        command = f'"{statusline_command}"'
    else:
        command = f'"{python_executable}" "{statusline_command}"'
    settings = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception:
            log("settings.json malformed — skipping statusLine install")
            return

    existing = settings.get("statusLine")
    if isinstance(existing, dict):
        current_command = existing.get("command", "")
        if "statusline.py" not in current_command:
            log("a custom statusLine is configured — leaving it untouched")
            return
        if current_command == command:
            return

    settings["statusLine"] = {
        "type": "command",
        "command": command,
        "padding": 0,
    }
    try:
        settings_path.write_text(
            json.dumps(settings, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        log(f"installed statusLine → {statusline_command}")
    except Exception as error:
        log(f"statusLine install failed: {error}")
