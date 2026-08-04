import tomllib
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def add_source_root(monkeypatch):
    monkeypatch.syspath_prepend(str(PLUGIN_ROOT / "src"))


def _hook_block(command: str, matcher: str = "startup") -> str:
    return (
        "[[hooks.SessionStart]]\n"
        f'matcher = "{matcher}"\n\n'
        "[[hooks.SessionStart.hooks]]\n"
        'type = "command"\n'
        f'command = "{command}"\n'
    )


def test_owned_hook_removal_reindexes_remaining_hooks(tmp_path):
    from agent_meeting.ai_platforms.codex import user_configuration

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        _hook_block("python /legacy/codex-register.py")
        + _hook_block("python /current/pickup.py", "resume")
        + f'\n[hooks.state."{config_path}:session_start:1:0"]\n'
        'enabled = true\ntrusted_hash = "stale"\n',
        encoding="utf-8",
    )

    assert user_configuration.remove_owned_hooks(
        config_path,
        event_table="SessionStart",
        command_markers=("codex-register.py",),
    )

    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    blocks = parsed["hooks"]["SessionStart"]
    assert len(blocks) == 1
    assert blocks[0]["matcher"] == "resume"
    key = f"{config_path}:session_start:0:0"
    assert parsed["hooks"]["state"][key]["enabled"] is True
    assert "codex-register.py" not in config_path.read_text(encoding="utf-8")


def test_owned_hook_removal_preserves_plugin_managed_state(tmp_path):
    from agent_meeting.ai_platforms.codex import user_configuration

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        _hook_block("python /legacy/codex-register.py")
        + f'\n[hooks.state."{config_path}:session_start:0:0"]\n'
        'trusted_hash = "config-owned"\n'
        + '\n[hooks.state."handoff@woodor:hooks/hooks.json:'
        'session_start:0:0"]\ntrusted_hash = "plugin-owned"\n',
        encoding="utf-8",
    )

    assert user_configuration.remove_owned_hooks(
        config_path,
        event_table="SessionStart",
        command_markers=("codex-register.py",),
    )

    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    state = parsed["hooks"]["state"]
    assert f"{config_path}:session_start:0:0" not in state
    assert state[
        "handoff@woodor:hooks/hooks.json:session_start:0:0"
    ]["trusted_hash"] == "plugin-owned"


def test_owned_hook_removal_is_noop_without_owned_block(tmp_path):
    from agent_meeting.ai_platforms.codex import user_configuration

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        _hook_block("python /current/pickup.py"),
        encoding="utf-8",
    )
    before = config_path.read_text(encoding="utf-8")

    assert not user_configuration.remove_owned_hooks(
        config_path,
        event_table="SessionStart",
        command_markers=("codex-register.py",),
    )
    assert config_path.read_text(encoding="utf-8") == before
