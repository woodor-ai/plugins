import json
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PLUGIN_ROOT / "src"


@pytest.fixture(autouse=True)
def add_source_root(monkeypatch):
    monkeypatch.syspath_prepend(str(SRC_ROOT))


def _fake_runtime(
    meeting_home: Path,
    version: str,
    *,
    is_windows: bool,
) -> Path:
    from agent_meeting.installation.version_activation import runtime_commands

    runtime = meeting_home / "runtimes" / version
    command_dir = (
        runtime / "venv" / "Scripts"
        if is_windows
        else runtime / "venv" / "bin"
    )
    command_dir.mkdir(parents=True)
    for command in runtime_commands(is_windows=is_windows):
        path = command_dir / (f"{command}.exe" if is_windows else command)
        path.write_bytes(f"{version}:{command}".encode())
    return runtime


def test_posix_activation_uses_stable_symlinks_and_atomic_manifest(tmp_path):
    from agent_meeting.installation.version_activation import activate_runtime

    first = _fake_runtime(tmp_path, "0.15.0", is_windows=False)
    second = _fake_runtime(tmp_path, "0.16.0", is_windows=False)
    legacy_command = tmp_path / "bin" / "meeting"
    legacy_command.parent.mkdir(parents=True)
    legacy_command.write_text("legacy", encoding="utf-8")
    obsolete_alias = tmp_path / "bin" / "mycodex"
    obsolete_alias.write_text("obsolete", encoding="utf-8")
    obsolete_link = tmp_path / "bin" / "lnk"
    obsolete_link.write_text("obsolete", encoding="utf-8")
    obsolete_runtime = tmp_path / "bin" / "session-bootstrap.py"
    obsolete_runtime.write_text("obsolete", encoding="utf-8")
    obsolete_cache = tmp_path / "bin" / "__pycache__"
    obsolete_cache.mkdir()
    (obsolete_cache / "session-bootstrap.pyc").write_bytes(b"obsolete")
    obsolete_configure = (
        tmp_path / "bin" / "am-configure-codex-user-environment"
    )
    obsolete_configure.write_text("obsolete", encoding="utf-8")

    activate_runtime(
        meeting_home=tmp_path,
        version="0.15.0",
        is_windows=False,
    )
    meeting_link = tmp_path / "bin" / "am"
    assert meeting_link.is_symlink()
    assert meeting_link.resolve() == first / "venv" / "bin" / "am"

    payload = activate_runtime(
        meeting_home=tmp_path,
        version="0.16.0",
        is_windows=False,
    )
    assert meeting_link.resolve() == second / "venv" / "bin" / "am"
    assert json.loads(
        (tmp_path / "active-runtime.json").read_text(encoding="utf-8")
    ) == payload
    assert not list(tmp_path.glob(".active-runtime.json.tmp.*"))
    assert not legacy_command.exists()
    assert not obsolete_alias.exists()
    assert not obsolete_link.exists()
    assert not obsolete_runtime.exists()
    assert not obsolete_cache.exists()
    assert not obsolete_configure.exists()


