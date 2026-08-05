"""Behavioral tests for marketplace-free Codex integration installation."""

import importlib.util
import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "installers/shared/install-codex-integration.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("install_codex_integration", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_installs_owned_skills_without_a_marketplace(tmp_path, monkeypatch):
    module = _load_module()
    codex_home = tmp_path / "codex"
    monkeypatch.delenv("CODEX_BIN", raising=False)
    monkeypatch.setattr(module.shutil, "which", lambda _command: None)

    assert module.main(["--codex-home", str(codex_home)]) == 0

    for skill_name in module.SKILL_NAMES:
        destination = codex_home / "skills" / skill_name
        assert (destination / "SKILL.md").is_file()
        if skill_name == "imagent":
            assert (destination / "scripts" / "bootstrap_runtime.py").is_file()
        owner = json.loads(
            (destination / module.OWNER_FILE).read_text(encoding="utf-8")
        )
        assert owner == {
            "product": "agent-meeting",
            "schema_version": 1,
            "version": "0.18.26",
        }


def test_upgrade_replaces_an_owned_skill(tmp_path, monkeypatch):
    module = _load_module()
    codex_home = tmp_path / "codex"
    destination = codex_home / "skills" / "imagent"
    destination.mkdir(parents=True)
    (destination / module.OWNER_FILE).write_text(
        json.dumps({"product": "agent-meeting"}),
        encoding="utf-8",
    )
    (destination / "obsolete.txt").write_text("obsolete", encoding="utf-8")
    monkeypatch.setattr(module.shutil, "which", lambda _command: None)

    assert module.main(["--codex-home", str(codex_home)]) == 0
    assert not (destination / "obsolete.txt").exists()
    assert (destination / "SKILL.md").is_file()


def test_refuses_to_replace_an_unowned_skill(tmp_path, monkeypatch, capsys):
    module = _load_module()
    codex_home = tmp_path / "codex"
    destination = codex_home / "skills" / "imagent"
    destination.mkdir(parents=True)
    (destination / "SKILL.md").write_text("user content", encoding="utf-8")
    monkeypatch.setattr(module.shutil, "which", lambda _command: None)

    assert module.main(["--codex-home", str(codex_home)]) == 1
    assert capsys.readouterr().err.splitlines() == [
        "ERROR: Codex integration installation failed: "
        f"refusing to replace unowned Codex skill: {destination}"
    ]
    assert (destination / "SKILL.md").read_text(encoding="utf-8") == "user content"


def test_removes_legacy_plugin_and_disposable_marketplace(
    tmp_path,
    monkeypatch,
):
    module = _load_module()
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        '[marketplaces.woodor]\nsource_type = "local"\n'
        'source = "C:\\\\Temp\\\\agent-meeting-install-old\\\\source"\n',
        encoding="utf-8",
    )
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "{}", "")

    monkeypatch.setattr(module.shutil, "which", lambda _command: "codex")
    monkeypatch.setattr(module.subprocess, "run", run)

    assert module.main(["--codex-home", str(codex_home)]) == 0
    assert commands == [
        ["codex", "plugin", "remove", "agent-meeting@woodor"],
        ["codex", "plugin", "marketplace", "remove", "woodor"],
    ]


def test_preserves_non_disposable_marketplace(tmp_path, monkeypatch):
    module = _load_module()
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        '[marketplaces.woodor]\nsource_type = "git"\n'
        'source = "https://github.com/woodor-ai/plugins.git"\n',
        encoding="utf-8",
    )
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 1, "", "not installed")

    monkeypatch.setattr(module.shutil, "which", lambda _command: "codex")
    monkeypatch.setattr(module.subprocess, "run", run)

    assert module.main(["--codex-home", str(codex_home)]) == 0
    assert commands == [
        ["codex", "plugin", "remove", "agent-meeting@woodor"]
    ]


def test_reports_disposable_marketplace_cleanup_failure(
    tmp_path,
    monkeypatch,
    capsys,
):
    module = _load_module()
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        '[marketplaces.woodor]\nsource_type = "local"\n'
        'source = "C:\\\\Temp\\\\agent-meeting-install-old\\\\source"\n',
        encoding="utf-8",
    )

    def run(command, **_kwargs):
        returncode = 5 if "marketplace" in command else 0
        return subprocess.CompletedProcess(
            command,
            returncode,
            "",
            "Access is denied" if returncode else "",
        )

    monkeypatch.setattr(module.shutil, "which", lambda _command: "codex")
    monkeypatch.setattr(module.subprocess, "run", run)

    assert module.main(["--codex-home", str(codex_home)]) == 1
    assert capsys.readouterr().err.splitlines() == [
        "ERROR: Codex integration installation failed: could not remove the "
        "obsolete disposable Codex marketplace (exit 5): Access is denied"
    ]


def test_reports_legacy_plugin_cleanup_failure(tmp_path, monkeypatch, capsys):
    module = _load_module()
    codex_home = tmp_path / "codex"
    monkeypatch.setattr(module.shutil, "which", lambda _command: "codex")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            5,
            "",
            "Access is denied",
        ),
    )

    assert module.main(["--codex-home", str(codex_home)]) == 1
    assert capsys.readouterr().err.splitlines() == [
        "ERROR: Codex integration installation failed: could not remove the "
        "legacy Codex marketplace plugin (exit 5): Access is denied"
    ]
