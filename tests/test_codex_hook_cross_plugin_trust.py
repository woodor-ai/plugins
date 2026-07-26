"""Cross-plugin trust tests for removing agent-meeting's obsolete Codex hook."""

import importlib.util
import os
import tomllib
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
REMOVER_SCRIPT = (
    REPO / "agent-meeting" / "codex" / "remove-legacy-codex-hook.py"
)
HANDOFF_SCRIPT = REPO / "handoff" / "codex" / "install-codex-hook.py"


def load(script, codex_home, tag):
    os.environ["CODEX_HOME"] = str(codex_home)
    spec = importlib.util.spec_from_file_location(tag, script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def legacy_blocks():
    command = "/venv/python /plugin/codex/codex-register.py"
    return "\n".join(
        f"""
[[hooks.SessionStart]]
matcher = "{matcher}"

[[hooks.SessionStart.hooks]]
type = "command"
command = "{command}"
"""
        for matcher in ("startup", "resume", "clear", "compact")
    )


def assert_handoff_trust_is_consistent(config_path):
    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    blocks = parsed["hooks"]["SessionStart"]
    state = parsed["hooks"]["state"]
    assert len(blocks) == 4
    assert all("codex-handoff-pickup.py" in block["hooks"][0]["command"] for block in blocks)
    for index, block in enumerate(blocks):
        key = f"{config_path}:session_start:{index}:0"
        assert key in state
        assert state[key]["enabled"] is True


def test_remover_is_noop_when_only_handoff_hook_exists(tmp_path):
    codex_home = tmp_path / "codex"
    handoff = load(HANDOFF_SCRIPT, codex_home, "handoff_noop")
    handoff.install(None)
    before = handoff.CONFIG_PATH.read_text(encoding="utf-8")

    remover = load(REMOVER_SCRIPT, codex_home, "meeting_remover_noop")
    remover.main()

    assert remover.CONFIG_PATH.read_text(encoding="utf-8") == before
    assert_handoff_trust_is_consistent(remover.CONFIG_PATH)


def test_remover_reindexes_handoff_after_legacy_blocks_are_deleted(tmp_path):
    codex_home = tmp_path / "codex"
    handoff = load(HANDOFF_SCRIPT, codex_home, "handoff_reindex")
    handoff.install(None)
    content = handoff.CONFIG_PATH.read_text(encoding="utf-8")
    insert_at = content.index("[[hooks.SessionStart]]")
    content = content[:insert_at] + legacy_blocks() + "\n" + content[insert_at:]
    handoff.CONFIG_PATH.write_text(content, encoding="utf-8")

    remover = load(REMOVER_SCRIPT, codex_home, "meeting_remover_reindex")
    remover.main()

    updated = remover.CONFIG_PATH.read_text(encoding="utf-8")
    assert "codex-register.py" not in updated
    assert_handoff_trust_is_consistent(remover.CONFIG_PATH)


def test_remover_preserves_plugin_owned_hook_state(tmp_path):
    codex_home = tmp_path / "codex"
    handoff = load(HANDOFF_SCRIPT, codex_home, "handoff_plugin_state")
    handoff.install(None)
    content = handoff.CONFIG_PATH.read_text(encoding="utf-8")
    insert_at = content.index("[[hooks.SessionStart]]")
    content = content[:insert_at] + legacy_blocks() + "\n" + content[insert_at:]
    content += """

[hooks.state."handoff@woodor:hooks/hooks.json:session_start:0:0"]
trusted_hash = "plugin-owned"
"""
    handoff.CONFIG_PATH.write_text(content, encoding="utf-8")

    remover = load(REMOVER_SCRIPT, codex_home, "meeting_remover_plugin_state")
    remover.main()

    parsed = tomllib.loads(remover.CONFIG_PATH.read_text(encoding="utf-8"))
    assert (
        parsed["hooks"]["state"][
            "handoff@woodor:hooks/hooks.json:session_start:0:0"
        ]["trusted_hash"]
        == "plugin-owned"
    )
