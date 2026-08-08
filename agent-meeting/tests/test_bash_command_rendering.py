"""Every command string a host hands to bash must survive the MSYS layer.

Windows hosts spawn bash with the command line we wrote. The MSYS runtime
parses that command line first and eats a backslash as an escape unless the
spawning process happened to add quotes of its own, which it only does when the
string contains a space. A backslash path therefore works or breaks depending
on whether the install path has a space in it. These tests hold every emitter
to POSIX separators so the outcome no longer depends on that accident.
"""

import json
from pathlib import Path, PureWindowsPath
import shlex

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WINDOWS_HOME = PureWindowsPath(r"C:\Users\User\.agent-meeting")


@pytest.fixture(autouse=True)
def add_source_root(monkeypatch):
    monkeypatch.syspath_prepend(str(REPOSITORY_ROOT / "agent-meeting" / "src"))


def test_bash_argument_keeps_a_spaceless_windows_path_intact():
    from agent_meeting.operating_systems.bash_command import bash_argument

    rendered = bash_argument(WINDOWS_HOME / "bin" / "am.exe")

    assert "\\" not in rendered
    assert shlex.split(rendered) == ["C:/Users/User/.agent-meeting/bin/am.exe"]


def test_bash_argument_quotes_a_path_that_contains_spaces():
    from agent_meeting.operating_systems.bash_command import bash_argument

    rendered = bash_argument(
        PureWindowsPath(r"C:\Users\Tommy Wang\.agent-meeting\bin\am.exe")
    )

    assert "\\" not in rendered
    assert shlex.split(rendered) == [
        "C:/Users/Tommy Wang/.agent-meeting/bin/am.exe"
    ]


def test_session_start_hook_carries_no_backslash(tmp_path):
    from agent_meeting.installation import claude_integration

    command = claude_integration.session_start_command(
        tmp_path / "agent-meeting",
        is_windows=True,
    )

    assert "\\" not in command
    assert shlex.split(command) == [
        (tmp_path / "agent-meeting" / "bin" / "am-claude-session-start.exe")
        .as_posix()
    ]


def test_status_line_install_carries_no_backslash(tmp_path):
    from agent_meeting.ai_platforms.claude_code import meeting_status_line

    settings_path = tmp_path / "claude" / "settings.json"
    settings_path.parent.mkdir()
    meeting_home = tmp_path / "agent-meeting"
    statusline = meeting_home / "bin" / "am-statusline.exe"

    meeting_status_line.install_meeting_status_line(
        settings_path=settings_path,
        statusline_command=statusline,
        meeting_home=meeting_home,
        log=lambda message: None,
    )

    command = json.loads(settings_path.read_text(encoding="utf-8"))[
        "statusLine"
    ]["command"]
    assert "\\" not in command
    assert shlex.split(command) == [statusline.as_posix()]


def test_status_line_install_replaces_the_entry_an_earlier_release_wrote(
    tmp_path,
):
    from agent_meeting.ai_platforms.claude_code import meeting_status_line

    settings_path = tmp_path / "claude" / "settings.json"
    settings_path.parent.mkdir()
    meeting_home = tmp_path / "agent-meeting"
    statusline = meeting_home / "bin" / "am-statusline.exe"
    # 0.18.28 and earlier wrote a cmd.exe-quoted backslash path, then failed to
    # recognize it as ours and froze it in place.
    stale = f'"{PureWindowsPath(statusline)}"'
    settings_path.write_text(
        json.dumps({"statusLine": {"command": stale, "padding": 0}}),
        encoding="utf-8",
    )

    meeting_status_line.install_meeting_status_line(
        settings_path=settings_path,
        statusline_command=statusline,
        meeting_home=meeting_home,
        log=lambda message: None,
    )

    command = json.loads(settings_path.read_text(encoding="utf-8"))[
        "statusLine"
    ]["command"]
    assert command == shlex.quote(statusline.as_posix())


def test_status_line_install_leaves_a_foreign_status_line_alone(tmp_path):
    from agent_meeting.ai_platforms.claude_code import meeting_status_line

    settings_path = tmp_path / "claude" / "settings.json"
    settings_path.parent.mkdir()
    meeting_home = tmp_path / "agent-meeting"
    foreign = {"type": "command", "command": "my-own-status-line"}
    settings_path.write_text(
        json.dumps({"statusLine": foreign}),
        encoding="utf-8",
    )

    meeting_status_line.install_meeting_status_line(
        settings_path=settings_path,
        statusline_command=meeting_home / "bin" / "am-statusline.exe",
        meeting_home=meeting_home,
        log=lambda message: None,
    )

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["statusLine"] == foreign


