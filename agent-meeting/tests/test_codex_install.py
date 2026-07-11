"""
Tests for:
  - agent-meeting/codex/install.py   _parse_controls()
  - agent-meeting/bin/session-bootstrap.py  mycodex wrapper generation
    and _all_present() sentinel when mycodex is absent.

All tests run without a live daemon and without touching real ~/.agent-meeting
or ~/.codex.  The bootstrap is loaded with env vars pointing at tmp_path dirs,
then its module-level globals are monkey-patched to keep everything in tmp_path.
"""
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parents[2]
INSTALL_PY = REPO / "agent-meeting" / "codex" / "install.py"
BOOTSTRAP_PY = REPO / "agent-meeting" / "bin" / "session-bootstrap.py"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _load_install():
    spec = importlib.util.spec_from_file_location("am_codex_install", INSTALL_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_bootstrap(meeting_home: Path, plugin_root: Path):
    """Load session-bootstrap with MEETING_HOME + PLUGIN_ROOT env overrides."""
    env_patch = {
        "MEETING_HOME": str(meeting_home),
        "PLUGIN_ROOT": str(plugin_root),
    }
    with patch.dict(os.environ, env_patch):
        spec = importlib.util.spec_from_file_location(
            f"bootstrap_{id(meeting_home)}", BOOTSTRAP_PY
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# _parse_controls
# ---------------------------------------------------------------------------

def test_parse_controls_empty_list():
    mod = _load_install()
    assert mod._parse_controls("[]") == ""


def test_parse_controls_single():
    mod = _load_install()
    data = [{"ip": "192.168.1.10", "port": 8765}]
    assert mod._parse_controls(json.dumps(data)) == "http://192.168.1.10:8765"


def test_parse_controls_prefers_is_current():
    mod = _load_install()
    data = [
        {"ip": "10.0.0.1", "port": 8765},
        {"ip": "192.168.1.5", "port": 8765, "is_current": True},
    ]
    assert mod._parse_controls(json.dumps(data)) == "http://192.168.1.5:8765"


def test_parse_controls_star_among_many():
    mod = _load_install()
    data = [
        {"ip": "1.1.1.1", "port": 9000},
        {"ip": "2.2.2.2", "port": 9000, "is_current": True},
        {"ip": "3.3.3.3", "port": 9000},
    ]
    assert mod._parse_controls(json.dumps(data)) == "http://2.2.2.2:9000"


def test_parse_controls_fallback_to_first():
    mod = _load_install()
    data = [
        {"ip": "10.0.0.1", "port": 8765},
        {"ip": "10.0.0.2", "port": 8765},
    ]
    assert mod._parse_controls(json.dumps(data)) == "http://10.0.0.1:8765"


def test_parse_controls_missing_ip():
    mod = _load_install()
    data = [{"port": 8765}]
    assert mod._parse_controls(json.dumps(data)) == ""


def test_parse_controls_missing_port():
    mod = _load_install()
    data = [{"ip": "192.168.1.10"}]
    assert mod._parse_controls(json.dumps(data)) == ""


def test_parse_controls_invalid_json():
    mod = _load_install()
    assert mod._parse_controls("not-json") == ""


# ---------------------------------------------------------------------------
# _ensure_agents_md refresh branch — regression for the Windows-path bad-escape
# crash (re.sub was passed `block` as a raw replacement string; a Windows venv
# path like C:\Users\admin\... contains `\U`, which re.sub's template parser
# rejects with `re.error: bad escape \U`). Only the REFRESH branch is affected
# (existing AGENTS.md already has the begin/end markers) — a fresh install
# takes the append branch and never hits re.sub.
# ---------------------------------------------------------------------------

def test_ensure_agents_md_refresh_with_windows_backslash_path(tmp_path):
    mod = _load_install()
    codex_home = tmp_path / "codex_home"
    meeting_home = tmp_path / "meeting_home"
    codex_home.mkdir()
    meeting_home.mkdir()

    agents = codex_home / "AGENTS.md"
    agents.write_text(
        f"some pre-existing content\n\n{mod._AGENTS_BEGIN}\nstale block\n{mod._AGENTS_END}\n",
        encoding="utf-8",
    )

    # Force a Windows-style backslash path into the generated block (this is
    # what a real Windows install produces via _venv_python; on this test
    # machine pathlib would render POSIX paths, so fake it directly). `mod` is
    # a fresh, throwaway module instance for this test only — no restore needed.
    win_vpy = r"C:\Users\admin\.agent-meeting\venv\Scripts\python.exe"
    mod._venv_python = lambda _meeting_home: win_vpy
    mod._ensure_agents_md(codex_home, meeting_home, "http://10.0.0.5:8765")

    text = agents.read_text(encoding="utf-8")
    assert "some pre-existing content" in text, "unrelated pre-existing content must survive the refresh"
    assert win_vpy in text
    assert "agent-meeting (peer messaging)" in text
    assert "stale block" not in text


def test_ensure_agents_md_append_branch_unaffected(tmp_path):
    """Sanity: a fresh AGENTS.md (no markers yet) takes the append branch,
    which never touches re.sub and was never at risk."""
    mod = _load_install()
    codex_home = tmp_path / "codex_home"
    meeting_home = tmp_path / "meeting_home"
    codex_home.mkdir()
    meeting_home.mkdir()

    win_vpy = r"C:\Users\admin\.agent-meeting\venv\Scripts\python.exe"
    mod._venv_python = lambda _meeting_home: win_vpy
    mod._ensure_agents_md(codex_home, meeting_home, "http://10.0.0.5:8765")

    text = (codex_home / "AGENTS.md").read_text(encoding="utf-8")
    assert win_vpy in text
    assert mod._AGENTS_BEGIN in text and mod._AGENTS_END in text


# ---------------------------------------------------------------------------
# bootstrap wrapper generation (POSIX only — .cmd branch is Windows-specific)
# ---------------------------------------------------------------------------

def _make_plugin_root(base: Path) -> Path:
    """Create minimal plugin root structure for bootstrap."""
    pr = base / "agent-meeting"
    (pr / "bin").mkdir(parents=True)
    (pr / "codex").mkdir(parents=True)
    (pr / ".claude-plugin").mkdir(parents=True)
    (pr / "bin" / "meeting").write_text("#!/bin/sh\necho meeting\n")
    (pr / "bin" / "meeting-daemon").write_text("#!/bin/sh\necho daemon\n")
    (pr / "codex" / "codex-meeting.py").write_text("# stub\n")
    (pr / "codex" / "meeting-say.py").write_text("# stub\n")
    (pr / "codex" / "mycodex-posix.sh").write_text("#!/bin/sh\necho mycodex-stub\n")
    (pr / "codex" / "mycodex.ps1").write_text("# mycodex-stub\n")
    (pr / "codex" / "mycodex.cmd").write_text("@echo off\r\n")
    (pr / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "agent-meeting", "version": "0.8.39"})
    )
    return pr


