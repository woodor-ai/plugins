"""Tests for save-money/codex/install-codex-hook.py — Windows-safety focus.

Mirrors handoff/tests/test_codex_install.py: assert the generated
~/.codex/config.toml is valid TOML, the PostToolUse hook `command` round-trips,
and the trusted_hash matches the parsed command. Guards against the two known
Windows bugs (hard-coded `python3`, backslash paths in TOML strings).
"""

import importlib.util
import os
import sys
import tomllib
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN_ROOT / "codex" / "install-codex-hook.py"


def _load(codex_home: Path):
    os.environ["CODEX_HOME"] = str(codex_home)
    spec = importlib.util.spec_from_file_location(
        f"savemoney_codex_install_{codex_home.name}", SCRIPT
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_generated_config_is_valid_toml_and_roundtrips(tmp_path):
    mod = _load(tmp_path / "codex_home")
    project = tmp_path / "my project"
    project.mkdir()
    mod.install(str(project))

    parsed = tomllib.loads(mod.CONFIG_PATH.read_text(encoding="utf-8"))
    block = parsed["hooks"]["PostToolUse"][0]
    assert block["matcher"] == mod.MATCHER
    cmd = block["hooks"][0]["command"]
    assert cmd == mod.HOOK_COMMAND


def test_hook_command_is_windows_safe():
    mod = _load(Path(os.environ.get("CODEX_HOME", "")) or Path.cwd())
    cmd = mod.HOOK_COMMAND
    assert "python3 " in cmd
    assert "py -3 " in cmd
    assert "python " in cmd
    assert cmd.count("||") == 2
    assert "\\" not in cmd


def test_trusted_hash_matches_parsed_command(tmp_path):
    mod = _load(tmp_path / "codex_home")
    mod.install(None)
    parsed = tomllib.loads(mod.CONFIG_PATH.read_text(encoding="utf-8"))

    cmd = parsed["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
    key = f"{mod.CONFIG_PATH}:{mod.EVENT_NAME}:0:0"
    expected = mod.compute_trusted_hash(mod.EVENT_NAME, mod.MATCHER, cmd)
    state = parsed["hooks"]["state"][key]
    assert state["trusted_hash"] == expected
    assert state["enabled"] is True


def test_idempotent_reinstall_stays_valid_and_unduplicated(tmp_path):
    mod = _load(tmp_path / "codex_home")
    project = tmp_path / "proj"
    project.mkdir()
    mod.install(str(project))
    mod.install(str(project))  # re-run exercises the re.sub replacement path

    parsed = tomllib.loads(mod.CONFIG_PATH.read_text(encoding="utf-8"))
    assert len(parsed["hooks"]["PostToolUse"]) == 1
    assert len(parsed["hooks"]["state"]) == 1
    assert len(parsed.get("projects", {})) == 1
    cmd = parsed["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
    assert cmd == mod.HOOK_COMMAND


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-v"]))
