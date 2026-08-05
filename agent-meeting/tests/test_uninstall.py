import json
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def add_source_roots(monkeypatch):
    monkeypatch.syspath_prepend(
        str(REPOSITORY_ROOT / "agent-meeting" / "src")
    )
    monkeypatch.syspath_prepend(str(REPOSITORY_ROOT / "mycodex" / "src"))


def _manifest(meeting_home: Path, targets=None):
    from agent_meeting.installation.install_manifest import record_installation

    return record_installation(
        meeting_home,
        version="0.18.23",
        targets=set(targets or {"codex"}),
    )


def test_install_manifest_merges_targets_and_validates_ownership(tmp_path):
    from agent_meeting.installation import install_manifest

    first = _manifest(tmp_path, {"codex"})
    second = install_manifest.record_installation(
        tmp_path,
        version="0.18.23",
        targets={"claude-code"},
    )

    assert first["targets"] == ["codex"]
    assert second["targets"] == ["claude-code", "codex"]
    assert install_manifest.read_manifest(tmp_path) == second
    payload = json.loads(
        (tmp_path / "install-manifest.json").read_text(encoding="utf-8")
    )
    payload["meeting_home"] = str(tmp_path / "other")
    (tmp_path / "install-manifest.json").write_text(json.dumps(payload))
    with pytest.raises(RuntimeError, match="does not own"):
        install_manifest.read_manifest(tmp_path)


def test_cleanup_deletes_only_manifest_owned_directory(tmp_path):
    from agent_meeting.installation import uninstall_cleanup

    meeting_home = tmp_path / "meeting"
    _manifest(meeting_home)
    (meeting_home / "db").mkdir()
    (meeting_home / "db" / "rooms.db").write_bytes(b"test")

    uninstall_cleanup.delete_installation(meeting_home, retry_seconds=0)

    assert not meeting_home.exists()
    assert tmp_path.exists()


def test_cleanup_refuses_home_and_mismatched_manifest(tmp_path, monkeypatch):
    from agent_meeting.installation import uninstall_cleanup

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    with pytest.raises(RuntimeError, match="unsafe uninstall target"):
        uninstall_cleanup.validate_target(tmp_path)

    target = tmp_path / "meeting"
    target.mkdir()
    (target / "install-manifest.json").write_text(
        json.dumps({"meeting_home": str(tmp_path / "other")}),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="does not own"):
        uninstall_cleanup.validate_target(target)


def test_uninstall_dry_run_makes_no_changes(tmp_path, monkeypatch, capsys):
    from agent_meeting.installation import uninstall

    _manifest(tmp_path, {"claude-code", "codex"})
    monkeypatch.setattr(
        uninstall.uninstall_cleanup,
        "schedule_cleanup",
        lambda _home: pytest.fail("dry-run scheduled cleanup"),
    )

    assert uninstall.run(tmp_path, dry_run=True) == 0
    output = capsys.readouterr().out
    assert "permanently delete runtime, config, logs, and messages" in output
    assert "dry run: nothing was changed" in output
    assert tmp_path.exists()


def test_uninstall_refuses_active_codex_sessions(tmp_path, monkeypatch):
    from agent_meeting.installation import uninstall

    _manifest(tmp_path)
    monkeypatch.setattr(
        uninstall,
        "_codex_daemon_info",
        lambda: {"sessions": 2},
    )
    monkeypatch.setattr(
        uninstall.uninstall_cleanup,
        "schedule_cleanup",
        lambda _home: pytest.fail("active-session uninstall continued"),
    )

    with pytest.raises(RuntimeError, match="2 amcodex session"):
        uninstall.run(tmp_path, assume_yes=True)


def test_uninstall_removes_owned_components_and_schedules_cleanup(
    tmp_path,
    monkeypatch,
):
    from agent_meeting.installation import uninstall

    _manifest(tmp_path, {"claude-code", "codex"})
    events = []
    monkeypatch.setattr(uninstall, "_codex_daemon_info", lambda: {})
    monkeypatch.setattr(
        uninstall,
        "_stop_codex_daemon",
        lambda: events.append("daemon"),
    )
    monkeypatch.setattr(
        uninstall.message_hub_user_service,
        "uninstall",
        lambda *_args, **_kwargs: events.append("msgd"),
    )
    monkeypatch.setattr(
        uninstall.lifecycle_user_service,
        "uninstall_lifecycle_control_service",
        lambda *_args, **_kwargs: events.append("ctld"),
    )
    monkeypatch.setattr(
        uninstall,
        "_remove_plugin",
        lambda target: events.append(target),
    )
    monkeypatch.setattr(
        uninstall,
        "_remove_path_entry",
        lambda _home: events.append("path"),
    )
    monkeypatch.setattr(
        uninstall,
        "_remove_codex_skills",
        lambda: events.append("codex-skills"),
    )
    monkeypatch.setattr(
        uninstall,
        "_remove_claude_integration",
        lambda _home: events.append("claude-integration"),
    )
    monkeypatch.setattr(
        uninstall.uninstall_cleanup,
        "schedule_cleanup",
        lambda _home: events.append("cleanup"),
    )

    assert uninstall.run(tmp_path, assume_yes=True) == 0
    assert events == [
        "daemon",
        "msgd",
        "ctld",
        "claude-code",
        "codex",
        "claude-integration",
        "codex-skills",
        "path",
        "cleanup",
    ]


def test_uninstall_removes_only_agent_meeting_owned_codex_skills(
    tmp_path,
    monkeypatch,
):
    from agent_meeting.installation import uninstall

    codex_home = tmp_path / "codex"
    owned = codex_home / "skills" / "imagent"
    unowned = codex_home / "skills" / "talkto"
    owned.mkdir(parents=True)
    unowned.mkdir(parents=True)
    (owned / uninstall.CODEX_SKILL_OWNER_FILE).write_text(
        json.dumps({"product": "agent-meeting"}),
        encoding="utf-8",
    )
    (unowned / "SKILL.md").write_text("user content", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    uninstall._remove_codex_skills()

    assert not owned.exists()
    assert unowned.is_dir()


def test_posix_path_removal_preserves_unrelated_shell_configuration(
    tmp_path,
    monkeypatch,
):
    from mycodex.operating_systems.macos import shell_command_path

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/zsh")
    bin_directory = tmp_path / ".agent-meeting" / "bin"
    shell_command_path.ensure_command_directory(bin_directory)
    rc_path = tmp_path / ".zshrc"
    rc_path.write_text(
        "export KEEP=yes\n\n" + rc_path.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    assert shell_command_path.remove_command_directory(bin_directory) is True
    assert rc_path.read_text(encoding="utf-8") == "export KEEP=yes\n\n"
    assert shell_command_path.remove_command_directory(bin_directory) is False
