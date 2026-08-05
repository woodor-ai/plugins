"""The status line must survive a non-UTF-8 stdout and carry every segment.

Claude Code runs the renderer with stdout attached to a pipe, so Python picks
the locale encoding. On a stock Windows box that is cp1252, which cannot encode
the badge emoji: before the renderer pinned UTF-8, every registered session's
status line raised UnicodeEncodeError and the last-resort handler swallowed the
whole line, leaving the bar blank. The subprocess tests below reproduce that
exact condition through PYTHONIOENCODING, which names a codec rather than a
platform and so fails the same way on any host.
"""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RENDERER = (
    REPOSITORY_ROOT
    / "agent-meeting"
    / "src"
    / "agent_meeting"
    / "ai_platforms"
    / "claude_code"
    / "meeting_status_line_process.py"
)
SESSION_ID = "11111111-2222-3333-4444-555555555555"


def _register(meeting_home: Path, **overrides) -> None:
    """Write the cache file monitor.py leaves behind for a registered session."""
    from agent_meeting.ai_platforms.claude_code import (
        meeting_status_line_process as renderer,
    )

    payload = {
        "name": "am-win",
        "project": "plugins",
        "control_host": "OMI-MacDev",
        "control_ip_port": "10.0.0.114:8765",
    }
    payload.update(overrides)
    directory = meeting_home / "statusline"
    directory.mkdir(parents=True, exist_ok=True)
    key = renderer._badge_key(SESSION_ID, "")
    (directory / key).write_text(json.dumps(payload), encoding="utf-8")


def _render(payload: dict, *, meeting_home: Path, claude_home: Path,
            stdout_encoding: str = "utf-8") -> str:
    environment = dict(os.environ)
    environment["MEETING_HOME"] = str(meeting_home)
    environment["CLAUDE_CONFIG_DIR"] = str(claude_home)
    environment["PYTHONIOENCODING"] = stdout_encoding
    completed = subprocess.run(
        [sys.executable, str(RENDERER)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


@pytest.fixture(autouse=True)
def add_source_root(monkeypatch):
    monkeypatch.syspath_prepend(str(REPOSITORY_ROOT / "agent-meeting" / "src"))


@pytest.fixture
def homes(tmp_path):
    return tmp_path / "meeting", tmp_path / "claude"


FULL_PAYLOAD = {
    "session_id": SESSION_ID,
    "cwd": "/repo",
    "model": {"display_name": "Opus 5"},
    "effort": {"level": "high"},
    "workspace": {"current_dir": "/repo"},
    "version": "2.1.222",
    "context_window": {"used_percentage": 37},
    "rate_limits": {
        "five_hour": {"used_percentage": 22.4},
        "seven_day": {"used_percentage": 8.1},
    },
}


def test_a_registered_badge_survives_a_cp1252_stdout(homes):
    meeting_home, claude_home = homes
    _register(meeting_home)

    rendered = _render(
        FULL_PAYLOAD,
        meeting_home=meeting_home,
        claude_home=claude_home,
        stdout_encoding="cp1252",
    )

    assert "\U0001F4DE am-win@plugins" in rendered
    assert "\U0001F6F0 10.0.0.114:8765" in rendered
    assert "Opus 5 · high" in rendered


def _layout(rendered: str) -> list[list[str]]:
    return [
        [segment.strip() for segment in line.split("|")]
        for line in rendered.split("\n")
    ]


def test_identity_and_numbers_split_across_two_lines(homes):
    meeting_home, claude_home = homes
    _register(meeting_home)
    tasks = claude_home / "tasks" / SESSION_ID
    tasks.mkdir(parents=True)
    for index, status in enumerate(["completed", "completed", "pending"]):
        (tasks / f"{index}.json").write_text(
            json.dumps({"id": str(index), "status": status}),
            encoding="utf-8",
        )

    rendered = _render(
        FULL_PAYLOAD, meeting_home=meeting_home, claude_home=claude_home
    )

    assert _layout(rendered) == [
        [
            "\U0001F4DE am-win@plugins \U0001F6F0 10.0.0.114:8765",
            "Opus 5 · high",
            "/repo",
            "v2.1.222",
        ],
        ["ctx 63% left", "5h 78% left", "wk 92% left", "tasks 2/3"],
    ]


def test_unavailable_segments_drop_out_instead_of_rendering_blanks(homes):
    meeting_home, claude_home = homes

    rendered = _render(
        {
            "session_id": SESSION_ID,
            "workspace": {"current_dir": "/repo"},
            "model": {"display_name": "Haiku 4.5"},
            "version": "2.1.222",
            "context_window": {"used_percentage": None},
            "rate_limits": {},
        },
        meeting_home=meeting_home,
        claude_home=claude_home,
    )

    # Nothing on the second line survives here, so the bar collapses to one
    # line rather than emitting a trailing newline Claude Code would drop.
    assert _layout(rendered) == [["Haiku 4.5", "/repo", "v2.1.222"]]
