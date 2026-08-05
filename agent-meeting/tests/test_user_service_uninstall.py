from pathlib import Path
import subprocess

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def add_source_root(monkeypatch):
    monkeypatch.syspath_prepend(str(PLUGIN_ROOT / "src"))


def _spec(tmp_path):
    from agent_meeting.operating_systems.user_service import UserServiceSpec

    return UserServiceSpec(
        description="test",
        command=("test-command",),
        macos_label="ai.woodor.test",
        windows_task_name="woodor-test",
        linux_unit_name="woodor-test.service",
        log_path=tmp_path / "test.log",
    )


def _success(command):
    return subprocess.CompletedProcess(command, 0, "", "")


def test_macos_uninstall_boots_out_and_deletes_plist(tmp_path, monkeypatch):
    from agent_meeting.operating_systems import user_service

    spec = _spec(tmp_path)
    plist = user_service.macos_plist_path(spec, home=tmp_path)
    plist.parent.mkdir(parents=True)
    plist.write_bytes(user_service.macos_definition(spec))
    commands = []
    monkeypatch.setattr(
        user_service,
        "_run",
        lambda command: commands.append(command) or _success(command),
    )

    user_service.uninstall(spec, system_name="Darwin", home=tmp_path)

    assert commands[0][0:2] == ["launchctl", "bootout"]
    assert commands[1][0:2] == ["launchctl", "enable"]
    assert not plist.exists()


def test_linux_uninstall_disables_and_deletes_unit(tmp_path, monkeypatch):
    from agent_meeting.operating_systems import user_service

    spec = _spec(tmp_path)
    unit = user_service.linux_unit_path(spec, home=tmp_path)
    unit.parent.mkdir(parents=True)
    unit.write_text(user_service.linux_definition(spec), encoding="utf-8")
    commands = []
    monkeypatch.setattr(
        user_service,
        "_run",
        lambda command: commands.append(command) or _success(command),
    )

    user_service.uninstall(spec, system_name="Linux", home=tmp_path)

    assert commands == [
        ["systemctl", "--user", "disable", "--now", "woodor-test.service"],
        ["systemctl", "--user", "daemon-reload"],
    ]
    assert not unit.exists()


def test_windows_uninstall_ends_and_deletes_task(tmp_path, monkeypatch):
    from agent_meeting.operating_systems import user_service

    spec = _spec(tmp_path)
    commands = []
    monkeypatch.setattr(
        user_service,
        "_run",
        lambda command: commands.append(command) or _success(command),
    )

    user_service.uninstall(spec, system_name="Windows", home=tmp_path)

    assert commands == [
        ["schtasks", "/End", "/TN", "woodor-test"],
        ["schtasks", "/Delete", "/TN", "woodor-test", "/F"],
    ]
