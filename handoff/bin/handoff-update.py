#!/usr/bin/env python3
"""Update every installed handoff host integration through its public CLI."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Sequence


UPDATER_VERSION = "0.6.4"
PLUGIN_SELECTOR = "handoff@woodor"
TARGET_CLAUDE_CODE = "claude-code"
TARGET_CODEX = "codex"
ALL_TARGETS = (TARGET_CLAUDE_CODE, TARGET_CODEX)
VERSION_PATTERN = re.compile(r"\b\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?\b")


@dataclass(frozen=True)
class Installation:
    target: str
    version: str


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="handoff-update",
        description="Update handoff in every installed AI-platform integration.",
    )
    parser.add_argument(
        "--target",
        action="append",
        choices=ALL_TARGETS,
        help="update this platform only; repeat to update more than one",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="show installed integrations and versions without updating",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {UPDATER_VERSION}",
    )
    return parser


def _command_argv(command: str, arguments: Sequence[str]) -> list[str] | None:
    executable = shutil.which(command)
    if executable is None:
        return None
    if sys.platform.startswith("win") and executable.lower().endswith((".cmd", ".bat")):
        command_line = subprocess.list2cmdline([executable, *arguments])
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", command_line]
    return [executable, *arguments]


def invoke_cli(
    command: str,
    arguments: Sequence[str],
    *,
    capture_output: bool,
) -> subprocess.CompletedProcess[str]:
    argv = _command_argv(command, arguments)
    if argv is None:
        return subprocess.CompletedProcess([command, *arguments], 127, "", "command not found")
    return subprocess.run(
        argv,
        text=True,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.STDOUT if capture_output else None,
        check=False,
    )


def _codex_version(output: str) -> str | None:
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped.startswith(f"{PLUGIN_SELECTOR} "):
            continue
        if "not installed" in stripped:
            return None
        match = VERSION_PATTERN.search(stripped)
        return match.group(0) if match else "unknown"
    return None


def _claude_version(output: str) -> str | None:
    match = re.search(
        rf"{re.escape(PLUGIN_SELECTOR)}(?:.|\n){{0,240}}?Version:\s*([^\s]+)",
        output,
    )
    return match.group(1) if match else None


def probe_target(
    target: str,
    *,
    invoke: Callable[..., subprocess.CompletedProcess[str]] = invoke_cli,
) -> Installation | None:
    command = "claude" if target == TARGET_CLAUDE_CODE else "codex"
    result = invoke(command, ["plugin", "list"], capture_output=True)
    if result.returncode != 0:
        return None
    output = result.stdout or ""
    version = _claude_version(output) if target == TARGET_CLAUDE_CODE else _codex_version(output)
    return Installation(target, version) if version is not None else None


def installed_integrations(
    *,
    probe: Callable[[str], Installation | None] = probe_target,
) -> tuple[Installation, ...]:
    return tuple(
        installation
        for target in ALL_TARGETS
        if (installation := probe(target)) is not None
    )


def update_commands(target: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if target == TARGET_CLAUDE_CODE:
        return (
            ("claude", ("plugin", "marketplace", "update", "woodor")),
            ("claude", ("plugin", "update", PLUGIN_SELECTOR)),
        )
    if target == TARGET_CODEX:
        return (
            ("codex", ("plugin", "marketplace", "upgrade", "woodor")),
            ("codex", ("plugin", "add", PLUGIN_SELECTOR)),
        )
    raise ValueError(f"unknown target: {target}")


def update_target(
    target: str,
    *,
    invoke: Callable[..., subprocess.CompletedProcess[str]] = invoke_cli,
) -> bool:
    print(f"Updating {target}...", flush=True)
    for command, arguments in update_commands(target):
        result = invoke(command, arguments, capture_output=False)
        if result.returncode != 0:
            print(f"ERROR: {command} exited with status {result.returncode}", file=sys.stderr)
            return False
    return True


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    installed = installed_integrations()
    by_target = {item.target: item for item in installed}

    if args.check:
        if not installed:
            print("installed targets: none")
            return 0
        print("installed targets:")
        for item in installed:
            print(f"  {item.target}: {item.version}")
        return 0

    targets = tuple(dict.fromkeys(args.target or tuple(by_target)))
    if not targets:
        print(f"ERROR: {PLUGIN_SELECTOR} is not installed in Claude Code or Codex", file=sys.stderr)
        return 1
    missing = [target for target in targets if target not in by_target]
    if missing:
        print(
            "ERROR: handoff is not installed for: " + ", ".join(missing),
            file=sys.stderr,
        )
        return 1

    for target in targets:
        if not update_target(target):
            return 1

    refreshed = {item.target: item for item in installed_integrations()}
    for target in targets:
        version = refreshed.get(target)
        print(f"Updated {target}: {version.version if version else 'unknown'}")
    print("Restart the Codex app or app-server, or restart Claude Code, to load the update.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
