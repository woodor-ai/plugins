import json
from pathlib import Path
import shutil

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def add_source_root(monkeypatch):
    monkeypatch.syspath_prepend(
        str(REPOSITORY_ROOT / "agent-meeting" / "src")
    )


def test_installs_owned_personal_skills(tmp_path):
    from agent_meeting.installation import claude_integration

    claude_home = tmp_path / "claude"
    claude_integration.install_skills(
        source_root=REPOSITORY_ROOT,
        claude_home=claude_home,
        version="0.18.23",
    )

    for skill_name in claude_integration.SKILL_NAMES:
        destination = claude_home / "skills" / skill_name
        assert (destination / "SKILL.md").is_file()
        owner = json.loads(
            (destination / claude_integration.OWNER_FILE).read_text(
                encoding="utf-8"
            )
        )
        assert owner == {
            "product": "agent-meeting",
            "schema_version": 1,
            "version": "0.18.23",
        }
    assert (
        claude_home / "skills" / "imagent" / "scripts" / "bootstrap_runtime.py"
    ).is_file()


def test_installed_skills_survive_disposable_source_removal(tmp_path):
    from agent_meeting.installation import claude_integration

    source_root = tmp_path / "agent-meeting-install-temp" / "source"
    for skill_name in claude_integration.SKILL_NAMES:
        skill = source_root / "agent-meeting" / "skills" / skill_name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {skill_name}\n---\n",
            encoding="utf-8",
        )
    scripts = source_root / "agent-meeting" / "scripts"
    scripts.mkdir()
    (scripts / "bootstrap_runtime.py").write_text("pass\n", encoding="utf-8")
    claude_home = tmp_path / "claude"

    claude_integration.install_skills(
        source_root=source_root,
        claude_home=claude_home,
        version="0.18.23",
    )
    shutil.rmtree(source_root.parent)

    assert (claude_home / "skills" / "imagent" / "SKILL.md").is_file()
    assert (claude_home / "skills" / "talkto" / "SKILL.md").is_file()


def test_skill_upgrade_replaces_owned_content_and_refuses_unowned(tmp_path):
    from agent_meeting.installation import claude_integration

    claude_home = tmp_path / "claude"
    imagent = claude_home / "skills" / "imagent"
    talkto = claude_home / "skills" / "talkto"
    imagent.mkdir(parents=True)
    talkto.mkdir(parents=True)
    (imagent / claude_integration.OWNER_FILE).write_text(
        json.dumps({"product": "agent-meeting"}),
        encoding="utf-8",
    )
    (imagent / "obsolete.txt").write_text("obsolete", encoding="utf-8")
    (talkto / "SKILL.md").write_text("user content", encoding="utf-8")

    with pytest.raises(RuntimeError, match="unowned Claude skill"):
        claude_integration.install_skills(
            source_root=REPOSITORY_ROOT,
            claude_home=claude_home,
            version="0.18.23",
        )

    assert not (imagent / "obsolete.txt").exists()
    assert (talkto / "SKILL.md").read_text(encoding="utf-8") == "user content"


def test_user_configuration_is_idempotent_and_preserves_other_hooks(tmp_path):
    from agent_meeting.installation import claude_integration

    settings_path = tmp_path / "claude" / "settings.json"
    settings_path.parent.mkdir()
    unrelated = {
        "matcher": "resume",
        "hooks": [{"type": "command", "command": "keep-me"}],
    }
    settings_path.write_text(
        json.dumps(
            {
                "theme": "dark",
                "hooks": {
                    "SessionStart": [unrelated],
                    "PostToolUse": [],
                },
            }
        ),
        encoding="utf-8",
    )
    meeting_home = tmp_path / "agent meeting"

    for _attempt in range(2):
        claude_integration.install_user_configuration(
            settings_path=settings_path,
            meeting_home=meeting_home,
            is_windows=True,
        )

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["theme"] == "dark"
    assert settings["hooks"]["PostToolUse"] == []
    groups = settings["hooks"]["SessionStart"]
    assert groups[0] == unrelated
    assert groups[1] == {
        "matcher": "startup",
        "hooks": [
            {
                "type": "command",
                "command": claude_integration.session_start_command(
                    meeting_home,
                    is_windows=True,
                ),
            }
        ],
    }
    assert len(groups) == 2


def test_removal_deletes_only_owned_claude_configuration(tmp_path):
    from agent_meeting.installation import claude_integration

    claude_home = tmp_path / "claude"
    meeting_home = tmp_path / "meeting"
    settings_path = claude_home / "settings.json"
    claude_integration.install_user_configuration(
        settings_path=settings_path,
        meeting_home=meeting_home,
        is_windows=True,
    )
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings["hooks"]["SessionStart"].append(
        {
            "matcher": "resume",
            "hooks": [{"type": "command", "command": "keep-me"}],
        }
    )
    settings["statusLine"] = {
        "type": "command",
        "command": str(meeting_home / "bin" / "am-statusline.exe"),
    }
    settings_path.write_text(json.dumps(settings), encoding="utf-8")

    owned = claude_home / "skills" / "imagent"
    unowned = claude_home / "skills" / "talkto"
    owned.mkdir(parents=True)
    unowned.mkdir(parents=True)
    (owned / claude_integration.OWNER_FILE).write_text(
        json.dumps({"product": "agent-meeting"}),
        encoding="utf-8",
    )
    (unowned / "SKILL.md").write_text("user content", encoding="utf-8")

    claude_integration.remove_skills(claude_home)
    claude_integration.remove_user_configuration(
        settings_path=settings_path,
        meeting_home=meeting_home,
        is_windows=True,
    )

    assert not owned.exists()
    assert unowned.exists()
    remaining = json.loads(settings_path.read_text(encoding="utf-8"))
    assert remaining["hooks"]["SessionStart"] == [
        {
            "matcher": "resume",
            "hooks": [{"type": "command", "command": "keep-me"}],
        }
    ]
    assert "statusLine" not in remaining
