#!/usr/bin/env python3
"""Register this repository with Claude Code and install agent-meeting."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _command_failure(stage: str, result: subprocess.CompletedProcess) -> None:
    detail = (
        getattr(result, "stderr", "")
        or getattr(result, "stdout", "")
        or "no diagnostic output"
    ).strip()
    print(
        f"ERROR: {stage} failed (exit {result.returncode}): {detail}",
        file=sys.stderr,
    )


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
        _command_failure("Claude plugin list", listed)
        return None, None
    try:
        plugins = json.loads(listed.stdout)
    except (json.JSONDecodeError, TypeError) as error:
        print(
            f"ERROR: Claude plugin list returned invalid JSON: {error}",
            file=sys.stderr,
        )
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
    removed = subprocess.run(
        [claude, "plugin", "marketplace", "remove", "woodor"],
        capture_output=True,
        text=True,
    )
    added = subprocess.run(
        [
            claude,
            "plugin",
            "marketplace",
            "add",
            str(REPOSITORY_ROOT),
        ]
    )
    if added.returncode != 0:
        if removed.returncode != 0:
            _command_failure("Claude marketplace remove", removed)
        print(
            f"ERROR: Claude marketplace add failed (exit {added.returncode})",
            file=sys.stderr,
        )
    return added.returncode


def main() -> int:
    claude = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if not claude:
        print("ERROR: claude CLI not found", file=sys.stderr)
        return 1
    plugin_id = "agent-meeting@woodor"
    is_installed, installed_version = installed_plugin(claude, plugin_id)
    if is_installed is None:
        return 1
    try:
        bundled_version = source_plugin_version()
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        print(
            f"ERROR: bundled Claude plugin manifest is invalid: {error}",
            file=sys.stderr,
        )
        return 1
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
        updated = subprocess.run(
            [claude, "plugin", "update", plugin_id]
        )
        if updated.returncode != 0:
            print(
                f"ERROR: Claude plugin update failed (exit {updated.returncode})",
                file=sys.stderr,
            )
        return updated.returncode
    installed = subprocess.run(
        [claude, "plugin", "install", plugin_id]
    )
    if installed.returncode != 0:
        print(
            f"ERROR: Claude plugin install failed (exit {installed.returncode})",
            file=sys.stderr,
        )
    return installed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