def test_windows_session_context_advertises_no_backslash_commands(tmp_path):
    from agent_meeting.ai_platforms.claude_code.session_start_context import (
        build_session_start_payload,
    )

    for standalone in (True, False):
        payload = build_session_start_payload(
            config={"is_host": True},
            database_path=tmp_path / "db" / "rooms.db",
            am_command=tmp_path / "bin" / "am.exe",
            monitor_script=tmp_path / "bin" / "am-session-monitor.exe",
            python_executable=tmp_path / "venv" / "Scripts" / "python.exe",
            is_windows=True,
            is_codex_thread=False,
            online_peers="(none online)",
            hostname="test-host",
            standalone_commands=standalone,
        )

        context = payload["hookSpecificOutput"]["additionalContext"]
        advertised = [
            line
            for line in context.splitlines()
            if line.startswith("- CLI invocation:")
            or line.startswith("- Monitor tool command")
        ]
        assert len(advertised) == 2
        for line in advertised:
            assert "\\" not in line


def test_assigned_session_context_orders_the_monitor_not_a_slash_command(
    tmp_path,
):
    from agent_meeting.ai_platforms.claude_code.session_start_context import (
        build_session_start_payload,
    )

    payload = build_session_start_payload(
        config={"is_host": True},
        database_path=tmp_path / "db" / "rooms.db",
        am_command=tmp_path / "bin" / "am.exe",
        monitor_script=tmp_path / "bin" / "am-session-monitor.exe",
        python_executable=tmp_path / "venv" / "Scripts" / "python.exe",
        is_windows=True,
        is_codex_thread=False,
        online_peers="(none online)",
        hostname="test-host",
        standalone_commands=True,
        assigned_name="worker",
        assigned_project="tools",
        control_url="http://10.0.0.114:8765",
    )

    context = payload["hookSpecificOutput"]["additionalContext"]
    monitor = (tmp_path / "bin" / "am-session-monitor.exe").as_posix()
    assert f"{monitor} worker --proj=tools --host http://10.0.0.114:8765" in context
    assert "`description`: `📬 agent-meeting inbox for worker`" in context
    assert "`persistent`: `true`" in context
    assert "NO meeting name yet" not in context
    assert "\\" not in context.split("Backend:")[0]


def test_manual_monitor_uses_the_same_description_contract():
    skill = (
        REPOSITORY_ROOT
        / "agent-meeting"
        / "skills"
        / "imagent"
        / "SKILL.md"
    ).read_text(encoding="utf-8")

    assert "`description`: `📬 agent-meeting inbox for <name>`" in skill
    assert "`~/.agent-meeting/bin/am message <recipient> <msg_id>`" in skill
    assert "Read recent history" not in skill
    assert "📞 agent-meeting: incoming call" not in skill


def test_a_global_assigned_session_registers_without_a_project(tmp_path):
    from agent_meeting.ai_platforms.claude_code.session_start_context import (
        build_session_start_payload,
    )

    payload = build_session_start_payload(
        config={"is_host": True},
        database_path=tmp_path / "db" / "rooms.db",
        am_command=tmp_path / "bin" / "am",
        monitor_script=tmp_path / "bin" / "am-session-monitor",
        python_executable=tmp_path / "venv" / "bin" / "python",
        is_windows=False,
        is_codex_thread=False,
        online_peers="(none online)",
        hostname="test-host",
        standalone_commands=True,
        assigned_name="director",
        assigned_project="*",
    )

    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "am-session-monitor director --global" in context
    assert "--proj=" not in context


def test_an_unassigned_session_still_gets_the_optional_registration_notice(
    tmp_path,
):
    from agent_meeting.ai_platforms.claude_code.session_start_context import (
        build_session_start_payload,
    )

    payload = build_session_start_payload(
        config={"is_host": True},
        database_path=tmp_path / "db" / "rooms.db",
        am_command=tmp_path / "bin" / "am",
        monitor_script=tmp_path / "bin" / "am-session-monitor",
        python_executable=tmp_path / "venv" / "bin" / "python",
        is_windows=False,
        is_codex_thread=False,
        online_peers="(none online)",
        hostname="test-host",
        standalone_commands=True,
    )

    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "NO meeting name yet" in context
