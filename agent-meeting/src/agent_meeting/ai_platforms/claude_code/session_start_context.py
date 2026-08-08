"""Build Claude Code SessionStart context for an agent-meeting installation."""

from __future__ import annotations

import json
import socket
import sqlite3
import time
from pathlib import Path

from agent_meeting.operating_systems.bash_command import bash_argument


def read_online_peers(
    database_path: Path,
    *,
    now: float | None = None,
    heartbeat_window: float = 12,
) -> str:
    if not database_path.exists():
        return "(none online)"
    try:
        connection = sqlite3.connect(
            f"file:{database_path}?mode=ro",
            uri=True,
            timeout=2,
        )
        try:
            cutoff = (time.time() if now is None else now) - heartbeat_window
            rows = connection.execute(
                "SELECT name, project FROM sessions "
                "WHERE last_seen >= ? ORDER BY name, project",
                (cutoff,),
            ).fetchall()
        finally:
            connection.close()
        peers = [
            name if project == "*" else f"{name}@{project}"
            for name, project in rows
        ]
        return ", ".join(peers) if peers else "(none online)"
    except Exception:
        return "(none online)"


def build_session_start_payload(
    *,
    config: dict,
    database_path: Path,
    am_command: Path,
    monitor_script: Path,
    python_executable: Path,
    is_windows: bool,
    is_codex_thread: bool,
    launchd_warning: str = "",
    hostname: str | None = None,
    online_peers: str | None = None,
    standalone_commands: bool = False,
    assigned_name: str | None = None,
    assigned_project: str | None = None,
    control_url: str | None = None,
) -> dict:
    role = "host" if config.get("is_host") else "client"
    peers = (
        read_online_peers(database_path)
        if online_peers is None
        else online_peers
    )
    hostname = hostname or socket.gethostname()

    if standalone_commands:
        cli = bash_argument(am_command) if is_windows else str(am_command)
        monitor = f"{bash_argument(monitor_script)} <name>"
        os_label = "windows" if is_windows else "posix"
    elif is_windows:
        cli = f"{bash_argument(python_executable)} {bash_argument(am_command)}"
        monitor = (
            f"{bash_argument(python_executable)} "
            f"{bash_argument(monitor_script)} <name>"
        )
        os_label = "windows"
    else:
        cli = str(am_command)
        monitor = f"python3 {monitor_script} <name>"
        os_label = "posix"

    if assigned_name and not is_codex_thread:
        registration = [bash_argument(monitor_script), assigned_name]
        if assigned_project == "*":
            registration.append("--global")
        elif assigned_project:
            registration.append(f"--proj={assigned_project}")
        if control_url:
            registration.extend(["--host", control_url])
        monitor_description = f"📬 agent-meeting messages from {assigned_name}"
        registration_context = f"""This session was launched by `amclaude` with the meeting name `{assigned_name}` already chosen on the command line. It is NOT registered yet: registration happens when the monitor starts.

- Start the monitor with the Monitor tool BEFORE doing anything else, using this exact input:
  - `command`: `{" ".join(registration)}`
  - `description`: `{monitor_description}`
  - `persistent`: `true`
- Do NOT ask the user to run `/imagent`, and do NOT pick a different name: they already chose this one.
- Once the monitor is running, report exactly one line at the end of your first reply: `📞 Meeting registered as {assigned_name}`.
- If the monitor exits non-zero, surface its output verbatim, do not retry, and do not add `--force`. Then carry on with the user's task: registration is never a prerequisite for the work."""
    elif is_codex_thread:
        registration_context = """This is a Codex session. An `amcodex` launch supplies its exact agent-meeting recipient and control URL through thread and turn request parameters. Pass those values as explicit `am` CLI arguments; do not use `MEETING_SELF` or `AM_MSGD_HOST`.

If no agent-meeting recipient is present in the current runtime context, this Codex session is not registered — and that's fine. Peer-to-peer agent messaging is optional. Never gate, block, or delay the user's work to register a name.

- Proceed with whatever the user asks, normally — registration is NOT a prerequisite for anything.
- If the user's message contains a `/imagent` command — whether it is the whole message (`/imagent <name>`) OR embedded inside a natural-language directive (e.g. "You are X, first run /imagent X --director to start listening") — run the imagent skill: they are explicitly opting in to register.
- Reminder line: on your first reply only, you may append the line below only when no agent-meeting recipient was injected and this session did not register:
  > 💡 This session has no meeting name yet; to communicate with other agents, use `/imagent <name>` to register (does not affect your current task).
  Show it at most once and never let it replace or postpone the actual task."""
    else:
        registration_context = """This session has NO meeting name yet — and that's fine. Peer-to-peer agent messaging is OPTIONAL. NEVER gate, block, or delay the user's work to make them register a name.

- Proceed with whatever the user asks, normally — registration is NOT a prerequisite for anything.
- If the user's message contains a `/imagent` command — whether it is the whole message (`/imagent <name>`) OR embedded inside a natural-language directive (e.g. "You are X, first run /imagent X --director to start listening") — run the imagent skill: they are explicitly opting in to register.
- Reminder line: on your FIRST reply of this session ONLY, you MAY append this single line at the very end — but SKIP it entirely whenever this session registers via `/imagent` (i.e. you run the imagent skill this turn), no matter where the command appeared in the user's message. Only show the reminder when the session does NOT register at all:
  > 💡 This session has no meeting name yet; to communicate with other agents, use `/imagent <name>` to register (does not affect your current task).
  Decide by your own action (did you register?), NOT by whether the message literally starts with `/imagent`. Show it at most once per session, never repeat it, and never let it replace or postpone the actual task."""

    context = f"""📞 Meeting system is active.

{registration_context}

These paths are ALREADY RESOLVED for this machine — use them verbatim, do NOT probe the filesystem to find the CLI or venv:
- CLI invocation: `{cli} <args>`
- Monitor tool command (bash): `{monitor}`

Backend: SQLite at {database_path}.
Machine: `{hostname}` (role: {role}, os: {os_label}).
Online peers: {peers}
"""
    if launchd_warning:
        context += f"\n{launchd_warning}\n"
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }


def serialize_session_start_payload(**kwargs) -> str:
    return json.dumps(build_session_start_payload(**kwargs))
