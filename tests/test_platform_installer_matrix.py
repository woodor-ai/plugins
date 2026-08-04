import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "installers" / "install.py"


def _load_installer():
    spec = importlib.util.spec_from_file_location(
        "unified_agent_meeting_installer",
        INSTALLER,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_one_cross_platform_installer_owns_every_target(tmp_path, monkeypatch):
    installer = _load_installer()
    calls = []
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )

    installer.install(
        source_root=tmp_path / "plugins",
        meeting_home=tmp_path / "meeting",
        target="all",
        control_url="http://10.0.0.8:8765",
        enable_full_automation=True,
    )

    commands = [command for command, _kwargs in calls]
    assert "--configure-codex" in commands[0]
    assert "--control-url" in commands[0]
    assert "--enable-full-automation" in commands[0]
    assert any("register-claude-marketplace.py" in command[1] for command in commands)
    assert any("register-codex-marketplace.py" in command[1] for command in commands)
    assert commands[-1][-2:] == ["update", "--defer-if-active"]


def test_legacy_platform_installers_are_removed():
    assert INSTALLER.is_file()
    assert not (ROOT / "installers/claude-code").exists()
    assert not (ROOT / "installers/codex").exists()
