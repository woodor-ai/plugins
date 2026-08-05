#!/usr/bin/env python3
"""Register this repository with Codex and install agent-meeting."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def marketplace_is_registered(codex: str, marketplace: str) -> bool | None:
    """Return whether a marketplace is registered, or ``None`` if unknown.

    A failed or malformed listing is kept distinct so the caller does not
    remove a marketplace whose registration state cannot be established.
    """
    listed = subprocess.run(
        [codex, "plugin", "marketplace", "list", "--json"],
        capture_output=True,
        text=True,
    )
    if listed.returncode != 0:
        return None
    try:
        marketplaces = json.loads(listed.stdout)["marketplaces"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    return any(item.get("name") == marketplace for item in marketplaces)


def plugin_is_installed(codex: str, plugin_id: str) -> bool | None:
    """Return whether a plugin is installed, or ``None`` if unknown."""
    listed = subprocess.run(
        [codex, "plugin", "list", "--json"],
        capture_output=True,
        text=True,
    )
    if listed.returncode != 0:
        return None
    try:
        payload = json.loads(listed.stdout)
        plugins = payload.get("installed") or payload.get("plugins") or []
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    return any(
        plugin.get("pluginId") == plugin_id and plugin.get("installed")
        for plugin in plugins
    )


def installed_plugin_version(codex: str, plugin_id: str) -> str | None:
    """Return an installed plugin's version, if Codex can report one."""
    listed = subprocess.run(
        [codex, "plugin", "list", "--json"],
        capture_output=True,
        text=True,
    )
    if listed.returncode != 0:
        return None
    try:
        payload = json.loads(listed.stdout)
        plugins = payload.get("installed") or payload.get("plugins") or []
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    for plugin in plugins:
        if plugin.get("pluginId") == plugin_id and plugin.get("installed"):
            version = plugin.get("version")
            return str(version) if version else None
    return None


def source_plugin_version() -> str | None:
    """Return the plugin version bundled with this checkout."""
    manifest = REPOSITORY_ROOT / "agent-meeting/.codex-plugin/plugin.json"
    try:
        version = json.loads(manifest.read_text(encoding="utf-8"))["version"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return None
    return str(version) if version else None


def refresh_local_marketplace(codex: str, marketplace: str) -> int:
    """Refresh a marketplace from the disposable release archive."""
    registered = marketplace_is_registered(codex, marketplace)
    if registered is None:
        return 1
    if registered:
        removed = subprocess.run(
            [codex, "plugin", "marketplace", "remove", marketplace]
        )
        if removed.returncode != 0:
            return removed.returncode
    return subprocess.run(
        [
            codex,
            "plugin",
            "marketplace",
            "add",
            str(REPOSITORY_ROOT),
        ]
    ).returncode


def main() -> int:
    codex = os.environ.get("CODEX_BIN") or shutil.which("codex")
    if not codex:
        print("ERROR: codex CLI not found", file=sys.stderr)
        return 1
    plugin_id = "agent-meeting@woodor"
    installed_version = installed_plugin_version(codex, plugin_id)
    bundled_version = source_plugin_version()
    if installed_version and installed_version == bundled_version:
        print(
            f"Codex plugin already matches version {bundled_version}; "
            "skipping marketplace refresh."
        )
        return 0
    print("Refreshing Codex marketplace from the release archive...", flush=True)
    refreshed = refresh_local_marketplace(codex, "woodor")
    if refreshed != 0:
        return refreshed
    if plugin_is_installed(codex, plugin_id) is True:
        print("Codex plugin already installed; skipping redundant reinstall.")
        return 0
    return subprocess.run(
        [codex, "plugin", "add", plugin_id]
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
