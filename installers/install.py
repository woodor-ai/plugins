#!/usr/bin/env python3
"""Install agent-meeting for Claude Code, Codex, or both clients."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_SOURCE = REPOSITORY_ROOT / "agent-meeting" / "src"
sys.path.insert(0, str(PACKAGE_SOURCE))
try:
    from agent_meeting.installation.legacy_checkout import (
        remove_legacy_checkout,
    )
finally:
    sys.path.pop(0)


TARGET_CLAUDE_CODE = "claude-code"
TARGET_CODEX = "codex"
TARGET_ALL = "all"


def _record_installation(
    source_root: Path,
    meeting_home: Path,
    target: str,
) -> None:
    package_source = source_root / "agent-meeting" / "src"
    sys.path.insert(0, str(package_source))
    try:
        from agent_meeting.installation.install_manifest import (
            record_installation,
        )
    finally:
        sys.path.pop(0)
    manifest = source_root / "agent-meeting" / ".codex-plugin" / "plugin.json"
    version = str(json.loads(manifest.read_text(encoding="utf-8"))["version"])
    targets = (
        {TARGET_CLAUDE_CODE, TARGET_CODEX}
        if target == TARGET_ALL
        else {target}
    )
    record_installation(
        meeting_home,
        version=version,
        targets=targets,
    )


def _run_python(
    source_root: Path,
    relative_script: str,
    *arguments: str,
) -> None:
    try:
        subprocess.run(
            [
                sys.executable,
                str(source_root / relative_script),
                *arguments,
            ],
            check=True,
        )
    except subprocess.CalledProcessError as error:
        print(
            "ERROR: installer stage "
            f"{Path(relative_script).name} failed (exit {error.returncode})",
            file=sys.stderr,
        )
        raise


def install(
    *,
    source_root: Path,
    meeting_home: Path,
    target: str,
    control_url: str = "",
    enable_full_automation: bool = False,
) -> None:
    try:
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
                "installers/shared/install-codex-integration.py",
                "--codex-home",
                str(
                    Path(
                        os.environ.get("CODEX_HOME")
                        or (Path.home() / ".codex")
                    )
                ),
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
        _record_installation(source_root, meeting_home, target)
    finally:
        remove_legacy_checkout(
            meeting_home,
            suppress_errors=sys.exc_info()[0] is not None,
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
