"""Update agent-meeting from the public ephemeral release installer."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Callable, Iterable

from agent_meeting.installation.legacy_checkout import (
    legacy_checkout,
    remove_legacy_checkout,
)


PUBLIC_INSTALLER_URL = "https://dl.omi-atlas.com/am/install.py"
DOWNLOAD_TIMEOUT_SECONDS = 60
TARGET_CLAUDE_CODE = "claude-code"
TARGET_CODEX = "codex"
ALL_TARGETS = (TARGET_CLAUDE_CODE, TARGET_CODEX)


def default_meeting_home() -> Path:
    return Path(os.environ.get("MEETING_HOME") or (Path.home() / ".agent-meeting"))

def detect_targets(*, home: Path | None = None) -> tuple[str, ...]:
    """Return integrations that are present on this machine."""
    home = Path.home() if home is None else home
    targets: list[str] = []
    if shutil.which("claude") or (home / ".claude").exists():
        targets.append(TARGET_CLAUDE_CODE)
    if shutil.which("codex") or (home / ".codex").exists():
        targets.append(TARGET_CODEX)
    return tuple(targets)


def selected_target(targets: Iterable[str]) -> str:
    targets = tuple(dict.fromkeys(targets))
    invalid_targets = set(targets).difference(ALL_TARGETS)
    if invalid_targets:
        raise ValueError(
            f"unknown update target(s): {', '.join(sorted(invalid_targets))}"
        )
    if not targets:
        raise ValueError("no installed Claude Code or Codex integration was found")
    return "all" if set(targets) == set(ALL_TARGETS) else targets[0]


def install_latest(
    *,
    meeting_home: Path,
    targets: Iterable[str],
    installer_url: str = PUBLIC_INSTALLER_URL,
    opener: Callable[..., object] = urllib.request.urlopen,
    run: Callable[..., object] = subprocess.run,
) -> None:
    """Run the current public installer from a disposable directory."""
    target = selected_target(targets)
    try:
        with tempfile.TemporaryDirectory(prefix="agent-meeting-update-") as raw:
            installer = Path(raw) / "install.py"
            request = urllib.request.Request(
                installer_url,
                headers={"User-Agent": "agent-meeting-updater"},
            )
            with opener(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
                with installer.open("wb") as output:
                    shutil.copyfileobj(response, output)
            run(
                [
                    sys.executable,
                    str(installer),
                    "--target",
                    target,
                    "--meeting-home",
                    str(meeting_home),
                ],
                check=True,
            )
    finally:
        remove_legacy_checkout(
            meeting_home,
            suppress_errors=sys.exc_info()[0] is not None,
        )


def active_runtime_version(meeting_home: Path) -> str | None:
    """Return the version selected by the atomic activation record, if any."""
    state_file = meeting_home / "active-runtime.json"
    try:
        payload = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = payload.get("version")
    return str(version) if version else None
