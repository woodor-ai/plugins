#!/usr/bin/env python3
"""Install the released agent-meeting integration from its R2 release bundle."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path


RELEASE = "v0.18.30"
ARCHIVE_URL = (
    "https://dl.omi-atlas.com/am/releases/" + RELEASE + "/agent-meeting.zip"
)
TARGETS = ("claude-code", "codex", "all")


def detect_target() -> str:
    has_claude = shutil.which("claude") is not None
    has_codex = shutil.which("codex") is not None
    if has_claude and has_codex:
        return "all"
    if has_claude:
        return "claude-code"
    if has_codex:
        return "codex"
    raise RuntimeError(
        "neither claude nor codex CLI is on PATH; install a supported client first"
    )


def download_archive(destination: Path) -> None:
    request = urllib.request.Request(
        ARCHIVE_URL,
        headers={"User-Agent": "agent-meeting-installer/" + RELEASE},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        destination.write_bytes(response.read())


def extracted_repository(directory: Path) -> Path:
    repositories = [
        item
        for item in directory.iterdir()
        if item.is_dir() and (item / "installers" / "install.py").is_file()
    ]
    if len(repositories) != 1:
        raise RuntimeError("downloaded archive has an unexpected layout")
    return repositories[0]


def install(
    *,
    target: str,
    meeting_home: Path | None = None,
    control_url: str = "",
    enable_full_automation: bool = False,
) -> None:
    with tempfile.TemporaryDirectory(prefix="agent-meeting-install-") as raw:
        temporary = Path(raw)
        archive = temporary / "agent-meeting.zip"
        source = temporary / "source"
        source.mkdir()
        print(f"Downloading agent-meeting {RELEASE}...", flush=True)
        download_archive(archive)
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(source)
        repository = extracted_repository(source)
        command = [
            sys.executable,
            str(repository / "installers" / "install.py"),
            "--target",
            target,
            "--source-root",
            str(repository),
        ]
        if meeting_home is not None:
            command.extend(("--meeting-home", str(meeting_home.resolve())))
        if control_url:
            command.extend(("--control-url", control_url))
        if enable_full_automation:
            command.append("--enable-full-automation")
        subprocess.run(command, check=True)
    print(f"agent-meeting {RELEASE} installation complete for {target}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install agent-meeting for Claude Code, Codex, or both.",
    )
    parser.add_argument("--target", choices=TARGETS)
    parser.add_argument("--meeting-home", type=Path)
    parser.add_argument("--control-url", default="")
    parser.add_argument("--enable-full-automation", action="store_true")
    args = parser.parse_args(argv)
    target = args.target or os.environ.get("AGENT_MEETING_INSTALL_TARGET")
    if target and target not in TARGETS:
        parser.error(
            "AGENT_MEETING_INSTALL_TARGET must be claude-code, codex, or all"
        )
    try:
        install(
            target=target or detect_target(),
            meeting_home=args.meeting_home,
            control_url=args.control_url,
            enable_full_automation=args.enable_full_automation,
        )
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
