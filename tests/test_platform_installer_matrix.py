import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


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
    recorded = []
    monkeypatch.setattr(
        installer,
        "_record_installation",
        lambda *args: recorded.append(args),
    )
    meeting_home = tmp_path / "meeting"
    legacy_checkout = meeting_home / "updates" / "plugins"
    (legacy_checkout / ".git").mkdir(parents=True)

    installer.install(
        source_root=tmp_path / "plugins",
        meeting_home=meeting_home,
        target="all",
        control_url="http://10.0.0.8:8765",
        enable_full_automation=True,
    )

    commands = [command for command, _kwargs in calls]
    assert "--configure-codex" in commands[0]
    assert "--control-url" in commands[0]
    assert "--enable-full-automation" in commands[0]
    assert any("register-claude-marketplace.py" in command[1] for command in commands)
    assert any("install-codex-integration.py" in command[1] for command in commands)
    assert commands[-1][-2:] == ["update", "--defer-if-active"]
    assert recorded == [(tmp_path / "plugins", meeting_home, "all")]
    assert not legacy_checkout.exists()


def test_legacy_platform_installers_are_removed():
    assert INSTALLER.is_file()
    assert not (ROOT / "installers/claude-code").exists()
    assert not (ROOT / "installers/codex").exists()


def test_child_installer_failure_identifies_stage(tmp_path, monkeypatch, capsys):
    installer = _load_installer()
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.CalledProcessError(5, "install-codex-integration.py")
        ),
    )

    with pytest.raises(subprocess.CalledProcessError):
        installer._run_python(
            tmp_path,
            "installers/shared/install-codex-integration.py",
        )

    assert capsys.readouterr().err.splitlines() == [
        "ERROR: installer stage install-codex-integration.py failed (exit 5)"
    ]
