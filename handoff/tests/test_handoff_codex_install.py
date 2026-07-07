"""Tests for handoff/codex/install-codex-hook.py — Windows-safety focus.

These assert the generated ~/.codex/config.toml is valid TOML and that the
hook `command` round-trips byte-for-byte, so the trusted_hash (computed over
HOOK_COMMAND) still matches what Codex parses back. The historical Windows
bugs were: (1) `python3` hard-coded (absent on Windows), (2) backslash paths
that are invalid TOML escape sequences.
"""

import importlib.util
import os
import sys
import tomllib
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN_ROOT / "codex" / "install-codex-hook.py"


def _load(codex_home: Path):
    """Import the install script fresh with CODEX_HOME pointed at a temp dir."""
    os.environ["CODEX_HOME"] = str(codex_home)
    spec = importlib.util.spec_from_file_location(
        f"handoff_codex_install_{codex_home.name}", SCRIPT
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_generated_config_is_valid_toml_and_roundtrips(tmp_path):
    mod = _load(tmp_path / "codex_home")
    # A project path with a space exercises the double-quoting in the command.
    project = tmp_path / "my project"
    project.mkdir()
    mod.install(str(project))

    raw = mod.CONFIG_PATH.read_text(encoding="utf-8")
    parsed = tomllib.loads(raw)  # raises on invalid TOML — the core regression check

    blocks = parsed["hooks"]["SessionStart"]
    assert len(blocks) == len(mod.MATCHERS)
    for block in blocks:
        cmd = block["hooks"][0]["command"]
        # Round-trip: TOML-parsed command equals the value the hash was fed.
        assert cmd == mod.HOOK_COMMAND


def test_hook_command_is_windows_safe():
    mod = _load(Path(os.environ.get("CODEX_HOME", "")) or Path.cwd())
    cmd = mod.HOOK_COMMAND
    # No backslashes — they break TOML basic strings; we use forward slashes.
    assert "\\" not in cmd
    if os.name == "nt":
        # Windows: codex splits the command on whitespace and ignores quotes, so
        # the command must be a bare "<python> <pickup>" — no `||`, no quotes.
        assert "||" not in cmd
        assert '"' not in cmd
        assert mod._PICKUP in cmd
        # exactly two space-separated, space-free tokens (python + pickup)
        assert len(cmd.split(" ")) == 2
    else:
        # POSIX: shell-executed fallback chain (python3 often absent on Windows,
        # but this branch is the POSIX form).
        assert "python3 " in cmd
        assert "py -3 " in cmd
        assert "python " in cmd
        assert cmd.count("||") == 2


def test_trusted_hash_matches_parsed_command(tmp_path):
    mod = _load(tmp_path / "codex_home")
    mod.install(None)
    parsed = tomllib.loads(mod.CONFIG_PATH.read_text(encoding="utf-8"))

    # Every registered handler's state hash must match a hash recomputed over
    # the command + matcher exactly as Codex would parse them back.
    state = parsed["hooks"]["state"]
    blocks = parsed["hooks"]["SessionStart"]
    for i, (matcher, block) in enumerate(zip(mod.MATCHERS, blocks)):
        cmd = block["hooks"][0]["command"]
        key = f"{mod.CONFIG_PATH}:session_start:{i}:0"
        expected = mod.compute_trusted_hash("session_start", matcher, cmd)
        assert state[key]["trusted_hash"] == expected
        assert state[key]["enabled"] is True


def test_idempotent_reinstall_stays_valid_and_unduplicated(tmp_path):
    mod = _load(tmp_path / "codex_home")
    project = tmp_path / "proj"
    project.mkdir()
    mod.install(str(project))
    mod.install(str(project))  # re-run exercises the re.sub replacement path

    parsed = tomllib.loads(mod.CONFIG_PATH.read_text(encoding="utf-8"))
    # No duplicated blocks despite two installs.
    assert len(parsed["hooks"]["SessionStart"]) == len(mod.MATCHERS)
    assert len(parsed["hooks"]["state"]) == len(mod.MATCHERS)
    assert len(parsed.get("projects", {})) == 1
    # Windows paths in the state keys survived re-write intact (no \\ collapse).
    for block in parsed["hooks"]["SessionStart"]:
        assert block["hooks"][0]["command"] == mod.HOOK_COMMAND


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-v"]))
