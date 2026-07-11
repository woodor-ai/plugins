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


def test_copy_clears_stale_dest(tmp_path):
    mod = _load()
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir()
    (src / "codex").mkdir()
    (src / "codex" / "install.py").write_text("def run_install(ctx): pass\n")

    dest.mkdir()
    stale = dest / "stale.txt"
    stale.write_text("old")

    mod._copy_plugin(src, dest)
    assert not stale.exists()
    assert (dest / "codex" / "install.py").exists()


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
