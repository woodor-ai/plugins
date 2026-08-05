"""Windows scheduled-task registration and privilege escalation."""

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
        description="test service",
        command=("C:\\Program Files\\am\\am-msgd.exe", "serve", "--config", "c"),
        macos_label="ai.woodor.test",
        windows_task_name="woodor-test",
        linux_unit_name="woodor-test.service",
        log_path=tmp_path / "test.log",
    )


def _result(command, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def _recorder(commands, outcomes=None):
    def run(command):
        commands.append(list(command))
        if outcomes:
            return outcomes.pop(0)(command)
        return _result(command)

    return run


def test_logon_trigger_is_scoped_to_the_installing_user(tmp_path):
    from agent_meeting.operating_systems import user_service

    definition = user_service.windows_definition(
        _spec(tmp_path),
        principal="S-1-5-21-0-0-0-1000",
    )

    # An unscoped logon trigger means "at logon of any user", which Windows
    # only lets an elevated caller register.
    assert definition.count("<UserId>S-1-5-21-0-0-0-1000</UserId>") == 2
    assert "<LogonType>InteractiveToken</LogonType>" in definition
    assert "<RunLevel>LeastPrivilege</RunLevel>" in definition


def test_definition_never_terminates_the_daemon_on_a_timer(tmp_path):
    from agent_meeting.operating_systems import user_service

    definition = user_service.windows_definition(
        _spec(tmp_path),
        principal="S-1-5-21-0-0-0-1000",
    )

    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in definition
    assert "<DisallowStartIfOnBatteries>false" in definition
    assert "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>" in definition
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in (
        definition
    )


def test_registration_writes_a_utf16_definition_file(tmp_path, monkeypatch):
    from agent_meeting.operating_systems import user_service

    seen = {}

    def run(command):
        path = Path(command[command.index("/XML") + 1])
        seen["exists"] = path.is_file()
        seen["text"] = path.read_text(encoding="utf-16")
        return _result(command)

    monkeypatch.setattr(user_service, "_run", run)

    user_service.ensure_installed(
        _spec(tmp_path),
        system_name="Windows",
        home=tmp_path,
    )

    assert seen["exists"] is True
    assert seen["text"].startswith('<?xml version="1.0" encoding="UTF-16"?>')
    assert (
        "<Command>C:\\Program Files\\am\\am-msgd.exe</Command>" in seen["text"]
    )


def test_registration_reclaims_a_task_left_by_an_elevated_install(
    tmp_path,
    monkeypatch,
):
    from agent_meeting.operating_systems import user_service

    commands = []
    elevated = []
    attempts = {"create": 0}

    def run(command):
        commands.append(list(command))
        if command[1] == "/Create":
            attempts["create"] += 1
            if attempts["create"] == 1:
                return _result(command, 1, stderr="ERROR: Access is denied.")
        return _result(command)

    monkeypatch.setattr(user_service, "_run", run)
    monkeypatch.setattr(user_service, "_is_elevated", lambda: False)
    monkeypatch.setattr(
        user_service,
        "_run_elevated",
        lambda sequence: elevated.append(sequence) or _result(sequence),
    )

    user_service.ensure_installed(
        _spec(tmp_path),
        system_name="Windows",
        home=tmp_path,
    )

    # One consent prompt removes the task, then it is registered again without
    # elevation so it ends up owned by this user.
    assert elevated == [
        [
            ["schtasks", "/End", "/TN", "woodor-test"],
            ["schtasks", "/Delete", "/TN", "woodor-test", "/F"],
        ]
    ]
    assert attempts["create"] == 2
    assert commands[-1][1] == "/Create"


def test_registration_reports_a_declined_consent_prompt(tmp_path, monkeypatch):
    from agent_meeting.operating_systems import user_service

    monkeypatch.setattr(
        user_service,
        "_run",
        lambda command: (
            _result(command)
            if command[1] == "/Query"
            else _result(command, 1, stderr="ERROR: Access is denied.")
        ),
    )
    monkeypatch.setattr(user_service, "_is_elevated", lambda: False)
    monkeypatch.setattr(
        user_service,
        "_run_elevated",
        lambda sequence: _result(
            sequence,
            user_service.ELEVATION_DECLINED_EXIT_CODE,
            stdout="administrator approval was declined",
        ),
    )

    with pytest.raises(RuntimeError) as error:
        user_service.ensure_installed(
            _spec(tmp_path),
            system_name="Windows",
            home=tmp_path,
        )

    assert "woodor-test" in str(error.value)
    assert "declined" in str(error.value)


def test_registration_never_prompts_for_an_unregistered_task(
    tmp_path,
    monkeypatch,
):
    from agent_meeting.operating_systems import user_service

    monkeypatch.setattr(
        user_service,
        "_run",
        lambda command: _result(command, 1, stderr="bad definition"),
    )
    monkeypatch.setattr(user_service, "_is_elevated", lambda: False)

    def refuse(sequence):
        raise AssertionError("must not ask for elevation")

    monkeypatch.setattr(user_service, "_run_elevated", refuse)

    with pytest.raises(RuntimeError, match="bad definition"):
        user_service.ensure_installed(
            _spec(tmp_path),
            system_name="Windows",
            home=tmp_path,
        )


def test_elevation_can_be_disabled_for_unattended_runs(tmp_path, monkeypatch):
    from agent_meeting.operating_systems import user_service

    monkeypatch.setenv(
        user_service.ELEVATION_DISABLED_ENVIRONMENT_VARIABLE,
        "1",
    )
    monkeypatch.setattr(
        user_service,
        "_run",
        lambda command: _result(command, 1, stderr="ERROR: Access is denied."),
    )
    monkeypatch.setattr(user_service, "_is_elevated", lambda: False)

    def refuse(sequence):
        raise AssertionError("must not ask for elevation")

    monkeypatch.setattr(user_service, "_run_elevated", refuse)

    with pytest.raises(RuntimeError, match="Access is denied"):
        user_service.ensure_installed(
            _spec(tmp_path),
            system_name="Windows",
            home=tmp_path,
        )


def test_start_escalates_a_refused_run(tmp_path, monkeypatch):
    from agent_meeting.operating_systems import user_service

    elevated = []

    def run(command):
        if command[1] == "/Run":
            return _result(command, 1, stderr="ERROR: Access is denied.")
        return _result(command)

    monkeypatch.setattr(user_service, "_run", run)
    monkeypatch.setattr(user_service, "_is_elevated", lambda: False)
    monkeypatch.setattr(
        user_service,
        "_run_elevated",
        lambda sequence: elevated.append(sequence) or _result(sequence),
    )

    user_service.start(_spec(tmp_path), system_name="Windows", home=tmp_path)

    assert elevated == [[["schtasks", "/Run", "/TN", "woodor-test"]]]


def test_elevated_transcript_survives_the_shell_that_wrote_it():
    from agent_meeting.operating_systems import user_service

    # Windows PowerShell writes UTF-16 transcripts, PowerShell 7 writes UTF-8.
    utf16 = "ERROR: Access is denied.".encode("utf-16")
    assert "Access is denied." in user_service._decode(utf16)
    assert "Access is denied." in user_service._decode(b"ERROR: Access is denied.")


def test_elevated_transcript_drops_the_powershell_error_frame():
    from agent_meeting.operating_systems import user_service

    transcript = (
        "schtasks.exe : ERROR: Access is denied.\n"
        "At C:\\x\\run.ps1:3 char:1\n"
        "+ & 'schtasks' '/Delete' ...\n"
        "+ ~~~~~~~~~~~~~~~~~~~~~~~\n"
        "    + CategoryInfo          : NotSpecified: (:String) [], RemoteException\n"
        "    + FullyQualifiedErrorId : NativeCommandError\n"
    )

    assert user_service._readable(transcript) == (
        "schtasks.exe : ERROR: Access is denied."
    )


def test_elevated_script_runs_every_command_in_one_prompt(tmp_path):
    from agent_meeting.operating_systems import user_service

    script = user_service._elevated_script(
        [
            ["schtasks", "/End", "/TN", "woodor-test"],
            ["schtasks", "/Delete", "/TN", "it's-quoted", "/F"],
        ],
        tmp_path / "out.log",
    )

    assert script.count("Start-Process") == 0
    assert "& 'schtasks' '/End' '/TN' 'woodor-test'" in script
    assert "'it''s-quoted'" in script
    assert script.rstrip().endswith("exit $LASTEXITCODE")
