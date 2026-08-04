from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PLUGIN_ROOT / "src"


@pytest.fixture(autouse=True)
def add_source_root(monkeypatch):
    monkeypatch.syspath_prepend(str(SRC_ROOT))


def _write_legacy_codex_hook(config_path: Path) -> None:
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        """
[[hooks.SessionStart]]
matcher = "startup"

[[hooks.SessionStart.hooks]]
type = "command"
command = "python /plugin/codex/codex-register.py"

[[hooks.SessionStart]]
matcher = "resume"

[[hooks.SessionStart.hooks]]
type = "command"
command = "python /plugin/keep-this-hook.py"

[[hooks.SessionStart]]
matcher = "startup"

[[hooks.SessionStart.hooks]]
type = "command"
command = "python /plugin/handoff/codex-handoff-pickup.py"

[[hooks.PostToolUse]]
matcher = "tool"

[[hooks.PostToolUse.hooks]]
type = "command"
command = "python /plugin/save-money/auto-handoff.py"
""",
        encoding="utf-8",
    )


def test_windows_migration_is_exact_and_idempotent(tmp_path):
    from agent_meeting.installation.legacy_layout_migration import (
        migrate_legacy_layout,
    )

    user_home = tmp_path / "user"
    meeting_home = user_home / ".agent-meeting"
    codex_home = user_home / ".codex"
    temp_dir = tmp_path / "temp"
    startup = (
        user_home
        / "AppData"
        / "Roaming"
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
    )
    (meeting_home / "bin").mkdir(parents=True)
    temp_dir.mkdir()
    startup.mkdir(parents=True)
    (meeting_home / "bin" / "amctl").write_text("legacy")
    (meeting_home / "bin" / "am-msgd").write_text("current")
    (meeting_home / "amctl.stopped").write_text("legacy")
    (temp_dir / "amctl.pid").write_text("123")
    (startup / "agent-meeting-amctl.cmd").write_text("legacy")
    (startup / "agent-meeting-am-msgd.cmd").write_text("current")
    _write_legacy_codex_hook(codex_home / "config.toml")

    commands = []

    class Result:
        returncode = 0

    def run_command(command, **kwargs):
        commands.append(command)
        return Result()

    removed = migrate_legacy_layout(
        meeting_home=meeting_home,
        codex_home=codex_home,
        user_home=user_home,
        platform_name="win32",
        temp_dir=temp_dir,
        run_command=run_command,
    )
    second_removed = migrate_legacy_layout(
        meeting_home=meeting_home,
        codex_home=codex_home,
        user_home=user_home,
        platform_name="win32",
        temp_dir=temp_dir,
        run_command=run_command,
    )

    assert removed
    assert second_removed == []
    assert (meeting_home / "bin" / "am-msgd").read_text() == "current"
    assert (startup / "agent-meeting-am-msgd.cmd").read_text() == "current"
    assert not (meeting_home / "bin" / "amctl").exists()
    assert not (startup / "agent-meeting-amctl.cmd").exists()
    config = (codex_home / "config.toml").read_text(encoding="utf-8")
    assert "codex-register.py" not in config
    assert "codex-handoff-pickup.py" not in config
    assert "auto-handoff.py" not in config
    assert "keep-this-hook.py" in config
    assert {
        tuple(command)
        for command in commands
        if command[:2] == ["schtasks", "/Delete"]
    } == {
        (
            "schtasks",
            "/Delete",
            "/TN",
            "agent-meeting-daemon",
            "/F",
        ),
        (
            "schtasks",
            "/Delete",
            "/TN",
            "agent-meeting-amctl",
            "/F",
        ),
    }


def test_macos_migration_removes_only_legacy_launch_agents(tmp_path):
    from agent_meeting.installation.legacy_layout_migration import (
        migrate_legacy_layout,
    )

    user_home = tmp_path / "user"
    launch_agents = user_home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    for label in (
        "com.tommy.agent-meeting",
        "com.tommy.agent-meeting.amctl",
        "com.tommy.agent-meeting.am-msgd",
    ):
        (launch_agents / f"{label}.plist").write_text(label)

    commands = []

    class Result:
        returncode = 0

    migrate_legacy_layout(
        meeting_home=user_home / ".agent-meeting",
        codex_home=user_home / ".codex",
        user_home=user_home,
        platform_name="darwin",
        temp_dir=tmp_path / "temp",
        run_command=lambda command, **kwargs: (
            commands.append(command) or Result()
        ),
    )

    assert not (
        launch_agents / "com.tommy.agent-meeting.amctl.plist"
    ).exists()
    assert not (launch_agents / "com.tommy.agent-meeting.plist").exists()
    assert (
        launch_agents / "com.tommy.agent-meeting.am-msgd.plist"
    ).exists()
    assert len(commands) == 2
