"""Idempotent migration of supported pre-0.15 agent-meeting artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path


LEGACY_CODEX_SCRIPT_NAME = "codex-register.py"
LEGACY_LAUNCHD_SERVICES = (
    "com.tommy.agent-meeting",
    "com.tommy.agent-meeting.amctl",
)
LEGACY_WINDOWS_TASKS = (
    "agent-meeting-daemon",
    "agent-meeting-amctl",
)
LEGACY_RUNTIME_FILES = (
    "bin/amctl",
    "bin/amctl.cmd",
    "amctl.stopped",
)
LEGACY_WINDOWS_STARTUP_FILES = (
    "agent-meeting-daemon.cmd",
    "agent-meeting-amctl.cmd",
)


def toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def trusted_hash(matcher, hooks) -> str:
    identity = {
        "event_name": "session_start",
        "hooks": [
            {
                "async": bool(hook.get("async", False)),
                "command": hook["command"],
                "timeout": int(hook.get("timeout", 600)),
                "type": hook.get("type", "command"),
            }
            for hook in hooks
        ],
        "matcher": matcher,
    }
    canonical = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def remove_legacy_blocks(content: str) -> str:
    pattern = re.compile(
        r"\[\[hooks\.SessionStart\]\][^\[]*"
        r"(?:\[\[hooks\.SessionStart\.hooks\]\][^\[]*)*",
        re.DOTALL,
    )
    return pattern.sub(
        lambda match: (
            ""
            if LEGACY_CODEX_SCRIPT_NAME in match.group(0)
            else match.group(0)
        ),
        content,
    )


def rewrite_session_start_state(
    content: str,
    *,
    config_path: Path,
) -> str:
    config_key = toml_escape(str(config_path))
    state_pattern = re.compile(
        r'\[hooks\.state\."'
        + re.escape(config_key)
        + r':session_start:\d+:\d+"\][^\[]*',
        re.DOTALL,
    )
    content = state_pattern.sub("", content)
    parsed = tomllib.loads(content)
    blocks = parsed.get("hooks", {}).get("SessionStart", [])
    if not blocks:
        return content

    entries = []
    for index, block in enumerate(blocks):
        matcher = block.get("matcher", "")
        hooks = block.get("hooks", [])
        block_hash = trusted_hash(matcher, hooks)
        for hook_index, _hook in enumerate(hooks):
            key = f"{config_path}:session_start:{index}:{hook_index}"
            entries.append(
                f'[hooks.state."{toml_escape(key)}"]\n'
                "enabled = true\n"
                f'trusted_hash = "{block_hash}"\n'
            )
    insert_at = len(content)
    match = re.search(r"^\[hooks\.state\.", content, re.MULTILINE)
    if match:
        insert_at = match.start()
    block_text = "\n".join(entries) + "\n"
    if content[:insert_at] and not content[:insert_at].endswith("\n\n"):
        block_text = "\n" + block_text
    return content[:insert_at] + block_text + content[insert_at:]


def remove_legacy_codex_hook(config_path: Path) -> bool:
    if not config_path.exists():
        return False
    content = config_path.read_text(encoding="utf-8")
    without_legacy = remove_legacy_blocks(content)
    if without_legacy == content:
        return False
    updated = rewrite_session_start_state(
        without_legacy,
        config_path=config_path,
    )
    config_path.write_text(updated, encoding="utf-8")
    return True


def _unlink_exact(path: Path, removed: list[str]) -> None:
    try:
        path.unlink()
        removed.append(str(path))
    except FileNotFoundError:
        pass


def migrate_legacy_layout(
    *,
    meeting_home: Path,
    codex_home: Path,
    user_home: Path,
    platform_name: str,
    temp_dir: Path | None = None,
    run_command=subprocess.run,
) -> list[str]:
    """Remove exact legacy artifacts and return the paths/actions removed."""
    removed: list[str] = []
    temp_dir = temp_dir or Path(tempfile.gettempdir())

    for relative_path in LEGACY_RUNTIME_FILES:
        _unlink_exact(meeting_home / relative_path, removed)
    _unlink_exact(temp_dir / "amctl.pid", removed)

    codex_config = codex_home / "config.toml"
    if remove_legacy_codex_hook(codex_config):
        removed.append(f"legacy Codex hook in {codex_config}")

    if platform_name == "darwin":
        launch_agents = user_home / "Library" / "LaunchAgents"
        uid = os.getuid()
        for label in LEGACY_LAUNCHD_SERVICES:
            run_command(
                ["launchctl", "bootout", f"gui/{uid}/{label}"],
                capture_output=True,
            )
            _unlink_exact(launch_agents / f"{label}.plist", removed)
    elif platform_name.startswith("win"):
        startup_dir = (
            user_home
            / "AppData"
            / "Roaming"
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
            / "Startup"
        )
        for task_name in LEGACY_WINDOWS_TASKS:
            run_command(
                ["schtasks", "/Delete", "/TN", task_name, "/F"],
                capture_output=True,
            )
        for filename in LEGACY_WINDOWS_STARTUP_FILES:
            _unlink_exact(startup_dir / filename, removed)

    return removed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrate-agent-meeting-legacy-layout.py"
    )
    parser.add_argument("--meeting-home", type=Path)
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument("--user-home", type=Path)
    parser.add_argument(
        "--codex-hook-only",
        action="store_true",
        help="only remove the obsolete per-session Codex hook",
    )
    return parser


def main(argv=None) -> int:
    arguments = build_parser().parse_args(argv)
    user_home = arguments.user_home or Path.home()
    meeting_home = arguments.meeting_home or Path(
        os.environ.get("MEETING_HOME") or (user_home / ".agent-meeting")
    )
    codex_home = arguments.codex_home or Path(
        os.environ.get("CODEX_HOME") or (user_home / ".codex")
    )
    codex_config = codex_home / "config.toml"

    if arguments.codex_hook_only:
        changed = remove_legacy_codex_hook(codex_config)
        if changed:
            print(
                "Removed legacy agent-meeting SessionStart hook from "
                f"{codex_config}"
            )
        else:
            print(
                "No legacy agent-meeting SessionStart hook found in "
                f"{codex_config}"
            )
        return 0

    removed = migrate_legacy_layout(
        meeting_home=meeting_home,
        codex_home=codex_home,
        user_home=user_home,
        platform_name=sys.platform,
    )
    if removed:
        print("Migrated legacy agent-meeting layout:")
        for item in removed:
            print(f"- {item}")
    else:
        print("No supported legacy agent-meeting artifacts found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
