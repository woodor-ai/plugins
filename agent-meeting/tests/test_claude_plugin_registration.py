import importlib.util
import json
from pathlib import Path
import subprocess

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "installers/shared/register-claude-marketplace.py"


@pytest.fixture
def registration():
    spec = importlib.util.spec_from_file_location(
        "register_claude_marketplace",
        SCRIPT,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_installed_plugin_reports_version(registration, monkeypatch):
    payload = json.dumps(
        [{"id": "agent-meeting@woodor", "version": "0.8.32"}]
    )
    monkeypatch.setattr(
        registration.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, payload, ""
        ),
    )

    assert registration.installed_plugin("claude", "agent-meeting@woodor") == (
        True,
        "0.8.32",
    )


def test_main_updates_an_existing_outdated_plugin(registration, monkeypatch):
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(registration.shutil, "which", lambda _name: "claude")
    monkeypatch.setattr(registration.subprocess, "run", run)
    monkeypatch.setattr(
        registration,
        "installed_plugin",
        lambda *_args: (True, "0.8.32"),
    )
    monkeypatch.setattr(
        registration,
        "source_plugin_version",
        lambda: "0.18.17",
    )

    assert registration.main() == 0
    assert commands == [
        ["claude", "plugin", "marketplace", "remove", "woodor"],
        ["claude", "plugin", "marketplace", "add", str(REPOSITORY_ROOT)],
        ["claude", "plugin", "update", "agent-meeting@woodor"],
    ]


def test_main_installs_when_plugin_is_absent(registration, monkeypatch):
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(registration.shutil, "which", lambda _name: "claude")
    monkeypatch.setattr(registration.subprocess, "run", run)
    monkeypatch.setattr(
        registration,
        "installed_plugin",
        lambda *_args: (False, None),
    )
    monkeypatch.setattr(
        registration,
        "source_plugin_version",
        lambda: "0.18.17",
    )

    assert registration.main() == 0
    assert commands == [
        ["claude", "plugin", "marketplace", "remove", "woodor"],
        ["claude", "plugin", "marketplace", "add", str(REPOSITORY_ROOT)],
        ["claude", "plugin", "install", "agent-meeting@woodor"],
    ]
