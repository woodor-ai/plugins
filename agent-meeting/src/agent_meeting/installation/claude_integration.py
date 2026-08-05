"""Install and remove the marketplace-free Claude Code integration."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


SKILL_NAMES = ("imagent", "talkto")
OWNER_FILE = ".agent-meeting-owner.json"


def _is_owned_skill(directory: Path) -> bool:
    try:
        payload = json.loads(
            (directory / OWNER_FILE).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return False
    return payload.get("product") == "agent-meeting"


def _install_skill(
    source: Path,
    destination: Path,
    version: str,
    *,
    bootstrap_script: Path | None = None,
) -> None:
    if destination.exists() and not _is_owned_skill(destination):
        raise RuntimeError(
            f"refusing to replace unowned Claude skill: {destination}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.agent-meeting.tmp.{os.getpid()}"
    )
    backup = destination.with_name(
        f".{destination.name}.agent-meeting.backup.{os.getpid()}"
    )
    shutil.rmtree(temporary, ignore_errors=True)
    shutil.rmtree(backup, ignore_errors=True)
    shutil.copytree(source, temporary)
    if bootstrap_script is not None:
        scripts = temporary / "scripts"
        scripts.mkdir()
        shutil.copy2(bootstrap_script, scripts / bootstrap_script.name)
    (temporary / OWNER_FILE).write_text(
        json.dumps(
            {
                "product": "agent-meeting",
                "schema_version": 1,
                "version": version,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    if destination.exists():
        os.replace(destination, backup)
    try:
        os.replace(temporary, destination)
    except Exception:
        if backup.exists() and not destination.exists():
            os.replace(backup, destination)
        raise
    shutil.rmtree(backup, ignore_errors=True)


def install_skills(
    *,
    source_root: Path,
    claude_home: Path,
    version: str,
) -> None:
    source_skills = source_root / "agent-meeting" / "skills"
    bootstrap_script = (
        source_root / "agent-meeting" / "scripts" / "bootstrap_runtime.py"
    )
    for skill_name in SKILL_NAMES:
        _install_skill(
            source_skills / skill_name,
            claude_home / "skills" / skill_name,
            version,
            bootstrap_script=(
                bootstrap_script if skill_name == "imagent" else None
            ),
        )


def remove_skills(claude_home: Path) -> None:
    for skill_name in SKILL_NAMES:
        directory = claude_home / "skills" / skill_name
        if not directory.exists():
            continue
        if not _is_owned_skill(directory):
            print(
                f"warning: preserving unowned Claude skill: {directory}",
                file=sys.stderr,
            )
            continue
        shutil.rmtree(directory)


def session_start_executable(
    meeting_home: Path,
    *,
    is_windows: bool | None = None,
) -> Path:
    is_windows = (
        sys.platform.startswith("win")
        if is_windows is None
        else is_windows
    )
    suffix = ".exe" if is_windows else ""
    return meeting_home / "bin" / f"am-claude-session-start{suffix}"


def session_start_command(
    meeting_home: Path,
    *,
    is_windows: bool | None = None,
) -> str:
    is_windows = (
        sys.platform.startswith("win")
        if is_windows is None
        else is_windows
    )
    executable = session_start_executable(
        meeting_home,
        is_windows=is_windows,
    )
    if is_windows:
        return subprocess.list2cmdline([str(executable)])
    return shlex.quote(str(executable))


def _read_settings(settings_path: Path) -> dict:
    if not settings_path.exists():
        return {}
    try:
        payload = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(
            f"Claude settings are invalid: {settings_path}: {error}"
        ) from error
    if not isinstance(payload, dict):
        raise RuntimeError(f"Claude settings must be a JSON object: {settings_path}")
    return payload


def _write_settings(settings_path: Path, payload: dict) -> None:
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = settings_path.with_name(
        f".{settings_path.name}.agent-meeting.tmp.{os.getpid()}"
    )
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, settings_path)


def _without_command(groups: list, command: str) -> list:
    updated = []
    for group in groups:
        if not isinstance(group, dict):
            updated.append(group)
            continue
        handlers = group.get("hooks")
        if not isinstance(handlers, list):
            updated.append(group)
            continue
        remaining = [
            handler
            for handler in handlers
            if not (
                isinstance(handler, dict)
                and handler.get("type") == "command"
                and handler.get("command") == command
            )
        ]
        if remaining:
            updated.append({**group, "hooks": remaining})
    return updated


def install_user_configuration(
    *,
    settings_path: Path,
    meeting_home: Path,
    is_windows: bool | None = None,
) -> None:
    command = session_start_command(
        meeting_home,
        is_windows=is_windows,
    )
    settings = _read_settings(settings_path)
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise RuntimeError("Claude settings hooks must be a JSON object")
    groups = hooks.setdefault("SessionStart", [])
    if not isinstance(groups, list):
        raise RuntimeError("Claude SessionStart hooks must be a JSON array")
    groups = _without_command(groups, command)
    groups.append(
        {
            "matcher": "startup",
            "hooks": [
                {
                    "type": "command",
                    "command": command,
                }
            ],
        }
    )
    hooks["SessionStart"] = groups
    _write_settings(settings_path, settings)


def _owned_status_line(command: object, meeting_home: Path) -> bool:
    if not isinstance(command, str):
        return False
    normalized = command.replace("\\", "/").lower()
    root = str(meeting_home).replace("\\", "/").lower().rstrip("/")
    return root in normalized and (
        "/bin/am-statusline" in normalized
        or "/bin/statusline.py" in normalized
    )


def remove_user_configuration(
    *,
    settings_path: Path,
    meeting_home: Path,
    is_windows: bool | None = None,
) -> None:
    if not settings_path.exists():
        return
    command = session_start_command(
        meeting_home,
        is_windows=is_windows,
    )
    settings = _read_settings(settings_path)
    changed = False
    hooks = settings.get("hooks")
    if isinstance(hooks, dict):
        groups = hooks.get("SessionStart")
        if isinstance(groups, list):
            remaining = _without_command(groups, command)
            if remaining != groups:
                changed = True
                if remaining:
                    hooks["SessionStart"] = remaining
                else:
                    hooks.pop("SessionStart", None)
        if not hooks:
            settings.pop("hooks", None)
    status_line = settings.get("statusLine")
    if isinstance(status_line, dict) and _owned_status_line(
        status_line.get("command"),
        meeting_home,
    ):
        settings.pop("statusLine", None)
        changed = True
    if changed:
        _write_settings(settings_path, settings)
