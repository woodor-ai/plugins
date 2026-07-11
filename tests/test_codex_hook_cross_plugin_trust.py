"""Regression tests for the cross-plugin Codex trust-index collision bug.

Root cause (Windows, real machine): agent-meeting's and handoff's
codex/install-codex-hook.py each maintain their [[hooks.SessionStart]] blocks
by deleting their own old blocks and re-appending fresh ones. Codex keys hook
trust by a block's ORDINAL POSITION in config.toml
(`hooks.state."<config>:session_start:<block-index>:0"`), not by which plugin
wrote the block. Running the unified installer (install-codex.py) installs
agent-meeting THEN handoff: handoff's install reorders/appends AFTER
agent-meeting's blocks already occupy positions 0-3, and either installer's
prior per-plugin index bookkeeping goes stale the moment the other one edits
the file — so trust entries end up keyed to the wrong blocks and codex
silently skips all 8 hooks (MISMATCH on every trusted_hash lookup).

The fix: both installers, after settling their own hook blocks, do a FULL
RESCAN of every [[hooks.SessionStart]] block in the final file and rewrite
ALL session_start trust entries from scratch keyed by actual position
(rewrite_session_start_state_entries). This must be correct regardless of
install order, and idempotent under reinstall/uninstall of either plugin.
"""

import importlib.util
import os
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
AM_SCRIPT = REPO / "agent-meeting" / "codex" / "install-codex-hook.py"
HANDOFF_SCRIPT = REPO / "handoff" / "codex" / "install-codex-hook.py"


def _load(script: Path, codex_home: Path, meeting_home: Path, tag: str):
    """Import an install-codex-hook.py fresh, with env vars pinning it at the
    shared tmp CODEX_HOME (and, for agent-meeting, a tmp MEETING_HOME)."""
    os.environ["CODEX_HOME"] = str(codex_home)
    os.environ["MEETING_HOME"] = str(meeting_home)
    spec = importlib.util.spec_from_file_location(f"codex_hook_{tag}", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _assert_all_session_start_entries_correct(config_path: Path):
    """The core regression check: every [[hooks.SessionStart]] block's trust
    entry must be keyed to ITS OWN actual position and match a hash recomputed
    over that block's own matcher + command."""
    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    blocks = parsed["hooks"]["SessionStart"]
    state = parsed["hooks"]["state"]

    assert len(blocks) == 8, f"expected 8 SessionStart blocks (4+4), got {len(blocks)}"

    session_start_keys = {
        k for k in state if f"{config_path}:session_start:" in k
    }
    assert len(session_start_keys) == 8, (
        f"expected exactly 8 session_start state entries, got {len(session_start_keys)}: "
        f"{sorted(session_start_keys)}"
    )

    for i, block in enumerate(blocks):
        matcher = block["matcher"]
        command = block["hooks"][0]["command"]
        key = f"{config_path}:session_start:{i}:0"
        assert key in state, f"missing trust entry for block {i} ({matcher} / {command})"
        # sha256 algorithm is identical in both installers; either's function works.
        expected = _sha256_identity("session_start", matcher, command)
        assert state[key]["trusted_hash"] == expected, (
            f"block {i} (matcher={matcher!r}) trust hash mismatch — codex will "
            f"silently skip this hook"
        )
        assert state[key]["enabled"] is True


def _sha256_identity(event_name: str, matcher: str, command: str) -> str:
    import hashlib
    import json

    identity = {
        "event_name": event_name,
        "hooks": [{"async": False, "command": command, "timeout": 600, "type": "command"}],
        "matcher": matcher,
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_unified_install_order_agent_meeting_then_handoff(tmp_path):
    """Reproduces install-codex.py's actual order: agent-meeting first, handoff
    second. Before the fix, handoff's install clobbered/misaligned
    agent-meeting's trust indices (and vice versa) -> 8/8 MISMATCH."""
    codex_home = tmp_path / "codex_home"
    meeting_home = tmp_path / "meeting_home"

    am_mod = _load(AM_SCRIPT, codex_home, meeting_home, "am1")
    am_mod.install(None)

    handoff_mod = _load(HANDOFF_SCRIPT, codex_home, meeting_home, "ho1")
    handoff_mod.install(None)

    _assert_all_session_start_entries_correct(am_mod.CONFIG_PATH)


def test_unified_install_order_handoff_then_agent_meeting(tmp_path):
    """Reverse install order must be equally self-consistent."""
    codex_home = tmp_path / "codex_home"
    meeting_home = tmp_path / "meeting_home"

    handoff_mod = _load(HANDOFF_SCRIPT, codex_home, meeting_home, "ho2")
    handoff_mod.install(None)

    am_mod = _load(AM_SCRIPT, codex_home, meeting_home, "am2")
    am_mod.install(None)

    _assert_all_session_start_entries_correct(am_mod.CONFIG_PATH)


def test_single_plugin_idempotent_rerun_after_other_installed(tmp_path):
    """Re-running one plugin's installer (idempotent path) after the other
    plugin already installed must re-settle ALL 8 entries correctly, even
    though the rerun moves that plugin's blocks to the end of the file."""
    codex_home = tmp_path / "codex_home"
    meeting_home = tmp_path / "meeting_home"

    am_mod = _load(AM_SCRIPT, codex_home, meeting_home, "am3")
    am_mod.install(None)

    handoff_mod = _load(HANDOFF_SCRIPT, codex_home, meeting_home, "ho3")
    handoff_mod.install(None)

    # Re-run agent-meeting's installer: it deletes+re-appends its own 4
    # blocks, moving them after handoff's blocks. The other plugin's (handoff)
    # trust entries must be recomputed at their new (shifted) positions too.
    am_mod.install(None)

    _assert_all_session_start_entries_correct(am_mod.CONFIG_PATH)


def test_uninstall_one_plugin_leaves_other_correctly_indexed(tmp_path):
    """Uninstalling one plugin's hook must recompute the remaining plugin's
    trust entries at their shifted (now-lower) positions."""
    codex_home = tmp_path / "codex_home"
    meeting_home = tmp_path / "meeting_home"

    am_mod = _load(AM_SCRIPT, codex_home, meeting_home, "am4")
    am_mod.install(None)

    handoff_mod = _load(HANDOFF_SCRIPT, codex_home, meeting_home, "ho4")
    handoff_mod.install(None)

    # Uninstall agent-meeting: only handoff's 4 blocks should remain, now at
    # positions 0-3 (they were 4-7 while agent-meeting's blocks preceded them).
    am_mod.uninstall()

    parsed = tomllib.loads(am_mod.CONFIG_PATH.read_text(encoding="utf-8"))
    blocks = parsed["hooks"]["SessionStart"]
    state = parsed["hooks"]["state"]
    assert len(blocks) == 4

    session_start_keys = {
        k for k in state if f"{am_mod.CONFIG_PATH}:session_start:" in k
    }
    assert len(session_start_keys) == 4

    for i, block in enumerate(blocks):
        matcher = block["matcher"]
        command = block["hooks"][0]["command"]
        key = f"{am_mod.CONFIG_PATH}:session_start:{i}:0"
        assert key in state
        expected = _sha256_identity("session_start", matcher, command)
        assert state[key]["trusted_hash"] == expected


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-v"]))
