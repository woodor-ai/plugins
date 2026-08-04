import json
import sqlite3
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def add_source_root(monkeypatch):
    monkeypatch.syspath_prepend(str(PLUGIN_ROOT / "src"))


def test_claude_context_reads_composite_online_identities(tmp_path):
    from agent_meeting.ai_platforms.claude_code.session_start_context import (
        build_session_start_payload,
    )
    from agent_meeting.message_hub.sqlite_message_database import (
        prepare_message_database,
    )

    database = tmp_path / "rooms.db"
    prepare_message_database(database)
    connection = sqlite3.connect(database)
    connection.executemany(
        "INSERT INTO sessions(project, name, last_seen) VALUES (?, ?, ?)",
        [
            ("project-a", "alpha", 1000),
            ("*", "global-agent", 1000),
        ],
    )
    connection.close()

    payload = build_session_start_payload(
        config={"is_host": False},
        database_path=database,
        am_command=tmp_path / "bin" / "am",
        monitor_script=tmp_path / "bin" / "am-session-monitor",
        python_executable=tmp_path / "venv" / "bin" / "python",
        is_windows=False,
        is_codex_thread=False,
        online_peers="alpha@project-a, global-agent",
        hostname="test-host",
        standalone_commands=True,
    )

    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "Machine: `test-host` (role: client, os: posix)." in context
    assert "Online peers: alpha@project-a, global-agent" in context
    assert "/imagent <name>" in context
    assert json.loads(json.dumps(payload)) == payload
