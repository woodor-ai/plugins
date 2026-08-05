import json
import os
import plistlib
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PLUGIN_ROOT / "src"
AM_MSGD = SRC_ROOT / "agent_meeting" / "commands" / "am_msgd_cli.py"


@pytest.fixture(autouse=True)
def add_source_root(monkeypatch):
    monkeypatch.syspath_prepend(str(SRC_ROOT))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _health(address: str, port: int):
    host = f"[{address}]" if ":" in address else address
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://{host}:{port}/health", timeout=2) as response:
        return json.loads(response.read().decode("utf-8"))


def _wait_for_health(port: int) -> dict:
    deadline = time.time() + 8
    while time.time() < deadline:
        try:
            return _health("127.0.0.1", port)
        except Exception:
            time.sleep(0.05)
    raise AssertionError("am-msgd did not become healthy")


def test_message_hub_uses_runtime_package_version():
    import agent_meeting
    from agent_meeting.message_hub import message_hub_process

    assert message_hub_process._plugin_version == agent_meeting.__version__


def test_service_configuration_defaults_to_loopback(tmp_path):
    from agent_meeting.message_hub import service_configuration

    path = tmp_path / "am-msgd.json"
    configuration = service_configuration.load(path, create=True)

    assert configuration.enabled is True
    assert configuration.port == 8765
    assert configuration.binds == ("127.0.0.1",)
    assert json.loads(path.read_text())["binds"] == ["127.0.0.1"]


def test_legacy_foreground_flags_are_rejected():
    from agent_meeting.commands import am_msgd_cli

    with pytest.raises(SystemExit):
        am_msgd_cli.build_parser().parse_args(
            ["--port", "8765", "--no-mdns"]
        )


def test_wildcard_cannot_replace_active_specific_ipv4():
    from agent_meeting.message_hub import service_configuration

    configuration = service_configuration.MessageHubServiceConfiguration()

    with pytest.raises(ValueError, match="cannot add 0.0.0.0"):
        service_configuration.with_added_bind(
            configuration,
            "0.0.0.0",
        )


def test_failed_dynamic_bind_keeps_existing_listener_and_configuration(
    tmp_path,
    monkeypatch,
):
    from agent_meeting.message_hub import listener_manager
    from agent_meeting.message_hub import service_configuration

    configuration_path = tmp_path / "am-msgd.json"
    configuration = service_configuration.MessageHubServiceConfiguration()
    service_configuration.write(configuration_path, configuration)
    manager = listener_manager.ListenerManager(
        handler_class=object,
        configuration_path=configuration_path,
        configuration=configuration,
        plugin_version="test",
        publish_mdns=lambda *_args: (None, None),
    )
    existing_server = object()
    existing_thread = object()
    manager._listeners["127.0.0.1"] = (  # noqa: SLF001
        existing_server,
        existing_thread,
    )

    def fail_new_listener(_address):
        raise OSError("address unavailable")

    monkeypatch.setattr(manager, "_start_listener", fail_new_listener)

    with pytest.raises(ValueError, match="cannot bind"):
        manager.add("192.0.2.1")

    assert manager._listeners["127.0.0.1"] == (  # noqa: SLF001
        existing_server,
        existing_thread,
    )
    assert service_configuration.load(configuration_path).binds == (
        "127.0.0.1",
    )


def test_loopback_listener_cannot_be_removed():
    from agent_meeting.message_hub import service_configuration

    configuration = service_configuration.MessageHubServiceConfiguration()

    with pytest.raises(ValueError, match="127.0.0.1"):
        service_configuration.with_removed_bind(
            configuration,
            "127.0.0.1",
        )


def test_ipv6_loopback_cannot_replace_ipv4_admin_listener():
    from agent_meeting.message_hub import service_configuration

    configuration = service_configuration.MessageHubServiceConfiguration(
        binds=("127.0.0.1", "::1"),
    )

    with pytest.raises(ValueError, match="127.0.0.1"):
        service_configuration.with_removed_bind(
            configuration,
            "127.0.0.1",
        )


def test_legacy_ipv4_wildcard_still_provides_local_admin_access(tmp_path):
    from agent_meeting.message_hub import service_configuration

    path = tmp_path / "am-msgd.json"
    service_configuration.write(
        path,
        service_configuration.MessageHubServiceConfiguration(
            binds=("0.0.0.0",),
        ),
    )

    assert service_configuration.load(path).binds == ("0.0.0.0",)


