"""Behavioral tests for Codex marketplace registration recovery."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "installers/shared/register-codex-marketplace.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("register_codex_marketplace", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_does_not_reregister_existing_marketplace_after_upgrade_failure(monkeypatch):
    module = _load_module()
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[-3:] == ["marketplace", "upgrade", "woodor"]:
            return SimpleNamespace(returncode=1)
        if command[-3:] == ["marketplace", "list", "--json"]:
            return SimpleNamespace(
                returncode=0,
                stdout='{"marketplaces": [{"name": "woodor"}]}',
            )
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(module.shutil, "which", lambda _command: "/usr/bin/codex")
    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module, "installed_plugin_version", lambda *_args: None)

    assert module.main() == 1
    assert [command for command, _ in calls] == [
        ["/usr/bin/codex", "plugin", "marketplace", "upgrade", "woodor"],
        ["/usr/bin/codex", "plugin", "marketplace", "list", "--json"],
    ]


def test_skips_marketplace_refresh_when_installed_version_matches(monkeypatch, capsys):
    module = _load_module()

    monkeypatch.setattr(module.shutil, "which", lambda _command: "/usr/bin/codex")
    monkeypatch.setattr(
        module, "installed_plugin_version", lambda *_args: "0.15.1"
    )
    monkeypatch.setattr(module, "source_plugin_version", lambda: "0.15.1")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("matching versions must not refresh the marketplace")
        ),
    )

    assert module.main() == 0
    assert capsys.readouterr().out.splitlines() == [
        "Codex plugin already matches version 0.15.1; skipping marketplace refresh."
    ]


def test_skips_plugin_add_when_plugin_is_already_installed(monkeypatch, capsys):
    module = _load_module()
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[-3:] == ["marketplace", "upgrade", "woodor"]:
            return SimpleNamespace(returncode=0)
        if command[-3:] == ["plugin", "list", "--json"]:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    '{"installed": [{"pluginId": "agent-meeting@woodor", '
                    '"installed": true}]}'
                ),
            )
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(module.shutil, "which", lambda _command: "/usr/bin/codex")
    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(
        module, "installed_plugin_version", lambda *_args: "0.15.1"
    )
    monkeypatch.setattr(module, "source_plugin_version", lambda: "0.15.2")

    assert module.main() == 0
    assert capsys.readouterr().out.splitlines() == [
        "Refreshing Codex marketplace: woodor...",
        "Codex plugin already installed; skipping redundant reinstall.",
    ]
    assert [command for command, _ in calls] == [
        ["/usr/bin/codex", "plugin", "marketplace", "upgrade", "woodor"],
        ["/usr/bin/codex", "plugin", "list", "--json"],
    ]


def test_adds_local_marketplace_only_when_listing_confirms_it_is_absent(monkeypatch):
    module = _load_module()
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[-3:] == ["marketplace", "upgrade", "woodor"]:
            return SimpleNamespace(returncode=1)
        if command[-3:] == ["marketplace", "list", "--json"]:
            return SimpleNamespace(returncode=0, stdout='{"marketplaces": []}')
        if command[-3:] == ["plugin", "list", "--json"]:
            return SimpleNamespace(returncode=0, stdout='{"installed": []}')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.shutil, "which", lambda _command: "/usr/bin/codex")
    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module, "installed_plugin_version", lambda *_args: None)

    assert module.main() == 0
    assert [command for command, _ in calls] == [
        ["/usr/bin/codex", "plugin", "marketplace", "upgrade", "woodor"],
        ["/usr/bin/codex", "plugin", "marketplace", "list", "--json"],
        ["/usr/bin/codex", "plugin", "marketplace", "add", str(ROOT)],
        ["/usr/bin/codex", "plugin", "list", "--json"],
        ["/usr/bin/codex", "plugin", "add", "agent-meeting@woodor"],
    ]