def _make_venv(meeting_home: Path):
    venv_bin = meeting_home / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    shutil.copy(sys.executable, str(venv_bin / "python"))
    return venv_bin / "python"


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX wrapper test")
def test_mycodex_wrapper_generated_posix(tmp_path):
    meeting_home = tmp_path / "meeting"
    meeting_home.mkdir()
    plugin_root = _make_plugin_root(tmp_path)
    _make_venv(meeting_home)

    mod = _load_bootstrap(meeting_home, plugin_root)
    mod.DATA = meeting_home
    mod.BIN_LINK = meeting_home / "bin"
    mod.VENV = meeting_home / "venv"
    mod.PLUGIN_ROOT = plugin_root

    mod.ensure_bin_wrappers()

    bin_dir = meeting_home / "bin"
    assert (bin_dir / "mycodex").exists(), "mycodex wrapper missing"
    assert not (bin_dir / "codex-meeting").exists(), "old codex-meeting should be absent"


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX wrapper test")
def test_old_codex_meeting_removed_on_regen(tmp_path):
    """If a stale codex-meeting file exists in bin, regeneration must remove it."""
    meeting_home = tmp_path / "meeting"
    meeting_home.mkdir()
    plugin_root = _make_plugin_root(tmp_path)
    _make_venv(meeting_home)

    old_bin = meeting_home / "bin"
    old_bin.mkdir(parents=True)
    (old_bin / "codex-meeting").write_text("#!/bin/sh\necho old\n")

    mod = _load_bootstrap(meeting_home, plugin_root)
    mod.DATA = meeting_home
    mod.BIN_LINK = old_bin
    mod.VENV = meeting_home / "venv"
    mod.PLUGIN_ROOT = plugin_root

    mod.ensure_bin_wrappers()

    assert not (meeting_home / "bin" / "codex-meeting").exists()
    assert (meeting_home / "bin" / "mycodex").exists()


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX sentinel test")
def test_sentinel_does_not_skip_when_mycodex_absent(tmp_path):
    """
    If mycodex is absent from bin/, _all_present() must return False even when
    the sentinel (PLUGIN_ROOT) matches — forcing regeneration.
    """
    meeting_home = tmp_path / "meeting"
    meeting_home.mkdir()
    plugin_root = _make_plugin_root(tmp_path)
    _make_venv(meeting_home)

    bin_dir = meeting_home / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("meeting", "meeting-daemon", "meeting-say"):
        (bin_dir / name).write_text("#!/bin/sh\n")

    mod = _load_bootstrap(meeting_home, plugin_root)
    mod.DATA = meeting_home
    mod.BIN_LINK = bin_dir
    mod.VENV = meeting_home / "venv"
    mod.PLUGIN_ROOT = plugin_root

    mod.ensure_bin_wrappers()

    assert (bin_dir / "mycodex").exists(), (
        "_all_present() incorrectly skipped regen when mycodex was absent"
    )


