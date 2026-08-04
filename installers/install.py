#!/usr/bin/env python3
"""Install agent-meeting for Claude Code, Codex, or both clients."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TARGET_CLAUDE_CODE = "claude-code"
TARGET_CODEX = "codex"
TARGET_ALL = "all"


def _run_python(
    source_root: Path,
    relative_script: str,
    *arguments: str,
) -> None:
    subprocess.run(
        [
            sys.executable,
            str(source_root / relative_script),
            *arguments,
        ],
        check=True,
    )


def install(
    *,
    source_root: Path,
    meeting_home: Path,
    target: str,
    control_url: str = "",
    enable_full_automation: bool = False,
) -> None:
    configure_codex = target in (TARGET_CODEX, TARGET_ALL)
    configure_claude = target in (TARGET_CLAUDE_CODE, TARGET_ALL)
    package_arguments = [
        "--source-root",
        str(source_root),
        "--meeting-home",
        str(meeting_home),
    ]
    if configure_codex:
        package_arguments.append("--configure-codex")
        if control_url:
            package_arguments.extend(("--control-url", control_url))
        if enable_full_automation:
            package_arguments.append("--enable-full-automation")
    _run_python(
        source_root,
        "installers/shared/install-agent-meeting-package.py",
        *package_arguments,
    )
    _run_python(
        source_root,
        "installers/shared/migrate-agent-meeting-legacy-layout.py",
        "--meeting-home",
        str(meeting_home),
    )
    if configure_claude:
        _run_python(
            source_root,
            "installers/shared/register-claude-marketplace.py",
        )
    if configure_codex:
        _run_python(
            source_root,
            "installers/shared/register-codex-marketplace.py",
        )
        daemon = meeting_home / "bin" / (
            "am-codexd.exe"
            if sys.platform.startswith("win")
            else "am-codexd"
        )
        subprocess.run(
            [str(daemon), "update", "--defer-if-active"],
            check=True,
        )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--target",
        choices=(TARGET_CLAUDE_CODE, TARGET_CODEX, TARGET_ALL),
        required=True,
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        default=REPOSITORY_ROOT,
    )
    parser.add_argument(
        "--meeting-home",
        type=Path,
        default=Path(
            os.environ.get("MEETING_HOME")
            or (Path.home() / ".agent-meeting")
        ),
    )
    parser.add_argument("--control-url", default="")
    parser.add_argument(
        "--enable-full-automation",
        action="store_true",
    )
    args = parser.parse_args(argv)
    install(
        source_root=args.source_root.resolve(),
        meeting_home=args.meeting_home.resolve(),
        target=args.target,
        control_url=args.control_url,
        enable_full_automation=args.enable_full_automation,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
