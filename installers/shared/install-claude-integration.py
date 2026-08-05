#!/usr/bin/env python3
"""Install agent-meeting Claude skills and hooks without a marketplace."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "agent-meeting" / "src"))

from agent_meeting.installation import claude_integration


PLUGIN_ID = "agent-meeting@woodor"


def _command_detail(result: subprocess.CompletedProcess) -> str:
    return (
        result.stderr
        or result.stdout
        or "no diagnostic output"
    ).strip()


def _json_command(command: list[str]) -> object:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"{' '.join(command[1:])} failed "
            f"(exit {result.returncode}): {_command_detail(result)}"
        )
    try:
        return json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as error:
        raise RuntimeError(
            f"{' '.join(command[1:])} returned invalid JSON: {error}"
        ) from error


def _plugin_version() -> str:
    manifest = REPOSITORY_ROOT / "agent-meeting/.claude-plugin/plugin.json"
    try:
        return str(json.loads(manifest.read_text(encoding="utf-8"))["version"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise RuntimeError(
            f"bundled Claude integration manifest is invalid: {error}"
        ) from error


def _is_disposable_source(entry: dict) -> bool:
    if entry.get("source") != "directory":
        return False
    source = str(entry.get("path") or entry.get("installLocation") or "")
    normalized = source.replace("\\", "/").lower().rstrip("/")
    return (
        "/agent-meeting-install-" in normalized
        or normalized.endswith("/.agent-meeting/updates/plugins")
    )


def remove_legacy_marketplace_integration(claude: str) -> None:
    plugins = _json_command([claude, "plugin", "list", "--json"])
    if not isinstance(plugins, list):
        raise RuntimeError("Claude plugin list must be a JSON array")
    if any(item.get("id") == PLUGIN_ID for item in plugins):
        removed = subprocess.run(
            [claude, "plugin", "uninstall", PLUGIN_ID, "-y"],
            capture_output=True,
            text=True,
        )
        if removed.returncode != 0:
            raise RuntimeError(
                "could not remove the legacy Claude marketplace plugin "
                f"(exit {removed.returncode}): {_command_detail(removed)}"
            )
        print("Removed legacy Claude marketplace plugin registration.")
        plugins = [item for item in plugins if item.get("id") != PLUGIN_ID]

    marketplaces = _json_command(
        [claude, "plugin", "marketplace", "list", "--json"]
    )
    if not isinstance(marketplaces, list):
        raise RuntimeError("Claude marketplace list must be a JSON array")
    woodor = next(
        (entry for entry in marketplaces if entry.get("name") == "woodor"),
        None,
    )
    if not isinstance(woodor, dict) or not _is_disposable_source(woodor):
        return
    remaining = [
        str(item.get("id"))
        for item in plugins
        if str(item.get("id") or "").endswith("@woodor")
    ]
    if remaining:
        raise RuntimeError(
            "cannot remove the disposable woodor marketplace while these "
            f"plugins still use it: {', '.join(remaining)}"
        )
    removed = subprocess.run(
        [claude, "plugin", "marketplace", "remove", "woodor"],
        capture_output=True,
        text=True,
    )
    if removed.returncode != 0:
        raise RuntimeError(
            "could not remove the disposable Claude marketplace "
            f"(exit {removed.returncode}): {_command_detail(removed)}"
        )
    print("Removed disposable Claude marketplace registration.")


def install(*, claude_home: Path, meeting_home: Path) -> None:
    version = _plugin_version()
    claude_integration.install_skills(
        source_root=REPOSITORY_ROOT,
        claude_home=claude_home,
        version=version,
    )
    settings_path = claude_home / "settings.json"
    claude_integration.install_user_configuration(
        settings_path=settings_path,
        meeting_home=meeting_home,
    )
    session_start = claude_integration.session_start_executable(meeting_home)
    refreshed = subprocess.run(
        [str(session_start)],
        capture_output=True,
        text=True,
    )
    if refreshed.returncode != 0:
        raise RuntimeError(
            "Claude SessionStart integration failed "
            f"(exit {refreshed.returncode}): {_command_detail(refreshed)}"
        )

    claude = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if claude:
        remove_legacy_marketplace_integration(claude)
    print(
        f"Installed agent-meeting Claude integration {version} at "
        f"{claude_home}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--claude-home",
        type=Path,
        default=Path(
            os.environ.get("CLAUDE_CONFIG_DIR")
            or (Path.home() / ".claude")
        ),
    )
    parser.add_argument(
        "--meeting-home",
        type=Path,
        default=Path(
            os.environ.get("MEETING_HOME")
            or (Path.home() / ".agent-meeting")
        ),
    )
    args = parser.parse_args(argv)
    try:
        install(
            claude_home=args.claude_home.resolve(),
            meeting_home=args.meeting_home.resolve(),
        )
    except (OSError, RuntimeError) as error:
        print(
            f"ERROR: Claude integration installation failed: {error}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