# ---------------------------------------------------------------------------
# Windows-only extensionless `mycodex` leftover cleanup — a pre-dual-extension
# install (or an old bootstrap on Windows) could leave a POSIX-shell
# extensionless `mycodex` sitting in bin/ forever, since Windows only ever
# regenerates mycodex.ps1/.cmd. IS_WINDOWS is force-patched here since the
# test host is macOS; all file operations exercised are OS-agnostic.
# ---------------------------------------------------------------------------

def test_windows_mycodex_leftover_removed_on_regen(tmp_path):
    meeting_home = tmp_path / "meeting"
    meeting_home.mkdir()
    plugin_root = _make_plugin_root(tmp_path)
    _make_venv(meeting_home)

    bin_dir = meeting_home / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "mycodex").write_text("#!/bin/sh\necho old posix shim stuck on windows\n")

    mod = _load_bootstrap(meeting_home, plugin_root)
    mod.IS_WINDOWS = True
    mod.DATA = meeting_home
    mod.BIN_LINK = bin_dir
    mod.VENV = meeting_home / "venv"
    mod.PLUGIN_ROOT = plugin_root

    mod.ensure_bin_wrappers()

    assert not (bin_dir / "mycodex").exists()
    assert (bin_dir / "mycodex.cmd").exists()
    assert (bin_dir / "mycodex.ps1").exists()


def test_windows_mycodex_leftover_swept_on_sentinel_match(tmp_path):
    """Once the sentinel matches and _all_present() is satisfied, the full
    regen path never runs again — the stale-file sweep must still fire on
    that early-return path, or a leftover extensionless `mycodex` would
    persist forever on Windows."""
    meeting_home = tmp_path / "meeting"
    meeting_home.mkdir()
    plugin_root = _make_plugin_root(tmp_path)
    _make_venv(meeting_home)

    bin_dir = meeting_home / "bin"

    mod = _load_bootstrap(meeting_home, plugin_root)
    mod.IS_WINDOWS = True
    mod.DATA = meeting_home
    mod.BIN_LINK = bin_dir
    mod.VENV = meeting_home / "venv"
    mod.PLUGIN_ROOT = plugin_root

    mod.ensure_bin_wrappers()  # first call: full regen, settles the sentinel
    assert (bin_dir / "mycodex.cmd").exists()

    # Simulate a leftover from a pre-dual-extension install reappearing.
    (bin_dir / "mycodex").write_text("old posix shim stuck on windows\n")

    mod.ensure_bin_wrappers()  # second call: sentinel matches -> early-return path

    assert not (bin_dir / "mycodex").exists(), (
        "stale extensionless mycodex must be swept even on the early-return path"
    )
    assert (bin_dir / "mycodex.cmd").exists()
    assert (bin_dir / "mycodex.ps1").exists()