def test_windows_activation_copies_console_exes_without_cmd_forwarders(
    tmp_path,
):
    from agent_meeting.installation.version_activation import (
        RUNTIME_COMMANDS,
        activate_runtime,
    )

    runtime = _fake_runtime(tmp_path, "0.15.0", is_windows=True)
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir(parents=True)
    for command in RUNTIME_COMMANDS:
        (bin_directory / command).write_text("legacy", encoding="utf-8")
        (bin_directory / f"{command}.cmd").write_text(
            "legacy",
            encoding="utf-8",
        )
    legacy_command = tmp_path / "bin" / "meeting.exe"
    legacy_command.write_bytes(b"legacy")
    (bin_directory / "meeting").write_text("legacy", encoding="utf-8")
    (bin_directory / "meeting.cmd").write_text("legacy", encoding="utf-8")
    obsolete_alias = tmp_path / "bin" / "mycodex.exe"
    obsolete_alias.write_bytes(b"obsolete")
    (bin_directory / "mycodex").write_text("obsolete", encoding="utf-8")
    (bin_directory / "mycodex.cmd").write_text(
        "obsolete",
        encoding="utf-8",
    )
    obsolete_link = tmp_path / "bin" / "lnk.exe"
    obsolete_link.write_bytes(b"obsolete")
    (bin_directory / "lnk").write_text("obsolete", encoding="utf-8")
    (bin_directory / "lnk.cmd").write_text("obsolete", encoding="utf-8")
    obsolete_configure = (
        tmp_path / "bin" / "am-configure-codex-user-environment.exe"
    )
    obsolete_configure.write_bytes(b"obsolete")
    activate_runtime(
        meeting_home=tmp_path,
        version="0.15.0",
        is_windows=True,
    )

    for command in (
        "am",
        "am-ctl",
        "am-msgd",
        "amclaude",
        "amcodex",
        "am-codexd",
    ):
        destination = tmp_path / "bin" / f"{command}.exe"
        assert destination.read_bytes() == (
            runtime / "venv" / "Scripts" / f"{command}.exe"
        ).read_bytes()
    for command in RUNTIME_COMMANDS:
        assert not (tmp_path / "bin" / command).exists()
        assert not (tmp_path / "bin" / f"{command}.cmd").exists()
    assert not (tmp_path / "bin" / "am-msgd-service.exe").exists()
    assert not (tmp_path / "bin" / "am-ctld-service.exe").exists()
    assert not legacy_command.exists()
    assert not obsolete_alias.exists()
    assert not obsolete_link.exists()
    assert not obsolete_configure.exists()


def test_windows_activation_defers_locked_stable_launcher(
    tmp_path,
    monkeypatch,
):
    from agent_meeting.installation import version_activation

    runtime = _fake_runtime(tmp_path, "0.18.21", is_windows=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    locked = bin_dir / "am-update.exe"
    locked.write_bytes(b"running")
    real_replace = version_activation.os.replace
    scheduled = []

    def replace(source, destination):
        if Path(destination) == locked:
            raise PermissionError("locked")
        return real_replace(source, destination)

    monkeypatch.setattr(version_activation.os, "replace", replace)

    payload = version_activation.activate_runtime(
        meeting_home=tmp_path,
        version="0.18.21",
        is_windows=True,
        schedule_windows_replacements=lambda **kwargs: (
            scheduled.append(kwargs) or tmp_path / "pending.json"
        ),
    )

    assert payload["version"] == "0.18.21"
    assert locked.read_bytes() == b"running"
    assert len(scheduled) == 1
    replacement = scheduled[0]["replacements"]
    assert replacement == [
        (
            next(bin_dir.glob(".am-update.exe.tmp.*")),
            locked,
        )
    ]
    assert replacement[0][0].read_bytes() == (
        runtime / "venv" / "Scripts" / "am-update.exe"
    ).read_bytes()


def test_activation_refuses_incomplete_runtime(tmp_path):
    from agent_meeting.installation.version_activation import activate_runtime

    runtime = tmp_path / "runtimes" / "0.15.0" / "venv" / "bin"
    runtime.mkdir(parents=True)
    (runtime / "am").write_text("am")

    with pytest.raises(FileNotFoundError, match="missing public command"):
        activate_runtime(
            meeting_home=tmp_path,
            version="0.15.0",
            is_windows=False,
        )

    assert not (tmp_path / "active-runtime.json").exists()


def test_windows_legacy_service_launchers_are_removed_after_task_migration(
    tmp_path,
):
    from agent_meeting.installation.version_activation import (
        remove_legacy_windows_service_launchers,
    )

    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    for command in ("am-msgd-service", "am-ctld-service"):
        (bin_directory / f"{command}.exe").write_bytes(b"legacy")

    remove_legacy_windows_service_launchers(tmp_path)

    assert not list(bin_directory.glob("*-service.exe"))


def test_activation_refuses_runtime_with_installing_marker(tmp_path):
    from agent_meeting.installation.version_activation import activate_runtime

    runtime = _fake_runtime(tmp_path, "0.15.0", is_windows=False)
    (runtime / ".installing").write_text("pid=1\n")

    with pytest.raises(RuntimeError, match="installation is incomplete"):
        activate_runtime(
            meeting_home=tmp_path,
            version="0.15.0",
            is_windows=False,
        )
