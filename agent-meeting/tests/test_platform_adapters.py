import json
import plistlib
import sqlite3
import subprocess
from types import SimpleNamespace
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PLUGIN_ROOT / "src"


@pytest.fixture(autouse=True)
def add_source_root(monkeypatch):
    monkeypatch.syspath_prepend(str(SRC_ROOT))


def test_macos_launch_agent_names_the_message_hub(tmp_path):
    from agent_meeting.operating_systems.macos.message_hub_launch_agent import (
        build_message_hub_launch_agent,
    )

    command = tmp_path / "bin" / "am-msgd"
    log_path = tmp_path / "logs" / "am-msgd.log"
    payload = plistlib.loads(
        build_message_hub_launch_agent(
            label="com.tommy.agent-meeting.am-msgd",
            message_hub_command=command,
            log_path=log_path,
        )
    )

    assert payload["Label"] == "com.tommy.agent-meeting.am-msgd"
    assert payload["ProgramArguments"] == [str(command), "--port", "8765"]
    assert payload["KeepAlive"] is True
    assert payload["StandardOutPath"] == str(log_path)


def test_windows_launch_artifacts_preserve_quoted_paths():
    from agent_meeting.operating_systems.windows import (
        message_hub_launch_artifacts,
    )

    pythonw = Path(r"C:\Users\Test User\.agent-meeting\venv\pythonw.exe")
    supervisor = Path(
        r"C:\Users\Test User\.agent-meeting\bin\supervisor.py"
    )
    action = message_hub_launch_artifacts.supervisor_task_action(
        pythonw,
        supervisor,
    )

    assert action == f'"{pythonw}" "{supervisor}"'
    assert message_hub_launch_artifacts.startup_launcher_text(
        pythonw,
        supervisor,
    ) == f'@echo off\nstart "" "{pythonw}" "{supervisor}"\n'
    assert message_hub_launch_artifacts.create_minute_task_command(
        task_name="agent-meeting-am-msgd",
        task_action=action,
    )[-2:] == ["/TR", action]


def test_windows_message_hub_persistence_stays_no_admin(
    tmp_path,
    monkeypatch,
):
    from agent_meeting.operating_systems.windows import (
        message_hub_persistence,
    )

    startup_directory = tmp_path / "Startup"
    paths = message_hub_persistence.MessageHubPersistencePaths(
        startup_directory=startup_directory,
        startup_command=startup_directory / "agent-meeting-am-msgd.cmd",
        pre_message_hub_startup_command=(
            startup_directory / "agent-meeting-daemon.cmd"
        ),
        legacy_amctl_startup_command=(
            startup_directory / "agent-meeting-amctl.cmd"
        ),
        task_action_sentinel=tmp_path / ".schtasks-cmd",
        message_hub_stop_sentinel=tmp_path / "am-msgd.stopped",
        legacy_amctl_stop_sentinel=tmp_path / "amctl.stopped",
        message_hub_pid_file=tmp_path / "am-msgd.pid",
        legacy_amctl_pid_file=tmp_path / "amctl.pid",
        supervisor_pid_file=tmp_path / "meeting-supervisor.pid",
    )
    pythonw = tmp_path / "venv" / "Scripts" / "pythonw.exe"
    supervisor = tmp_path / "bin" / "am-message-hub-supervisor.exe"
    commands = []

    def fake_run(args, **_kwargs):
        commands.append(list(args))
        return subprocess.CompletedProcess(args, 1 if "/Query" in args else 0)

    monkeypatch.setattr(message_hub_persistence.subprocess, "run", fake_run)
    callbacks = []

    message_hub_persistence.ensure_message_hub_persistence(
        paths=paths,
        pythonw_executable=pythonw,
        supervisor_command=supervisor,
        stop_bootstrap_message_hub=lambda: callbacks.append("stop-bootstrap"),
        launch_supervisor=lambda *_args: callbacks.append("launch-supervisor"),
        log=lambda _message: None,
    )

    assert callbacks == ["stop-bootstrap", "launch-supervisor"]
    assert paths.startup_command.read_text(encoding="utf-8") == (
        f'@echo off\nstart "" "{supervisor}"\n'
    )
    create_command = next(
        command
        for command in commands
        if command[:2] == ["schtasks", "/Create"]
    )
    assert create_command[
        create_command.index("/SC") + 1
    ] == "MINUTE"
    assert "ONLOGON" not in create_command


