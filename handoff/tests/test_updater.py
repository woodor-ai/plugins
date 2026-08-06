import importlib.util
import stat
import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


updater = _load("handoff_update", PLUGIN_ROOT / "bin" / "handoff-update.py")
installer = _load(
    "handoff_updater_install",
    PLUGIN_ROOT / "bin" / "handoff-updater-install.py",
)


def test_version_parsers_recognize_both_host_outputs():
    codex = "handoff@woodor  installed, enabled  0.6.3  /tmp/handoff\n"
    claude = "❯ handoff@woodor\n  Version: 0.6.3\n  Scope: user\n"

    assert updater._codex_version(codex) == "0.6.3"
    assert updater._claude_version(claude) == "0.6.3"
    assert updater._codex_version("handoff@woodor  not installed\n") is None


def test_update_commands_use_each_hosts_public_marketplace_flow():
    assert updater.update_commands("codex") == (
        ("codex", ("plugin", "marketplace", "upgrade", "woodor")),
        ("codex", ("plugin", "add", "handoff@woodor")),
    )
    assert updater.update_commands("claude-code") == (
        ("claude", ("plugin", "marketplace", "update", "woodor")),
        ("claude", ("plugin", "update", "handoff@woodor")),
    )


def test_default_update_targets_only_installed_integrations(monkeypatch, capsys):
    installations = (
        updater.Installation("claude-code", "0.6.2"),
        updater.Installation("codex", "0.6.2"),
    )
    calls = []
    monkeypatch.setattr(updater, "installed_integrations", lambda: installations)
    monkeypatch.setattr(updater, "update_target", lambda target: calls.append(target) or True)

    assert updater.main([]) == 0
    assert calls == ["claude-code", "codex"]
    assert "Start a new Codex thread" in capsys.readouterr().out


def test_posix_bootstrap_installs_stable_command_and_path_once(tmp_path):
    source = PLUGIN_ROOT / "bin" / "handoff-update.py"
    home = tmp_path / "home"
    handoff_home = home / ".handoff"
    home.mkdir()
    environment = {"PATH": "/usr/bin", "SHELL": "/bin/zsh"}

    launcher = installer.install_updater(
        source=source,
        handoff_home=handoff_home,
        home=home,
        python_executable=Path("/usr/bin/python3"),
        is_windows=False,
        environ=environment,
    )
    launcher_inode = launcher.stat().st_ino
    rc_inode = (home / ".zshrc").stat().st_ino
    installer.install_updater(
        source=source,
        handoff_home=handoff_home,
        home=home,
        python_executable=Path("/usr/bin/python3"),
        is_windows=False,
        environ=environment,
    )

    assert launcher == handoff_home / "bin" / "handoff-update"
    assert launcher.stat().st_mode & stat.S_IXUSR
    assert launcher.stat().st_ino == launcher_inode
    assert (home / ".zshrc").stat().st_ino == rc_inode
    assert (handoff_home / "bin" / "handoff-update.py").read_bytes() == source.read_bytes()
    assert (home / ".zshrc").read_text(encoding="utf-8").count(
        installer.PATH_BLOCK_BEGIN
    ) == 1


def test_windows_bootstrap_writes_cmd_launcher_without_touching_registry(tmp_path):
    source = PLUGIN_ROOT / "bin" / "handoff-update.py"
    launcher = installer.install_updater(
        source=source,
        handoff_home=tmp_path / ".handoff",
        home=tmp_path,
        python_executable=Path("C:/Python/python.exe"),
        is_windows=True,
        environ={"PATH": ""},
        update_path=False,
    )

    assert launcher.name == "handoff-update.cmd"
    content = launcher.read_text(encoding="utf-8")
    assert '"C:\\Python\\python.exe"' in content
