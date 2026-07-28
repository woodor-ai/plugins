#!/usr/bin/env python3
"""Compatibility entrypoint for the semantic legacy-layout migration."""

import os
import sys
from pathlib import Path


_SOURCE_ROOT = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SOURCE_ROOT))

from agent_meeting.installation import legacy_layout_migration


HOME = Path.home()
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or (HOME / ".codex"))
CONFIG_PATH = CODEX_HOME / "config.toml"
LEGACY_SCRIPT_NAME = legacy_layout_migration.LEGACY_CODEX_SCRIPT_NAME

toml_escape = legacy_layout_migration.toml_escape
trusted_hash = legacy_layout_migration.trusted_hash
remove_legacy_blocks = legacy_layout_migration.remove_legacy_blocks


def rewrite_session_start_state(content):
    return legacy_layout_migration.rewrite_session_start_state(
        content,
        config_path=CONFIG_PATH,
    )


def main():
    if not CONFIG_PATH.exists():
        print(f"No Codex config found at {CONFIG_PATH}; no legacy hook to remove")
        return
    content = CONFIG_PATH.read_text(encoding="utf-8")
    without_legacy = remove_legacy_blocks(content)
    if without_legacy == content:
        print(f"No legacy agent-meeting SessionStart hook found in {CONFIG_PATH}")
        return
    updated = rewrite_session_start_state(without_legacy)
    CONFIG_PATH.write_text(updated, encoding="utf-8")
    print(f"Removed legacy agent-meeting SessionStart hook from {CONFIG_PATH}")


if __name__ == "__main__":
    main()
