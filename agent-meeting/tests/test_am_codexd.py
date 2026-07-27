import importlib.machinery
import importlib.util
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMMAND_PATH = ROOT / "bin" / "am-codexd"


def load_command(name):
    loader = importlib.machinery.SourceFileLoader(name, str(COMMAND_PATH))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_help_lists_the_public_lifecycle_commands():
    result = subprocess.run(
        [sys.executable, str(COMMAND_PATH), "--help"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "{status,start,stop,restart,update}" in result.stdout
    assert "_serve" not in result.stdout


def test_update_restarts_an_idle_outdated_daemon(monkeypatch):
    module = load_command("am_codexd_idle_update")
    actions = []

    monkeypatch.setattr(module, "installed_version", lambda: "0.14.0")
    monkeypatch.setattr(
        module,
        "status_info",
        lambda: {"ok": True, "version": "0.13.9", "sessions": 0},
    )
    monkeypatch.setattr(module, "stop", lambda: actions.append("stop"))
    monkeypatch.setattr(module, "start", lambda: actions.append("start"))

    module.update()

    assert actions == ["stop", "start"]


def test_update_waits_for_a_same_version_daemon_that_is_still_starting(monkeypatch):
    module = load_command("am_codexd_starting_update")
    statuses = iter(
        [
            {"ok": False, "version": "0.14.0", "sessions": 0},
            {"ok": True, "version": "0.14.0", "sessions": 0},
        ]
    )
    actions = []

    monkeypatch.setattr(module, "installed_version", lambda: "0.14.0")
    monkeypatch.setattr(module, "status_info", lambda: next(statuses))
    monkeypatch.setattr(
        module,
        "wait_until",
        lambda predicate, timeout=25: predicate(),
    )
    monkeypatch.setattr(module, "stop", lambda: actions.append("stop"))
    monkeypatch.setattr(module, "start", lambda: actions.append("start"))

    module.update()

    assert actions == []


def test_concurrent_start_accepts_the_other_healthy_daemon(monkeypatch):
    module = load_command("am_codexd_concurrent_start")
    statuses = iter(
        [
            {},
            {"ok": True, "version": "0.14.0", "sessions": 0, "pid": 4321},
            {"ok": True, "version": "0.14.0", "sessions": 0, "pid": 4321},
        ]
    )

    class LostRaceProcess:
        @staticmethod
        def poll():
            return 1

    monkeypatch.setattr(module, "installed_version", lambda: "0.14.0")
    monkeypatch.setattr(module, "status_info", lambda: next(statuses))
    monkeypatch.setattr(module, "spawn_daemon", lambda: LostRaceProcess())
    monkeypatch.setattr(
        module,
        "wait_until",
        lambda predicate, timeout=25: predicate(),
    )

    module.start()


def test_concurrent_start_rejects_a_winner_from_another_version(monkeypatch):
    module = load_command("am_codexd_wrong_version_race")
    statuses = iter(
        [
            {},
            {"ok": True, "version": "0.13.9", "sessions": 0, "pid": 4321},
            {"ok": True, "version": "0.13.9", "sessions": 0, "pid": 4321},
        ]
    )

    class LostRaceProcess:
        @staticmethod
        def poll():
            return 1

    monkeypatch.setattr(module, "installed_version", lambda: "0.14.0")
    monkeypatch.setattr(module, "status_info", lambda: next(statuses))
    monkeypatch.setattr(module, "spawn_daemon", lambda: LostRaceProcess())
    monkeypatch.setattr(
        module,
        "wait_until",
        lambda predicate, timeout=25: predicate(),
    )

    try:
        module.start()
    except RuntimeError as exc:
        assert "0.13.9 won the startup race" in str(exc)
    else:
        raise AssertionError("start accepted a daemon from the wrong version")


def test_update_refuses_to_interrupt_active_sessions(monkeypatch):
    module = load_command("am_codexd_active_update")

    monkeypatch.setattr(module, "installed_version", lambda: "0.14.0")
    monkeypatch.setattr(
        module,
        "status_info",
        lambda: {"ok": True, "version": "0.13.9", "sessions": 3},
    )

    assert module.main(["update"]) == 1


def test_status_reports_running_and_installed_versions(monkeypatch, capsys):
    module = load_command("am_codexd_status")

    monkeypatch.setattr(module, "installed_version", lambda: "0.14.0")
    monkeypatch.setattr(
        module,
        "status_info",
        lambda: {
            "ok": True,
            "pid": 1234,
            "version": "0.13.9",
            "sessions": 2,
        },
    )

    assert module.main(["status"]) == 0
    output = capsys.readouterr().out
    assert "status: running" in output
    assert "pid: 1234" in output
    assert "running version: 0.13.9" in output
    assert "installed version: 0.14.0" in output
    assert "active sessions: 2" in output
