"""
Tests for:
  - agent-meeting/codex/install.py   _parse_controls()
  - agent-meeting/bin/session-bootstrap.py  amcodex wrapper generation
    and _all_present() sentinel when amcodex is absent.

All tests run without a live central am-msgd and without touching real ~/.agent-meeting
or ~/.codex.  The bootstrap is loaded with env vars pointing at tmp_path dirs,
then its module-level globals are monkey-patched to keep everything in tmp_path.
"""
import importlib.util
import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parents[2]
INSTALL_PY = REPO / "agent-meeting" / "codex" / "install.py"
BOOTSTRAP_PY = REPO / "agent-meeting" / "bin" / "session-bootstrap.py"
HOOK_REMOVER_PY = (
    REPO / "agent-meeting" / "codex" / "remove-legacy-codex-hook.py"
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _load_install():
    spec = importlib.util.spec_from_file_location("am_codex_install", INSTALL_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_bootstrap(
    meeting_home: Path, plugin_root: Path, *, ai_platform: str = "codex"
):
    """Load session-bootstrap with one AI platform's plugin-root contract."""
    env_patch = {"MEETING_HOME": str(meeting_home)}
    if ai_platform == "claude":
        env_patch["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
    else:
        env_patch["PLUGIN_ROOT"] = str(plugin_root)
    with patch.dict(os.environ, env_patch, clear=True):
        spec = importlib.util.spec_from_file_location(
            f"bootstrap_{id(meeting_home)}", BOOTSTRAP_PY
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


def _load_hook_remover():
    spec = importlib.util.spec_from_file_location(
        "am_codex_hook_remover", HOOK_REMOVER_PY
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


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX shell wrapper test")
def test_discover_control_runs_posix_runtime_wrapper_directly(tmp_path):
    mod = _load_install()
    meeting_home = tmp_path / "meeting-home"
    cli = meeting_home / "bin" / "am"
    cli.parent.mkdir(parents=True)
    cli.write_text(
        "#!/bin/sh\n"
        "printf '[{\"ip\":\"10.0.0.114\",\"port\":8765,\"is_current\":true}]'\n",
        encoding="utf-8",
    )
    cli.chmod(0o755)

    assert mod._discover_control(
        meeting_home, Path(sys.executable)
    ) == "http://10.0.0.114:8765"


def test_discover_control_reports_cli_failure(tmp_path, capsys):
    mod = _load_install()
    meeting_home = tmp_path / "meeting-home"
    cli = meeting_home / "bin" / "am"
    cli.parent.mkdir(parents=True)
    cli.write_text(
        "import sys\nprint('discovery exploded', file=sys.stderr)\nsys.exit(7)\n",
        encoding="utf-8",
    )

    assert mod._discover_control(meeting_home, Path(sys.executable)) == ""
    assert "control discovery command failed (rc=7): discovery exploded" in capsys.readouterr().out


def test_select_control_uses_discovery_without_prompt(tmp_path):
    mod = _load_install()

    def unexpected_prompt(*_args):
        pytest.fail("prompt must not run when mDNS discovery succeeded")

    assert mod._select_control_url(
        "http://10.0.0.114:8765",
        "",
        unexpected_prompt,
    ) == "http://10.0.0.114:8765"


def test_select_control_prompts_without_shared_result(tmp_path):
    mod = _load_install()

    assert mod._select_control_url(
        "", "", lambda *_args: "http://prompt:8765"
    ) == "http://prompt:8765"


def test_select_control_prefers_explicit_override(tmp_path):
    mod = _load_install()

    def unexpected_prompt(*_args):
        pytest.fail("prompt must not run for an explicit override")

    assert mod._select_control_url(
        "http://10.0.0.114:8765",
        "http://10.0.0.99:9000",
        unexpected_prompt,
    ) == "http://10.0.0.99:9000"


def test_pin_control_host_uses_public_am_host(tmp_path, monkeypatch, capsys):
    mod = _load_install()
    meeting_home = tmp_path / "meeting-home"
    cli = meeting_home / "bin" / "am"
    cli.parent.mkdir(parents=True)
    cli.write_text("#!/bin/sh\n", encoding="utf-8")
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(mod.am_common, "run_am_cli", fake_run)
    mod._pin_control_host(
        meeting_home,
        Path(sys.executable),
        "http://10.0.0.8:8765",
    )

    assert calls == [
        (
            (cli, "host", "http://10.0.0.8:8765"),
            {"python": Path(sys.executable), "timeout": 10},
        )
    ]
    assert "saved shared control_host" in capsys.readouterr().out


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
    mod.IS_WINDOWS = True
    mod._ensure_agents_md(codex_home, meeting_home)

    text = agents.read_text(encoding="utf-8")
    assert "some pre-existing content" in text, "unrelated pre-existing content must survive the refresh"
    assert win_vpy in text
    assert "agent-meeting (peer messaging)" in text
    assert "stale block" not in text


def test_windows_agents_md_executes_activated_meeting_exe_directly(
    tmp_path,
):
    mod = _load_install()
    mod.IS_WINDOWS = True
    codex_home = tmp_path / "codex-home"
    meeting_home = tmp_path / "meeting-home"
    meeting_exe = meeting_home / "bin" / "am.exe"
    meeting_exe.parent.mkdir(parents=True)
    meeting_exe.touch()

    mod._ensure_agents_md(
        codex_home,
        meeting_home,
    )

    text = (codex_home / "AGENTS.md").read_text(encoding="utf-8")
    assert f'& "{meeting_exe}" message NAME@PROJECT N' in text
    assert "python.exe" not in text


def test_active_runtime_python_precedes_legacy_venv(tmp_path):
    mod = _load_install()
    meeting_home = tmp_path / "meeting-home"
    runtime = meeting_home / "runtimes" / "0.15.0"
    runtime.mkdir(parents=True)
    (meeting_home / "active-runtime.json").write_text(
        json.dumps(
            {
                "version": "0.15.0",
                "runtime": str(runtime),
            }
        ),
        encoding="utf-8",
    )

    expected = (
        runtime / "venv" / (
            "Scripts/python.exe" if mod.IS_WINDOWS else "bin/python"
        )
    )
    assert mod._venv_python(meeting_home) == expected


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
    mod.IS_WINDOWS = True
    mod._ensure_agents_md(codex_home, meeting_home)

    text = (codex_home / "AGENTS.md").read_text(encoding="utf-8")
    assert win_vpy in text
    assert mod._AGENTS_BEGIN in text and mod._AGENTS_END in text


def test_ensure_agents_md_posix_runs_runtime_wrappers_directly(tmp_path):
    mod = _load_install()
    mod.IS_WINDOWS = False
    codex_home = tmp_path / "codex_home"
    meeting_home = tmp_path / "meeting_home"
    codex_home.mkdir()
    meeting_home.mkdir()

    mod._ensure_agents_md(codex_home, meeting_home)

    text = (codex_home / "AGENTS.md").read_text(encoding="utf-8")
    cli = meeting_home / "bin" / "am"
    assert f'"{cli}" send NAME@PROJECT X' in text
    assert "--host" not in text
    assert "MEETING_SELF" not in text
    assert "AM_MSGD_HOST" not in text
    assert f'"{cli}" list' in text
    assert "meeting-say" not in text
    assert "& \"" not in text
    assert str(mod._venv_python(meeting_home)) not in text


def test_session_context_keeps_claude_unregistered_flow(tmp_path, monkeypatch, capsys):
    meeting_home = tmp_path / "am"
    plugin_root = _make_plugin_root(tmp_path)
    meeting_home.mkdir()
    mod = _load_bootstrap(meeting_home, plugin_root)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("AGENT_MEETING_CODEX_RUNTIME", raising=False)
    monkeypatch.setattr(mod, "online_peers_str", lambda: "(none online)")

    mod.emit_context({"is_host": False})

    payload = json.loads(capsys.readouterr().out)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "This session has NO meeting name yet" in context
    assert "thread-level developer instructions" not in context


def test_session_context_defers_codex_identity_to_thread_params(
    tmp_path, monkeypatch, capsys
):
    meeting_home = tmp_path / "am"
    plugin_root = _make_plugin_root(tmp_path)
    meeting_home.mkdir()
    mod = _load_bootstrap(meeting_home, plugin_root)
    # The shared Codex app-server has no per-thread environment variable.  A
    # amcodex launch supplies this persistent runtime marker instead.
    monkeypatch.setenv("AGENT_MEETING_CODEX_RUNTIME", "1")
    monkeypatch.setattr(mod, "online_peers_str", lambda: "(none online)")

    mod.emit_context({"is_host": False})

    payload = json.loads(capsys.readouterr().out)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "An `amcodex` launch supplies its exact agent-meeting recipient" in context
    assert "thread and turn request parameters" in context
    assert "This session has NO meeting name yet" not in context


def test_session_start_never_rewrites_an_activated_runtime(
    tmp_path,
    monkeypatch,
):
    meeting_home = tmp_path / "am"
    plugin_root = _make_plugin_root(tmp_path)
    runtime = meeting_home / "runtimes" / "0.15.0"
    runtime.mkdir(parents=True)
    meeting_home.mkdir(exist_ok=True)
    (meeting_home / "active-runtime.json").write_text(
        json.dumps(
            {
                "version": "0.15.0",
                "runtime": str(runtime),
                "commands": {},
            }
        ),
        encoding="utf-8",
    )
    mod = _load_bootstrap(meeting_home, plugin_root)

    def forbidden(*args, **kwargs):
        raise AssertionError("activated runtime was rewritten")

    monkeypatch.setattr(mod, "ensure_venv", forbidden)
    monkeypatch.setattr(mod, "ensure_zeroconf", forbidden)
    monkeypatch.setattr(mod, "ensure_websockets", forbidden)
    monkeypatch.setattr(mod, "ensure_bin_wrappers", forbidden)
    monkeypatch.setattr(mod, "ensure_statusline", lambda: None)
    monkeypatch.setattr(
        mod,
        "load_or_create_config",
        lambda min_version=None: (
            {"is_host": False, "plugin_version": min_version},
            False,
            "machine",
        ),
    )
    monkeypatch.setattr(
        mod,
        "ensure_local_message_hub_service",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(mod, "emit_context", lambda cfg: None)

    mod.main()


def test_activated_runtime_still_repairs_macos_host_service(
    tmp_path,
    monkeypatch,
):
    meeting_home = tmp_path / "am"
    plugin_root = _make_plugin_root(tmp_path)
    runtime = meeting_home / "runtimes" / "0.15.0"
    runtime.mkdir(parents=True)
    (meeting_home / "active-runtime.json").write_text(
        json.dumps(
            {
                "version": "0.15.0",
                "runtime": str(runtime),
                "commands": {},
            }
        ),
        encoding="utf-8",
    )
    mod = _load_bootstrap(meeting_home, plugin_root)
    mod.IS_MAC = True
    mod.IS_WINDOWS = False
    mod.IS_LINUX = False

    def forbidden(*_args, **_kwargs):
        raise AssertionError("activated runtime was rewritten")

    monkeypatch.setattr(mod, "ensure_venv", forbidden)
    monkeypatch.setattr(mod, "ensure_zeroconf", forbidden)
    monkeypatch.setattr(mod, "ensure_websockets", forbidden)
    monkeypatch.setattr(mod, "ensure_bin_wrappers", forbidden)
    monkeypatch.setattr(mod, "ensure_statusline", lambda: None)
    monkeypatch.setattr(
        mod,
        "load_or_create_config",
        lambda min_version=None: (
            {"is_host": True, "plugin_version": min_version},
            False,
            "machine",
        ),
    )
    service_checks = []
    monkeypatch.setattr(
        mod,
        "ensure_local_message_hub_service",
        lambda *args, **kwargs: service_checks.append("user-service"),
    )
    monkeypatch.setattr(mod, "emit_context", lambda _cfg: None)

    mod.main()

    assert service_checks == ["user-service"]


def test_legacy_codex_registration_hook_is_removed_without_touching_others():
    mod = _load_hook_remover()
    content = """
[[hooks.SessionStart]]
matcher = "startup|resume|clear|compact"

[[hooks.SessionStart.hooks]]
type = "command"
command = "/venv/python /plugin/codex/codex-register.py"
timeout = 10

[[hooks.SessionStart]]
matcher = "startup"

[[hooks.SessionStart.hooks]]
type = "command"
command = "/venv/python /plugin/handoff/session-start.py"
timeout = 10
"""

    updated = mod.remove_legacy_blocks(content)

    assert "codex-register.py" not in updated
    assert "handoff/session-start.py" in updated


def test_hook_state_reindex_preserves_plugin_owned_trust_entries():
    mod = _load_hook_remover()
    config_key = mod.toml_escape(str(mod.CONFIG_PATH))
    content = f"""
[[hooks.SessionStart]]
matcher = "startup"

[[hooks.SessionStart.hooks]]
type = "command"
command = "/plugin/codex/codex-register.py"

[[hooks.SessionStart]]
matcher = "resume"

[[hooks.SessionStart.hooks]]
type = "command"
command = "/plugin/handoff/session-start.py"

[hooks.state]

[hooks.state."{config_key}:session_start:0:0"]
enabled = true
trusted_hash = "old-agent-meeting"

[hooks.state."{config_key}:session_start:1:0"]
enabled = true
trusted_hash = "old-handoff-index"

[hooks.state."handoff@woodor:hooks/hooks.json:session_start:0:0"]
trusted_hash = "plugin-owned"
"""

    updated = mod.rewrite_session_start_state(mod.remove_legacy_blocks(content))

    assert "codex-register.py" not in updated
    assert f'{config_key}:session_start:0:0' in updated
    assert f'{config_key}:session_start:1:0' not in updated
    assert "old-agent-meeting" not in updated
    assert "old-handoff-index" not in updated
    assert "plugin-owned" in updated


# ---------------------------------------------------------------------------
# bootstrap wrapper generation (POSIX only — .cmd branch is Windows-specific)
# ---------------------------------------------------------------------------

def _make_plugin_root(base: Path) -> Path:
    """Create minimal plugin root structure for bootstrap."""
    pr = base / "agent-meeting"
    (pr / "bin").mkdir(parents=True)
    (pr / "codex").mkdir(parents=True)
    (pr / ".codex-plugin").mkdir(parents=True)
    (pr / ".claude-plugin").mkdir(parents=True)
    (pr / "bin" / "am").write_text("#!/bin/sh\necho meeting\n")
    (pr / "bin" / "am-msgd").write_text("#!/bin/sh\necho central am-msgd\n")
    (pr / "bin" / "am-codexd").write_text("#!/usr/bin/env python3\n")
    (pr / "bin" / "monitor.py").write_text("print('monitor-v1')\n")
    (pr / "codex" / "codex-session.py").write_text("# stub\n")
    (pr / "codex" / "amcodex-posix.sh").write_text("#!/bin/sh\necho amcodex-stub\n")
    (pr / "codex" / "amcodex-impl.ps1").write_text("# amcodex-stub\n")
    (pr / "codex" / "amcodex.cmd").write_text("@echo off\r\n")
    (pr / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "agent-meeting", "version": "0.7.99"})
    )
    (pr / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": "agent-meeting", "version": "0.8.39"})
    )
    return pr


def _make_venv(meeting_home: Path):
    venv_bin = meeting_home / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    shutil.copy(sys.executable, str(venv_bin / "python"))
    return venv_bin / "python"


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX wrapper test")
def test_amcodex_wrapper_generated_posix(tmp_path):
    meeting_home = tmp_path / "am"
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
    assert (bin_dir / "amcodex").exists(), "amcodex wrapper missing"
    assert not (bin_dir / "mycodex").exists()
    assert (bin_dir / "am-codexd").exists(), "am-codexd wrapper missing"
    assert not (bin_dir / "codex-meeting").exists(), "old codex-meeting should be absent"


def test_posix_amcodex_uses_active_plugin_stamp():
    wrapper = (
        Path(__file__).resolve().parents[1] / "codex" / "amcodex-posix.sh"
    ).read_text(encoding="utf-8")

    assert ".bin-plugin-root" in wrapper
    assert 'plugins/agent-meeting/codex/codex-meeting.py' not in wrapper


def test_windows_amcodex_uses_active_plugin_stamp():
    wrapper = (
        Path(__file__).resolve().parents[1] / "codex" / "amcodex-impl.ps1"
    ).read_text(encoding="utf-8")

    assert ".bin-plugin-root" in wrapper
    assert 'plugins\\agent-meeting\\codex\\codex-meeting.py' not in wrapper


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX wrapper test")
def test_old_codex_meeting_removed_on_regen(tmp_path):
    """If a stale codex-meeting file exists in bin, regeneration must remove it."""
    meeting_home = tmp_path / "am"
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
    assert (meeting_home / "bin" / "amcodex").exists()
    assert not (meeting_home / "bin" / "mycodex").exists()


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX wrapper test")
def test_same_install_path_new_version_regenerates_copied_runtime(tmp_path):
    meeting_home = tmp_path / "am"
    meeting_home.mkdir()
    plugin_root = _make_plugin_root(tmp_path)
    _make_venv(meeting_home)

    mod = _load_bootstrap(meeting_home, plugin_root)
    mod.DATA = meeting_home
    mod.BIN_LINK = meeting_home / "bin"
    mod.VENV = meeting_home / "venv"
    mod.PLUGIN_ROOT = plugin_root

    mod.ensure_bin_wrappers()
    assert (meeting_home / "bin" / "monitor.py").read_text() == "print('monitor-v1')\n"

    (plugin_root / "bin" / "monitor.py").write_text(
        "print('monitor-v2')\n",
        encoding="utf-8",
    )
    (plugin_root / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": "agent-meeting", "version": "0.8.40"}),
        encoding="utf-8",
    )
    mod.ensure_bin_wrappers()

    assert (meeting_home / "bin" / "monitor.py").read_text() == "print('monitor-v2')\n"
    assert (meeting_home / ".bin-plugin-root").read_text().endswith("\n0.8.40")


def test_bootstrap_reads_invoking_ai_platform_manifest(tmp_path):
    meeting_home = tmp_path / "am"
    plugin_root = _make_plugin_root(tmp_path)

    codex_mod = _load_bootstrap(meeting_home, plugin_root, ai_platform="codex")
    claude_mod = _load_bootstrap(meeting_home, plugin_root, ai_platform="claude")

    assert codex_mod._read_plugin_version() == "0.8.39"
    assert claude_mod._read_plugin_version() == "0.7.99"


def test_copied_am_msgd_entrypoint_resolves_activated_plugin_source(tmp_path):
    plugin_root = REPO / "agent-meeting"
    meeting_home = tmp_path / ".agent-meeting"
    copied_bin = meeting_home / "bin"
    copied_bin.mkdir(parents=True)
    copied_entrypoint = copied_bin / "am-msgd"
    shutil.copyfile(plugin_root / "bin" / "am-msgd", copied_entrypoint)
    (meeting_home / ".bin-plugin-root").write_text(
        f"{plugin_root / 'bin'}\n0.15.0",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(copied_entrypoint), "--help"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "usage: am-msgd" in result.stdout


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX wrapper test")
def test_stale_command_names_are_removed_without_full_regeneration(tmp_path):
    meeting_home = tmp_path / "am"
    meeting_home.mkdir()
    plugin_root = _make_plugin_root(tmp_path)
    _make_venv(meeting_home)

    mod = _load_bootstrap(meeting_home, plugin_root)
    mod.DATA = meeting_home
    mod.BIN_LINK = meeting_home / "bin"
    mod.VENV = meeting_home / "venv"
    mod.PLUGIN_ROOT = plugin_root

    mod.ensure_bin_wrappers()
    stale_paths = [
        meeting_home / "bin" / "meeting-say",
        meeting_home / "bin" / "amctl",
    ]
    for stale in stale_paths:
        stale.write_text("#!/bin/sh\n", encoding="utf-8")

    mod.ensure_bin_wrappers()

    assert all(not stale.exists() for stale in stale_paths)


def test_legacy_launchd_install_migrates_missing_is_host_to_true(tmp_path):
    meeting_home = tmp_path / "am"
    meeting_home.mkdir()
    plugin_root = _make_plugin_root(tmp_path)
    config = meeting_home / "config.json"
    config.write_text(
        json.dumps({"machine_id": "machine-1", "plugin_version": "0.12.0"}),
        encoding="utf-8",
    )
    old_plist = tmp_path / "com.tommy.agent-meeting.plist"
    old_plist.touch()

    mod = _load_bootstrap(meeting_home, plugin_root)
    mod.CONFIG = config
    mod.PLUGIN_ROOT = plugin_root
    mod.IS_MAC = True
    mod.IS_WINDOWS = False
    mod.IS_LINUX = False
    mod._PRE_AM_MSGD_LAUNCHD_PLIST = old_plist
    mod.LAUNCHD_PLIST = tmp_path / "com.tommy.agent-meeting.am-msgd.plist"

    cfg, _, _ = mod.load_or_create_config()

    assert cfg["is_host"] is True
    assert json.loads(config.read_text(encoding="utf-8"))["is_host"] is True


def test_am_msgd_launchd_migration_removes_old_amctl_service(tmp_path, monkeypatch):
    meeting_home = tmp_path / "am"
    meeting_home.mkdir()
    plugin_root = _make_plugin_root(tmp_path)
    mod = _load_bootstrap(meeting_home, plugin_root)
    pre_msgd_plist = tmp_path / "com.tommy.agent-meeting.plist"
    legacy_amctl_plist = tmp_path / "com.tommy.agent-meeting.amctl.plist"
    pre_msgd_plist.touch()
    legacy_amctl_plist.touch()
    mod._PRE_AM_MSGD_LAUNCHD_PLIST = pre_msgd_plist
    mod._LEGACY_AMCTL_LAUNCHD_PLIST = legacy_amctl_plist

    commands = []

    def fake_run(args, **_kwargs):
        commands.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    monkeypatch.setattr(mod.os, "getuid", lambda: 501)

    mod._remove_pre_am_msgd_launchd_service()

    assert not pre_msgd_plist.exists()
    assert not legacy_amctl_plist.exists()
    assert [
        "launchctl",
        "bootout",
        "gui/501/com.tommy.agent-meeting.amctl",
    ] in commands


def test_windows_am_msgd_migration_stops_old_amctl_supervisor(tmp_path, monkeypatch):
    meeting_home = tmp_path / "am"
    meeting_home.mkdir()
    plugin_root = _make_plugin_root(tmp_path)
    mod = _load_bootstrap(meeting_home, plugin_root)
    mod.DATA = meeting_home
    mod.BIN_LINK = meeting_home / "bin"
    mod.BIN_LINK.mkdir()
    (mod.BIN_LINK / "supervisor.py").write_text("# supervisor\n", encoding="utf-8")
    mod.VENV = meeting_home / "venv"
    pythonw = mod.VENV / "Scripts" / "pythonw.exe"
    pythonw.parent.mkdir(parents=True)
    pythonw.touch()
    mod.STARTUP_DIR = tmp_path / "Startup"
    mod.STARTUP_CMD = mod.STARTUP_DIR / "agent-meeting-am-msgd.cmd"
    mod._PRE_AM_MSGD_STARTUP_CMD = mod.STARTUP_DIR / "agent-meeting-daemon.cmd"
    mod._LEGACY_AMCTL_STARTUP_CMD = mod.STARTUP_DIR / "agent-meeting-amctl.cmd"
    mod.STARTUP_DIR.mkdir()
    mod._LEGACY_AMCTL_STARTUP_CMD.touch()
    mod.SCHTASKS_SENTINEL = meeting_home / ".schtasks-cmd"
    mod._LEGACY_AMCTL_STOP_SENTINEL = meeting_home / "amctl.stopped"
    mod._LEGACY_AMCTL_PID_FILE = tmp_path / "amctl.pid"
    mod._LEGACY_AMCTL_PID_FILE.write_text("111", encoding="utf-8")
    mod.SUPERVISOR_PID_FILE = tmp_path / "meeting-supervisor.pid"
    mod.SUPERVISOR_PID_FILE.write_text("222", encoding="utf-8")

    commands = []

    def fake_run(args, **_kwargs):
        commands.append(list(args))
        if args[:4] == [
            "schtasks",
            "/Query",
            "/TN",
            mod._LEGACY_AMCTL_SCHTASKS_TN,
        ]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:4] == ["schtasks", "/Query", "/TN", mod.SCHTASKS_TN]:
            return subprocess.CompletedProcess(args, 1, "", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    monkeypatch.setattr(mod, "kill_bootstrap_am_msgd", lambda: None)
    monkeypatch.setattr(mod, "_launch_supervisor_now", lambda *_args: None)

    mod.ensure_windows_persistence()

    assert ["taskkill", "/F", "/PID", "111"] in commands
    assert ["taskkill", "/F", "/PID", "222"] in commands
    assert not mod._LEGACY_AMCTL_STARTUP_CMD.exists()
    assert not mod._LEGACY_AMCTL_PID_FILE.exists()
    assert not mod.SUPERVISOR_PID_FILE.exists()
    assert not mod._LEGACY_AMCTL_STOP_SENTINEL.exists()
    assert mod.STARTUP_CMD.exists()


def test_am_msgd_version_match_requires_live_installed_version(tmp_path, monkeypatch):
    meeting_home = tmp_path / "am"
    meeting_home.mkdir()
    plugin_root = _make_plugin_root(tmp_path)
    mod = _load_bootstrap(meeting_home, plugin_root)

    monkeypatch.setattr(
        mod,
        "_am_msgd_health_info",
        lambda _port=8765, _timeout=1.0: {
            "ok": True,
            "version": "0.13.4",
        },
    )

    assert mod._am_msgd_version_matches("0.13.4") is True
    assert mod._am_msgd_version_matches("0.13.5") is False


def test_am_msgd_version_match_accepts_unknown_installed_version(tmp_path, monkeypatch):
    meeting_home = tmp_path / "am"
    meeting_home.mkdir()
    plugin_root = _make_plugin_root(tmp_path)
    mod = _load_bootstrap(meeting_home, plugin_root)

    monkeypatch.setattr(
        mod,
        "_am_msgd_health_info",
        lambda _port=8765, _timeout=1.0: {
            "ok": True,
            "version": "legacy",
        },
    )

    assert mod._am_msgd_version_matches("unknown") is True


@pytest.mark.skipif(sys.platform.startswith("win"), reason="launchd test")
def test_launchd_plist_uses_stable_wrapper_across_plugin_cache_roots(
    tmp_path, monkeypatch
):
    meeting_home = tmp_path / "am"
    meeting_home.mkdir()
    plugin_root_a = _make_plugin_root(tmp_path / "claude-cache")
    plugin_root_b = _make_plugin_root(tmp_path / "codex-cache")
    _make_venv(meeting_home)

    mod = _load_bootstrap(meeting_home, plugin_root_a)
    mod.DATA = meeting_home
    mod.BIN_LINK = meeting_home / "bin"
    mod.VENV = meeting_home / "venv"
    mod.PLUGIN_ROOT = plugin_root_a
    mod.LAUNCHD_PLIST = tmp_path / "com.tommy.agent-meeting.am-msgd.plist"
    mod.ensure_bin_wrappers()

    expected_plist = {
        "Label": mod.LAUNCHD_LABEL,
        "ProgramArguments": [
            str(mod.BIN_LINK / "am-msgd"),
            "serve",
            "--config",
            str(mod.DATA / "am-msgd.json"),
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(mod.TMP / "am-msgd.log"),
        "StandardErrorPath": str(mod.TMP / "am-msgd.log"),
        "ProcessType": "Background",
    }
    mod.LAUNCHD_PLIST.write_bytes(plistlib.dumps(expected_plist))

    commands = []

    def fake_run(args, **_kwargs):
        commands.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(mod, "_remove_pre_am_msgd_launchd_service", lambda: None)
    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    monkeypatch.setattr(
        mod,
        "_am_msgd_health_info",
        lambda *_args, **_kwargs: {
            "ok": True,
            "version": "0.8.39",
            "instance_id": "running-instance",
        },
    )

    mod.ensure_launchd()
    mod.PLUGIN_ROOT = plugin_root_b
    mod.ensure_bin_wrappers()
    mod.ensure_launchd()

    plist = plistlib.loads(mod.LAUNCHD_PLIST.read_bytes())
    assert plist["ProgramArguments"] == [
        str(mod.BIN_LINK / "am-msgd"),
        "serve",
        "--config",
        str(mod.DATA / "am-msgd.json"),
    ]
    lifecycle_commands = [
        command[1]
        for command in commands
        if command[:1] == ["launchctl"] and len(command) > 1
    ]
    assert "bootout" not in lifecycle_commands
    assert "bootstrap" not in lifecycle_commands
    assert "load" not in lifecycle_commands


def test_wait_launchd_stopped_requires_old_health_instance_to_disappear(
    tmp_path, monkeypatch
):
    meeting_home = tmp_path / "am"
    meeting_home.mkdir()
    plugin_root = _make_plugin_root(tmp_path)
    mod = _load_bootstrap(meeting_home, plugin_root)

    listed = iter([True, False, False, False])
    health = iter([
        {"ok": True, "version": "0.8.39", "instance_id": "old-instance"},
        {"ok": True, "version": "0.8.39", "instance_id": "old-instance"},
        {},
        {},
    ])
    probes = []

    def fake_run(args, **_kwargs):
        value = next(listed)
        probes.append(("job", value))
        return subprocess.CompletedProcess(args, 0 if value else 1, "", "")

    def fake_health(*_args, **_kwargs):
        value = next(health)
        probes.append(("health", value.get("instance_id", "")))
        return value

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    monkeypatch.setattr(mod, "_am_msgd_health_info", fake_health)
    clock = [0.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        mod.time,
        "sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )

    assert mod._wait_launchd_stopped(
        "gui/501/com.tommy.agent-meeting.am-msgd",
        {"ok": True, "version": "0.8.39", "instance_id": "old-instance"},
        total=1.0,
        interval=0.25,
    )
    assert probes == [
        ("job", True),
        ("health", "old-instance"),
        ("job", False),
        ("health", "old-instance"),
        ("job", False),
        ("health", ""),
        ("job", False),
        ("health", ""),
    ]


def test_wait_launchd_stopped_treats_health_as_old_when_initial_probe_failed(
    tmp_path, monkeypatch
):
    meeting_home = tmp_path / "am"
    meeting_home.mkdir()
    plugin_root = _make_plugin_root(tmp_path)
    mod = _load_bootstrap(meeting_home, plugin_root)
    listed = iter([False, False, False])
    health = iter([
        {"ok": True, "version": "0.8.39", "instance_id": "late-old"},
        {},
        {},
    ])
    clock = [0.0]

    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda args, **_kwargs: subprocess.CompletedProcess(
            args, 0 if next(listed) else 1, "", ""
        ),
    )
    monkeypatch.setattr(mod, "_am_msgd_health_info", lambda **_kwargs: next(health))
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        mod.time,
        "sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )

    assert mod._wait_launchd_stopped(
        "gui/501/com.tommy.agent-meeting.am-msgd",
        {},
        total=0.75,
        interval=0.25,
    )


def test_wait_new_am_msgd_rejects_old_and_requires_stable_new_instance(
    tmp_path, monkeypatch
):
    meeting_home = tmp_path / "am"
    meeting_home.mkdir()
    plugin_root = _make_plugin_root(tmp_path)
    mod = _load_bootstrap(meeting_home, plugin_root)

    health = iter([
        {"ok": True, "version": "0.8.39", "instance_id": "old-instance"},
        {"ok": True, "version": "0.8.38", "instance_id": "wrong-version"},
        {"ok": True, "version": "0.8.39", "instance_id": "new-instance"},
        {"ok": True, "version": "0.8.39", "instance_id": "new-instance"},
    ])
    probes = []

    def fake_health(*_args, **_kwargs):
        value = next(health)
        probes.append(value["instance_id"])
        return value

    monkeypatch.setattr(mod, "_am_msgd_health_info", fake_health)
    clock = [0.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        mod.time,
        "sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )

    assert mod._wait_new_am_msgd(
        "0.8.39",
        "old-instance",
        total=1.0,
        interval=0.25,
        stable_checks=2,
    )
    assert probes == [
        "old-instance",
        "wrong-version",
        "new-instance",
        "new-instance",
    ]


def test_launchd_waits_include_probe_latency_in_deadline(tmp_path, monkeypatch):
    meeting_home = tmp_path / "am"
    meeting_home.mkdir()
    plugin_root = _make_plugin_root(tmp_path)
    mod = _load_bootstrap(meeting_home, plugin_root)
    clock = [0.0]

    def fake_run(args, timeout, **_kwargs):
        clock[0] += min(timeout, 0.6)
        return subprocess.CompletedProcess(args, 0, "", "")

    def fake_health(*_args, timeout, **_kwargs):
        clock[0] += timeout
        return {}

    monkeypatch.setattr(mod.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        mod.time,
        "sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )
    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    monkeypatch.setattr(mod, "_am_msgd_health_info", fake_health)

    assert not mod._wait_launchd_stopped(
        "gui/501/com.tommy.agent-meeting.am-msgd",
        {"ok": True, "instance_id": "old-instance"},
        total=1.0,
        interval=0.25,
    )
    assert clock[0] <= 1.0

    clock[0] = 0.0
    assert not mod._wait_new_am_msgd(
        "0.8.39",
        "old-instance",
        total=1.0,
        interval=0.25,
    )
    assert clock[0] <= 1.0


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX sentinel test")
def test_sentinel_does_not_skip_when_amcodex_absent(tmp_path):
    """
    If amcodex is absent from bin/, _all_present() must return False even when
    the sentinel (PLUGIN_ROOT) matches — forcing regeneration.
    """
    meeting_home = tmp_path / "am"
    meeting_home.mkdir()
    plugin_root = _make_plugin_root(tmp_path)
    _make_venv(meeting_home)

    bin_dir = meeting_home / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("am", "am-msgd"):
        (bin_dir / name).write_text("#!/bin/sh\n")

    mod = _load_bootstrap(meeting_home, plugin_root)
    mod.DATA = meeting_home
    mod.BIN_LINK = bin_dir
    mod.VENV = meeting_home / "venv"
    mod.PLUGIN_ROOT = plugin_root

    mod.ensure_bin_wrappers()

    assert (bin_dir / "amcodex").exists(), (
        "_all_present() incorrectly skipped regen when amcodex was absent"
    )


# ---------------------------------------------------------------------------
# Obsolete mycodex command cleanup. IS_WINDOWS is force-patched here since the
# test host is macOS; all file operations exercised are OS-agnostic.
# ---------------------------------------------------------------------------

def test_windows_obsolete_mycodex_files_removed_on_regen(tmp_path):
    meeting_home = tmp_path / "am"
    meeting_home.mkdir()
    plugin_root = _make_plugin_root(tmp_path)
    _make_venv(meeting_home)

    bin_dir = meeting_home / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "mycodex").write_text("#!/bin/sh\necho old posix shim stuck on windows\n")
    (bin_dir / "mycodex.ps1").write_text("# old same-named shim\n")
    (bin_dir / "mycodex.cmd").write_text("@echo off\r\n")
    (bin_dir / "mycodex-impl.ps1").write_text("# old implementation\n")

    mod = _load_bootstrap(meeting_home, plugin_root)
    mod.IS_WINDOWS = True
    mod.DATA = meeting_home
    mod.BIN_LINK = bin_dir
    mod.VENV = meeting_home / "venv"
    mod.PLUGIN_ROOT = plugin_root

    mod.ensure_bin_wrappers()

    assert not (bin_dir / "mycodex").exists()
    assert not (bin_dir / "mycodex.ps1").exists()
    assert not (bin_dir / "mycodex.cmd").exists()
    assert not (bin_dir / "mycodex-impl.ps1").exists()
    assert (bin_dir / "amcodex.cmd").exists()
    assert (bin_dir / "amcodex-impl.ps1").exists()


def test_windows_obsolete_mycodex_files_swept_on_sentinel_match(tmp_path):
    """Once the sentinel matches and _all_present() is satisfied, the full
    regen path never runs again — the stale-file sweep must still fire on
    that early-return path, or obsolete mycodex files would persist
    forever on Windows."""
    meeting_home = tmp_path / "am"
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
    assert (bin_dir / "amcodex.cmd").exists()
    assert not (bin_dir / "mycodex.ps1").exists()

    # Simulate leftovers from older installs reappearing.
    (bin_dir / "mycodex").write_text("old posix shim stuck on windows\n")
    (bin_dir / "mycodex.ps1").write_text("old same-named shim stuck on windows\n")
    (bin_dir / "mycodex.cmd").write_text("@echo off\r\n")
    (bin_dir / "mycodex-impl.ps1").write_text("# old implementation\n")

    mod.ensure_bin_wrappers()  # second call: sentinel matches -> early-return path

    assert not (bin_dir / "mycodex").exists()
    assert not (bin_dir / "mycodex.ps1").exists()
    assert not (bin_dir / "mycodex.cmd").exists()
    assert not (bin_dir / "mycodex-impl.ps1").exists()
    assert (bin_dir / "amcodex.cmd").exists()
    assert (bin_dir / "amcodex-impl.ps1").exists()
