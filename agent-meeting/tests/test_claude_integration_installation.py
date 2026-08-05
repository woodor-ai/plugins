import importlib.util
import json
from pathlib import Path
import subprocess


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "installers/shared/install-claude-integration.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "install_claude_integration",
        SCRIPT,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_install_writes_direct_integration_without_claude_marketplace(
    tmp_path,
    monkeypatch,
):
    module = _load_module()
    calls = []
    session_start = tmp_path / "meeting" / "bin" / "am-claude-session-start"
    monkeypatch.setattr(module, "_plugin_version", lambda: "0.18.34")
    monkeypatch.setattr(
        module.claude_integration,
        "install_skills",
        lambda **kwargs: calls.append(("skills", kwargs)),
    )
    monkeypatch.setattr(
        module.claude_integration,
        "install_user_configuration",
        lambda **kwargs: calls.append(("settings", kwargs)),
    )
    monkeypatch.setattr(
        module.claude_integration,
        "session_start_executable",
        lambda _meeting_home: session_start,
    )
    monkeypatch.setattr(module.shutil, "which", lambda _name: None)
    commands = []
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda command, **_kwargs: (
            commands.append(command)
            or subprocess.CompletedProcess(command, 0, "{}", "")
        ),
    )

    claude_home = tmp_path / "claude"
    meeting_home = tmp_path / "meeting"
    module.install(claude_home=claude_home, meeting_home=meeting_home)

    assert calls == [
        (
            "skills",
            {
                "source_root": REPOSITORY_ROOT,
                "claude_home": claude_home,
                "version": "0.18.34",
            },
        ),
        (
            "settings",
            {
                "settings_path": claude_home / "settings.json",
                "meeting_home": meeting_home,
            },
        ),
    ]
    assert commands == [[str(session_start)]]


def test_removes_legacy_plugin_and_disposable_marketplace(monkeypatch):
    module = _load_module()
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        if command[-3:] == ["plugin", "list", "--json"]:
            output = json.dumps(
                [{"id": "agent-meeting@woodor", "version": "0.18.34"}]
            )
        elif command[-4:] == ["plugin", "marketplace", "list", "--json"]:
            output = json.dumps(
                [
                    {
                        "name": "woodor",
                        "source": "directory",
                        "path": "C:\\Temp\\agent-meeting-install-old\\source",
                    }
                ]
            )
        else:
            output = ""
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(module.subprocess, "run", run)

    module.remove_legacy_marketplace_integration("claude")

    assert commands == [
        ["claude", "plugin", "list", "--json"],
        [
            "claude",
            "plugin",
            "uninstall",
            "agent-meeting@woodor",
            "-y",
        ],
        ["claude", "plugin", "marketplace", "list", "--json"],
        ["claude", "plugin", "marketplace", "remove", "woodor"],
    ]


def test_preserves_non_disposable_woodor_marketplace(monkeypatch):
    module = _load_module()
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        if command[-3:] == ["plugin", "list", "--json"]:
            output = "[]"
        elif command[-4:] == ["plugin", "marketplace", "list", "--json"]:
            output = json.dumps(
                [
                    {
                        "name": "woodor",
                        "source": "github",
                        "repo": "woodor-ai/plugins",
                    }
                ]
            )
        else:
            output = ""
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(module.subprocess, "run", run)

    module.remove_legacy_marketplace_integration("claude")

    assert commands == [
        ["claude", "plugin", "list", "--json"],
        ["claude", "plugin", "marketplace", "list", "--json"],
    ]
