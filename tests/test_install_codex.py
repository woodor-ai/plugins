"""Tests for install-codex.py — plugin discovery, copy exclusions, Y/n branches, default/custom dir."""
import importlib.util
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "install-codex.py"


def _load():
    spec = importlib.util.spec_from_file_location("install_codex_module", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_plugin(base: Path, name: str, with_install: bool = True) -> Path:
    p = base / name
    for d in ("codex", "bin", "tests", "__pycache__", ".claude-plugin"):
        (p / d).mkdir(parents=True, exist_ok=True)
    if with_install:
        (p / "codex" / "install.py").write_text("def run_install(ctx): pass\n")
    (p / "bin" / "some-script").write_text("#!/bin/sh\necho hi\n")
    (p / "tests" / "test_foo.py").write_text("def test_x(): pass\n")
    (p / "__pycache__" / "x.pyc").write_text("")
    (p / ".claude-plugin" / "plugin.json").write_text("{}")
    (p / "main.py").write_text("# main\n")
    return p


# ---------------------------------------------------------------------------
# plugin discovery
# ---------------------------------------------------------------------------

def test_discover_finds_plugins_with_install(tmp_path):
    mod = _load()
    _make_plugin(tmp_path, "plugin-a")
    _make_plugin(tmp_path, "plugin-b")
    _make_plugin(tmp_path, "plugin-c", with_install=False)
    found = mod._discover_plugins(tmp_path)
    assert {p.name for p in found} == {"plugin-a", "plugin-b"}


def test_discover_empty_dir(tmp_path):
    mod = _load()
    assert mod._discover_plugins(tmp_path) == []


def test_discover_nonexistent_dir(tmp_path):
    mod = _load()
    assert mod._discover_plugins(tmp_path / "nope") == []


# ---------------------------------------------------------------------------
# copy exclusions
# ---------------------------------------------------------------------------

def test_copy_excludes_tests_pycache_claudeplugin(tmp_path):
    mod = _load()
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    _make_plugin(tmp_path, "src")

    mod._copy_plugin(src, dest)

    assert (dest / "main.py").exists()
    assert (dest / "codex" / "install.py").exists()
    assert (dest / "bin" / "some-script").exists()
    assert not (dest / "tests").exists()
    assert not (dest / "__pycache__").exists()
    assert not (dest / ".claude-plugin").exists()


def test_copy_clears_stale_previous_install(tmp_path):
    """A dest recognizable as a previous install (codex/install.py sentinel) is cleared."""
    mod = _load()
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir()
    (src / "codex").mkdir()
    (src / "codex" / "install.py").write_text("def run_install(ctx): pass\n")

    # dest looks like a previous install: has the sentinel + a stale leftover
    (dest / "codex").mkdir(parents=True)
    (dest / "codex" / "install.py").write_text("# old version\n")
    stale = dest / "stale.txt"
    stale.write_text("old")

    mod._copy_plugin(src, dest)
    assert not stale.exists()
    assert (dest / "codex" / "install.py").read_text() == "def run_install(ctx): pass\n"


def test_copy_into_empty_dest_allowed(tmp_path):
    mod = _load()
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir()
    (src / "codex").mkdir()
    (src / "codex" / "install.py").write_text("def run_install(ctx): pass\n")
    dest.mkdir()  # exists but empty

    mod._copy_plugin(src, dest)
    assert (dest / "codex" / "install.py").exists()


def test_copy_refuses_nonempty_foreign_dir(tmp_path):
    """A non-empty dest WITHOUT the codex/install.py sentinel must be refused, untouched."""
    mod = _load()
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir()
    (src / "codex").mkdir()
    (src / "codex" / "install.py").write_text("def run_install(ctx): pass\n")

    dest.mkdir()
    precious = dest / "precious.txt"
    precious.write_text("do not delete")
    (dest / ".git").mkdir()
    (dest / ".git" / "HEAD").write_text("ref: refs/heads/main\n")

    with pytest.raises(SystemExit):
        mod._copy_plugin(src, dest)

    # nothing was deleted
    assert precious.read_text() == "do not delete"
    assert (dest / ".git" / "HEAD").exists()


# ---------------------------------------------------------------------------
# run_interactive — Y/n branches
# ---------------------------------------------------------------------------

def _fake_loader(mod):
    """Patch _load_plugin_installer to return a no-op module."""
    fake = types.ModuleType("fake_install")
    fake.run_install = lambda ctx: None
    mod._load_plugin_installer = lambda _: fake


def _make_prompt(responses):
    """Prompt fn: consume from iterator; return default when response is empty."""
    it = iter(responses)
    return lambda msg, default="": next(it, default) or default


def test_run_interactive_enter_installs(tmp_path):
    mod = _load()
    src = tmp_path / "src"
    _make_plugin(src, "myplugin")

    calls = []
    fake = types.ModuleType("fake_install")
    fake.run_install = lambda ctx: calls.append(ctx)
    mod._load_plugin_installer = lambda _: fake

    codex_home = tmp_path / "codex"
    result = mod.run_interactive(src, codex_home, prompt_fn=_make_prompt(["", ""]))

    assert len(result["installed"]) == 1
    assert result["installed"][0][0] == "myplugin"
    assert len(result["skipped"]) == 0
    assert len(calls) == 1
    assert calls[0]["install_dir"].name == "myplugin"


def test_run_interactive_n_skips(tmp_path):
    mod = _load()
    src = tmp_path / "src"
    _make_plugin(src, "myplugin")

    result = mod.run_interactive(src, tmp_path / "codex", prompt_fn=lambda msg, default="": "n")

    assert len(result["installed"]) == 0
    assert result["skipped"] == ["myplugin"]


def test_run_interactive_y_explicit(tmp_path):
    mod = _load()
    src = tmp_path / "src"
    _make_plugin(src, "myplugin")
    _fake_loader(mod)

    result = mod.run_interactive(src, tmp_path / "codex", prompt_fn=_make_prompt(["y", ""]))
    assert len(result["installed"]) == 1


def test_run_interactive_custom_dir(tmp_path):
    mod = _load()
    src = tmp_path / "src"
    _make_plugin(src, "myplugin")
    _fake_loader(mod)

    custom = tmp_path / "custom" / "loc"
    result = mod.run_interactive(
        src, tmp_path / "codex",
        prompt_fn=_make_prompt(["y", str(custom)]),
    )

    assert len(result["installed"]) == 1
    assert result["installed"][0][1] == custom
    assert custom.exists()
    assert (custom / "codex" / "install.py").exists()


def test_run_interactive_default_dir_uses_codex_home(tmp_path):
    mod = _load()
    src = tmp_path / "src"
    _make_plugin(src, "myplugin")
    _fake_loader(mod)

    codex_home = tmp_path / "codex"
    result = mod.run_interactive(src, codex_home, prompt_fn=_make_prompt(["y", ""]))

    assert len(result["installed"]) == 1
    expected = codex_home / "plugins" / "myplugin"
    assert result["installed"][0][1] == expected


def test_run_interactive_dot_dir_rejected(tmp_path, monkeypatch):
    """Regression: answering '.' for the install directory (the exact input that
    once wiped a repo checkout) must be refused and leave the cwd untouched."""
    mod = _load()
    src = tmp_path / "src"
    _make_plugin(src, "myplugin")
    _fake_loader(mod)

    workdir = tmp_path / "workdir"
    workdir.mkdir()
    keep = workdir / "keep.txt"
    keep.write_text("still here")
    (workdir / ".git").mkdir()
    (workdir / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    monkeypatch.chdir(workdir)

    responses = iter(["y", "."])
    with pytest.raises(SystemExit):
        mod.run_interactive(
            src, tmp_path / "codex",
            prompt_fn=lambda msg, default="": next(responses),
        )

    # cwd is intact — nothing was deleted
    assert keep.read_text() == "still here"
    assert (workdir / ".git" / "HEAD").exists()


def test_run_interactive_empty_answer_falls_back_to_default(tmp_path, monkeypatch):
    """Regression: a prompt function that returns a literal empty string for the
    install directory must fall back to the default dir — never Path('')/Path('.')."""
    mod = _load()
    src = tmp_path / "src"
    _make_plugin(src, "myplugin")
    _fake_loader(mod)

    workdir = tmp_path / "workdir"
    workdir.mkdir()
    keep = workdir / "keep.txt"
    keep.write_text("still here")
    monkeypatch.chdir(workdir)

    codex_home = tmp_path / "codex"
    # deliberately NO or-default fallback in this fake prompt
    result = mod.run_interactive(
        src, codex_home,
        prompt_fn=lambda msg, default="": "",
    )

    assert len(result["installed"]) == 1
    assert result["installed"][0][1] == codex_home / "plugins" / "myplugin"
    assert (codex_home / "plugins" / "myplugin" / "codex" / "install.py").exists()
    # cwd untouched
    assert keep.read_text() == "still here"


# ---------------------------------------------------------------------------
# mycodex convenience command
# ---------------------------------------------------------------------------

def test_generate_mycodex_command_posix(tmp_path):
    mod = _load()
    assert mod.IS_WINDOWS is False  # test host is macOS/Linux
    bin_dir = tmp_path / "bin"

    mod._generate_mycodex_command(ROOT, bin_dir)

    dest = bin_dir / "mycodex"
    assert dest.exists()
    assert dest.stat().st_mode & 0o111  # executable
    content = dest.read_text()
    assert content == (ROOT / "agent-meeting" / "codex" / "mycodex-posix.sh").read_text()
    assert '"$DEST/install-codex.py" "$@"' in content
    assert "--update" in content
    # no .cmd/.ps1 siblings written on POSIX
    assert not (bin_dir / "mycodex.cmd").exists()
    assert not (bin_dir / "mycodex.ps1").exists()


def test_generate_mycodex_command_windows(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "IS_WINDOWS", True)
    bin_dir = tmp_path / "bin"

    mod._generate_mycodex_command(ROOT, bin_dir)

    dest_ps1 = bin_dir / "mycodex.ps1"
    dest_cmd = bin_dir / "mycodex.cmd"
    assert dest_ps1.exists()
    assert dest_cmd.exists()
    assert dest_ps1.read_text() == (ROOT / "agent-meeting" / "codex" / "mycodex.ps1").read_text()
    assert dest_cmd.read_text() == (ROOT / "agent-meeting" / "codex" / "mycodex.cmd").read_text()
    assert not (bin_dir / "mycodex").exists()


def test_generate_mycodex_command_creates_bin_dir(tmp_path):
    mod = _load()
    bin_dir = tmp_path / "does" / "not" / "exist" / "bin"
    mod._generate_mycodex_command(ROOT, bin_dir)
    assert (bin_dir / "mycodex").exists()


def test_ensure_bin_on_path_posix_no_crash(tmp_path, capsys):
    mod = _load()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    mod._ensure_bin_on_path(bin_dir)  # POSIX branch just prints a hint
    out = capsys.readouterr().out
    assert str(bin_dir) in out


def test_main_generates_mycodex_command_when_something_installed(tmp_path, monkeypatch):
    mod = _load()
    meeting_home = tmp_path / "meeting-home"
    monkeypatch.setenv("MEETING_HOME", str(meeting_home))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setattr(
        mod, "run_interactive",
        lambda *a, **k: {"installed": [("agent-meeting", tmp_path / "installed")], "skipped": []},
    )

    mod.main()

    dest = meeting_home / "bin" / "mycodex"
    assert dest.exists()
    assert dest.stat().st_mode & 0o111


def test_main_generates_mycodex_command_even_when_nothing_installed(tmp_path, monkeypatch):
    """mycodex must be dropped unconditionally so `--update` always works, even
    when the user skipped every plugin in this run (e.g. agent-meeting was
    already installed and they only wanted to update `handoff`)."""
    mod = _load()
    meeting_home = tmp_path / "meeting-home"
    monkeypatch.setenv("MEETING_HOME", str(meeting_home))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setattr(
        mod, "run_interactive",
        lambda *a, **k: {"installed": [], "skipped": ["myplugin"]},
    )

    mod.main()

    dest = meeting_home / "bin" / "mycodex"
    assert dest.exists()
    assert dest.stat().st_mode & 0o111


def test_main_cleans_up_stale_codex_plugins(tmp_path, monkeypatch):
    mod = _load()
    meeting_home = tmp_path / "meeting-home"
    bin_dir = meeting_home / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "codex-plugins").write_text("#!/bin/sh\necho old\n")
    (bin_dir / "codex-plugins.cmd").write_text("@echo off\r\n")
    (bin_dir / "codex-plugins.ps1").write_text("# old\n")

    monkeypatch.setenv("MEETING_HOME", str(meeting_home))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setattr(
        mod, "run_interactive",
        lambda *a, **k: {"installed": [], "skipped": []},
    )

    mod.main()

    assert not (bin_dir / "codex-plugins").exists()
    assert not (bin_dir / "codex-plugins.cmd").exists()
    assert not (bin_dir / "codex-plugins.ps1").exists()
    assert (bin_dir / "mycodex").exists()


def test_cleanup_stale_codex_plugins_refuses_wrong_dir(tmp_path):
    """Safety guard: only ever acts on <meeting_home>/bin, never anywhere else."""
    mod = _load()
    meeting_home = tmp_path / "meeting-home"
    other_dir = tmp_path / "some-other-dir"
    other_dir.mkdir(parents=True)
    (other_dir / "codex-plugins").write_text("do not delete")

    mod._cleanup_stale_codex_plugins(meeting_home, other_dir)

    assert (other_dir / "codex-plugins").read_text() == "do not delete"


def test_cleanup_stale_codex_plugins_removes_windows_mycodex_leftover(tmp_path, monkeypatch):
    """Windows only: a pre-dual-extension extensionless `mycodex` left in bin/
    must be swept, since the Windows regen path only ever writes
    mycodex.ps1 / mycodex.cmd (never an extensionless copy)."""
    mod = _load()
    monkeypatch.setattr(mod, "IS_WINDOWS", True)
    meeting_home = tmp_path / "meeting-home"
    bin_dir = meeting_home / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "mycodex").write_text("#!/bin/sh\necho old posix shim on windows\n")
    (bin_dir / "mycodex.cmd").write_text("@echo off\r\n")
    (bin_dir / "mycodex.ps1").write_text("# current\n")

    mod._cleanup_stale_codex_plugins(meeting_home, bin_dir)

    assert not (bin_dir / "mycodex").exists()
    assert (bin_dir / "mycodex.cmd").exists()
    assert (bin_dir / "mycodex.ps1").exists()


def test_cleanup_stale_codex_plugins_leaves_posix_mycodex_alone(tmp_path):
    """POSIX: extensionless `mycodex` IS the current artifact — cleanup must
    never touch it."""
    mod = _load()
    assert mod.IS_WINDOWS is False  # test host is macOS/Linux
    meeting_home = tmp_path / "meeting-home"
    bin_dir = meeting_home / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "mycodex").write_text("#!/bin/sh\necho current\n")

    mod._cleanup_stale_codex_plugins(meeting_home, bin_dir)

    assert (bin_dir / "mycodex").read_text() == "#!/bin/sh\necho current\n"


def test_run_interactive_multiple_plugins(tmp_path):
    mod = _load()
    src = tmp_path / "src"
    _make_plugin(src, "alpha")
    _make_plugin(src, "beta")
    _fake_loader(mod)

    # install alpha (Y + default dir), skip beta
    result = mod.run_interactive(
        src, tmp_path / "codex",
        prompt_fn=_make_prompt(["y", "", "n"]),
    )

    assert len(result["installed"]) == 1
    assert result["installed"][0][0] == "alpha"
    assert result["skipped"] == ["beta"]