@pytest.mark.skipif(
    not socket.has_ipv6,
    reason="dynamic second loopback listener requires IPv6",
)
def test_dynamic_bind_keeps_pid_and_instance(tmp_path):
    from agent_meeting.message_hub import service_configuration

    meeting_home = tmp_path / "home"
    database_dir = meeting_home / "db"
    database_dir.mkdir(parents=True)
    sqlite3.connect(database_dir / "rooms.db").close()
    port = _free_port()
    configuration_path = meeting_home / "am-msgd.json"
    service_configuration.write(
        configuration_path,
        service_configuration.MessageHubServiceConfiguration(
            port=port,
        ),
    )
    environment = os.environ.copy()
    environment["MEETING_HOME"] = str(meeting_home)
    environment["PYTHONPATH"] = str(SRC_ROOT)
    process = subprocess.Popen(
        [
            sys.executable,
            str(AM_MSGD),
            "serve",
            "--config",
            str(configuration_path),
            "--no-mdns",
        ],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        before = _wait_for_health(port)
        result = subprocess.run(
            [
                sys.executable,
                str(AM_MSGD),
                "--bind",
                "::1",
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        after = _health("::1", port)
        assert process.pid > 0
        assert after["instance_id"] == before["instance_id"]
        assert f"127.0.0.1:{port}" in after["active_listeners"]
        assert f"[::1]:{port}" in after["active_listeners"]
        saved = service_configuration.load(configuration_path)
        assert saved.binds == ("127.0.0.1", "::1")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def test_agent_list_maps_project_to_proj(tmp_path, monkeypatch, capsys):
    from agent_meeting.commands import am_msgd_cli
    from agent_meeting.message_hub import service_configuration

    service_configuration.write(
        tmp_path / "am-msgd.json",
        service_configuration.MessageHubServiceConfiguration(),
    )
    monkeypatch.setattr(
        am_msgd_cli,
        "_request_json",
        lambda *args, **kwargs: [
            {"name": "z", "project": "two", "status": "historical"},
            {"name": "a", "project": "one", "status": "online"},
            {"name": "b", "project": "one", "status": "empty"},
        ],
    )

    result = am_msgd_cli._agent_list(
        type("Args", (), {"json": True})(),
        tmp_path,
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out) == [
        {"name": "a", "proj": "one", "status": "online"},
        {"name": "b", "proj": "one", "status": "empty"},
        {"name": "z", "proj": "two", "status": "historical"},
    ]


def test_status_reports_business_authentication(tmp_path, monkeypatch, capsys):
    from agent_meeting.commands import am_msgd_cli
    from agent_meeting.message_hub import service_configuration

    service_configuration.write(
        tmp_path / "am-msgd.json",
        service_configuration.MessageHubServiceConfiguration(),
    )
    (tmp_path / "config.json").write_text(
        json.dumps({"auth_token": "secret"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(am_msgd_cli, "_health", lambda _config: None)
    monkeypatch.setattr(
        am_msgd_cli.message_hub_user_service,
        "service_state",
        lambda _home: "inactive",
    )

    result = am_msgd_cli._status(
        type("Args", (), {"json": True})(),
        tmp_path,
    )

    assert result == 1
    assert json.loads(capsys.readouterr().out)["authentication"] == "enabled"


def test_status_reports_connected_agent_addresses(
    tmp_path,
    monkeypatch,
    capsys,
):
    from agent_meeting.commands import am_msgd_cli
    from agent_meeting.message_hub import service_configuration

    service_configuration.write(
        tmp_path / "am-msgd.json",
        service_configuration.MessageHubServiceConfiguration(),
    )
    monkeypatch.setattr(
        am_msgd_cli,
        "_health",
        lambda _config: {
            "version": "test",
            "instance_id": "instance",
            "active_listeners": ["127.0.0.1:8765"],
            "listener_errors": {},
            "mdns": "off",
        },
    )
    monkeypatch.setattr(
        am_msgd_cli,
        "_request_json",
        lambda *args, **kwargs: [
            {
                "name": "offline",
                "project": "old",
                "status": "empty",
                "host": "old-host",
            },
            {
                "name": "bob",
                "project": "tools",
                "status": "online",
                "host": "10.0.0.8",
            },
            {
                "name": "alice",
                "project": "apps",
                "status": "online",
                "host": "client.local",
            },
        ],
    )
    monkeypatch.setattr(
        am_msgd_cli.message_hub_user_service,
        "service_state",
        lambda _home: "running",
    )

    result = am_msgd_cli._status(
        type("Args", (), {"json": False})(),
        tmp_path,
    )

    assert result == 0
    output = capsys.readouterr().out
    assert "version:    test" in output
    assert "agents:     2 connected" in output
    assert "ADDRESS\tNAME\tPROJ" in output
    assert "client.local\talice\tapps" in output
    assert "10.0.0.8\tbob\ttools" in output
    assert "offline" not in output


def test_lifecycle_updates_enabled_before_service_call(
    tmp_path,
    monkeypatch,
):
    from agent_meeting.commands import am_msgd_cli
    from agent_meeting.message_hub import service_configuration

    calls = []
    monkeypatch.setattr(
        am_msgd_cli.message_hub_user_service,
        "stop",
        lambda meeting_home: calls.append(meeting_home),
    )

    assert am_msgd_cli._run_lifecycle("stop", tmp_path) == 0
    assert calls == [tmp_path]
    assert service_configuration.load(
        tmp_path / "am-msgd.json"
    ).enabled is False


def test_macos_service_definition_uses_explicit_serve(tmp_path):
    from agent_meeting.operating_systems import message_hub_user_service

    payload = plistlib.loads(
        message_hub_user_service._macos_definition(
            tmp_path,
            tmp_path / "am-msgd.json",
        )
    )

    assert payload["ProgramArguments"] == [
        str(tmp_path / "bin" / "am-msgd"),
        "serve",
        "--config",
        str(tmp_path / "am-msgd.json"),
    ]
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True


def test_linux_service_definition_uses_explicit_serve(
    tmp_path,
    monkeypatch,
):
    from agent_meeting.operating_systems import (
        message_hub_user_service,
        user_service,
    )

    unit_path = tmp_path / "agent-meeting-am-msgd.service"
    commands = []
    monkeypatch.setattr(
        user_service,
        "linux_unit_path",
        lambda *_args, **_kwargs: unit_path,
    )
    monkeypatch.setattr(
        user_service,
        "_run",
        lambda command: (
            commands.append(command)
            or subprocess.CompletedProcess(command, 0, "", "")
        ),
    )

    message_hub_user_service.ensure_installed(
        tmp_path,
        tmp_path / "am-msgd.json",
        system_name="Linux",
    )

    text = unit_path.read_text(encoding="utf-8")
    assert (
        f"ExecStart={tmp_path / 'bin' / 'am-msgd'} serve "
        f"--config {tmp_path / 'am-msgd.json'}"
    ) in text
    assert commands == [["systemctl", "--user", "daemon-reload"]]


def test_windows_service_uses_console_free_entrypoint_and_log(
    tmp_path,
    monkeypatch,
):
    from agent_meeting.operating_systems import (
        message_hub_user_service,
        user_service,
    )

    commands = []
    monkeypatch.setattr(message_hub_user_service.sys, "platform", "win32")
    monkeypatch.setattr(
        user_service,
        "_run",
        lambda command: (
            commands.append(command)
            or subprocess.CompletedProcess(command, 0, "", "")
        ),
    )

    meeting_home = tmp_path / "custom meeting"
    runtime_command = (
        meeting_home
        / "runtimes"
        / "0.18.23"
        / "venv"
        / "Scripts"
        / "am-msgd-service.exe"
    )
    runtime_command.parent.mkdir(parents=True)
    runtime_command.write_bytes(b"launcher")
    (meeting_home / "active-runtime.json").write_text(
        json.dumps(
            {"commands": {"am-msgd-service": str(runtime_command)}}
        ),
        encoding="utf-8",
    )
    message_hub_user_service.ensure_installed(
        meeting_home,
        meeting_home / "am-msgd.json",
        system_name="Windows",
    )

    assert commands[0][:4] == [
        "schtasks",
        "/Create",
        "/TN",
        "agent-meeting-am-msgd",
    ]
    assert commands[0][4] == "/XML"
    definition = user_service.windows_definition(
        message_hub_user_service._spec(
            meeting_home,
            meeting_home / "am-msgd.json",
        ),
        principal="S-1-5-21-0-0-0-1000",
    )
    assert f"<Command>{runtime_command}</Command>" in definition
    arguments = definition.split("<Arguments>")[1].split("</Arguments>")[0]
    assert (
        f'--service-log "{meeting_home / "logs" / "am-msgd.log"}"' in arguments
    )
    assert f'--config "{meeting_home / "am-msgd.json"}"' in arguments


def test_installation_migrates_legacy_host_and_stop_state(tmp_path):
    from agent_meeting.installation import message_hub_service_installation

    (tmp_path / "config.json").write_text(
        json.dumps({"is_host": True}),
        encoding="utf-8",
    )
    (tmp_path / "am-msgd.stopped").write_text("stopped", encoding="utf-8")

    configuration = (
        message_hub_service_installation.ensure_configuration(tmp_path)
    )

    assert configuration.binds == ("0.0.0.0",)
    assert configuration.enabled is False
