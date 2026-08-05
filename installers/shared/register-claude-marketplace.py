#!/usr/bin/env python3
"""Register this repository with Claude Code and install agent-meeting."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def installed_plugin(
    claude: str,
    plugin_id: str,
) -> tuple[bool | None, str | None]:
    """Return whether a Claude plugin is installed and its reported version."""
    listed = subprocess.run(
        [claude, "plugin", "list", "--json"],
        capture_output=True,
        text=True,
    )
    if listed.returncode != 0:
        return None, None
    try:
        plugins = json.loads(listed.stdout)
    except (json.JSONDecodeError, TypeError):
        return None, None
    for plugin in plugins:
        if plugin.get("id") == plugin_id:
            version = plugin.get("version")
            return True, str(version) if version else None
    return False, None


def source_plugin_version() -> str:
    manifest = REPOSITORY_ROOT / "agent-meeting/.claude-plugin/plugin.json"
    return str(json.loads(manifest.read_text(encoding="utf-8"))["version"])


def refresh_local_marketplace(claude: str) -> int:
    subprocess.run(
        [claude, "plugin", "marketplace", "remove", "woodor"],
        capture_output=True,
        text=True,
    )
    return subprocess.run(
        [
            claude,
            "plugin",
            "marketplace",
            "add",
            str(REPOSITORY_ROOT),
        ]
    ).returncode


def main() -> int:
    claude = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if not claude:
        print("ERROR: claude CLI not found", file=sys.stderr)
        return 1
    plugin_id = "agent-meeting@woodor"
    is_installed, installed_version = installed_plugin(claude, plugin_id)
    bundled_version = source_plugin_version()
    if is_installed and installed_version == bundled_version:
        print(
            f"Claude plugin already matches version {bundled_version}; "
            "skipping redundant update."
        )
        return 0
    print("Refreshing Claude marketplace from the release archive...", flush=True)
    refreshed = refresh_local_marketplace(claude)
    if refreshed != 0:
        return refreshed
    if is_installed:
        print(
            f"Updating Claude plugin {installed_version or 'unknown'} "
            f"-> {bundled_version}...",
            flush=True,
        )
        return subprocess.run(
            [claude, "plugin", "update", plugin_id]
        ).returncode
    return subprocess.run(
        [claude, "plugin", "install", plugin_id]
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