def test_windows_message_hub_stop_is_owned_by_os_adapter(
    tmp_path,
    monkeypatch,
):
    from agent_meeting.operating_systems.windows import (
        message_hub_persistence,
    )

    message_hub_pid = tmp_path / "am-msgd.pid"
    supervisor_pid = tmp_path / "supervisor.pid"
    message_hub_pid.write_text("101", encoding="utf-8")
    supervisor_pid.write_text("202", encoding="utf-8")
    paths = message_hub_persistence.MessageHubControlPaths(
        message_hub_stop_sentinel=tmp_path / "am-msgd.stopped",
        message_hub_pid_file=message_hub_pid,
        supervisor_pid_file=supervisor_pid,
    )
    commands = []

    monkeypatch.setattr(
        message_hub_persistence.subprocess,
        "run",
        lambda args, **_kwargs: (
            commands.append(list(args))
            or subprocess.CompletedProcess(args, 0, "", "")
        ),
    )

    message_hub_persistence.control_message_hub_persistence(
        "stop",
        paths=paths,
        process_is_alive=lambda _pid: True,
        health_probe=lambda: {"ok": True},
    )

    assert paths.message_hub_stop_sentinel.is_file()
    assert ["taskkill", "/F", "/PID", "101"] in commands
    assert ["taskkill", "/F", "/PID", "202"] in commands


def test_message_hub_start_prefers_activated_session_start_command(
    tmp_path,
    monkeypatch,
    capsys,
):
    from agent_meeting.commands import message_hub_lifecycle_commands

    meeting_home = tmp_path / "meeting-home"
    stable_command = meeting_home / "bin" / "am-claude-session-start"
    stable_command.parent.mkdir(parents=True)
    stable_command.write_text("#!/bin/sh\n", encoding="utf-8")
    paths = message_hub_lifecycle_commands.MessageHubLifecyclePaths(
        meeting_home=meeting_home,
        config_path=meeting_home / "config.json",
        plugin_root=tmp_path / "agent-meeting",
    )
    calls = []
    monkeypatch.setattr(
        message_hub_lifecycle_commands.subprocess,
        "run",
        lambda command, **kwargs: (
            calls.append((command, kwargs))
            or subprocess.CompletedProcess(command, 0, "", "")
        ),
    )

    message_hub_lifecycle_commands.run_message_hub_lifecycle_command(
        SimpleNamespace(action=None),
        paths=paths,
        port=8765,
        windows_process_is_alive=lambda _pid: False,
        health_probe=lambda: None,
        system_name="Darwin",
    )

    assert calls[0][0] == [str(stable_command)]
    assert json.loads(paths.config_path.read_text())["is_host"] is True
    assert "session/message hub" in capsys.readouterr().out


def test_macos_message_hub_restart_is_owned_by_launchd_adapter(
    tmp_path,
    monkeypatch,
    capsys,
):
    from agent_meeting.operating_systems.macos import (
        message_hub_launch_agent,
    )

    commands = []

    def fake_run(args, **_kwargs):
        commands.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(
        message_hub_launch_agent.subprocess,
        "run",
        fake_run,
    )
    monkeypatch.setattr(message_hub_launch_agent.os, "getuid", lambda: 501)

    message_hub_launch_agent.control_message_hub_launch_agent(
        "restart",
        label="com.tommy.agent-meeting.am-msgd",
        plist_path=tmp_path / "agent.plist",
        port=8765,
        host_is_enabled=True,
        health_probe=lambda: {"ok": True},
    )

    target = "gui/501/com.tommy.agent-meeting.am-msgd"
    assert ["launchctl", "print", target] in commands
    assert ["launchctl", "kickstart", "-k", target] in commands
    assert "central am-msgd restarted" in capsys.readouterr().out


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

    # Supply the already-formatted peer list so the payload test is independent
    # from wall-clock time; read_online_peers has its own coverage elsewhere.
    payload = build_session_start_payload(
        config={"is_host": False},
        database_path=database,
        meeting_command=tmp_path / "bin" / "meeting",
        monitor_script=tmp_path / "bin" / "monitor.py",
        python_executable=tmp_path / "venv" / "bin" / "python",
        is_windows=False,
        is_codex_thread=False,
        online_peers="alpha@project-a, global-agent",
        hostname="test-host",
    )

    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "Machine: `test-host` (role: client, os: posix)." in context
    assert "Online peers: alpha@project-a, global-agent" in context
    assert "/imagent <name>" in context
    assert json.loads(json.dumps(payload)) == payload
