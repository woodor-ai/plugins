"""Update the public agent-meeting distribution and installed integrations."""

from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Callable, Iterable


PUBLIC_REPOSITORY = "https://github.com/woodor-ai/plugins.git"
CHECKOUT_REFRESH_TIMEOUT_SECONDS = 60
CHECKOUT_REFRESH_MAX_RETRIES = 3
CHECKOUT_REFRESH_RETRY_DELAYS_SECONDS = (1, 2, 4)
TARGET_CLAUDE_CODE = "claude-code"
TARGET_CODEX = "codex"
ALL_TARGETS = (TARGET_CLAUDE_CODE, TARGET_CODEX)


def default_meeting_home() -> Path:
    return Path(os.environ.get("MEETING_HOME") or (Path.home() / ".agent-meeting"))


def default_checkout(meeting_home: Path) -> Path:
    return meeting_home / "updates" / "plugins"


def detect_targets(*, home: Path | None = None) -> tuple[str, ...]:
    """Return integrations that are present on this machine.

    The updater may be installed before either client command is on PATH, so a
    configured client directory is sufficient evidence of an installed target.
    """
    home = Path.home() if home is None else home
    targets: list[str] = []
    if shutil.which("claude") or (home / ".claude").exists():
        targets.append(TARGET_CLAUDE_CODE)
    if shutil.which("codex") or (home / ".codex").exists():
        targets.append(TARGET_CODEX)
    return tuple(targets)


def refresh_checkout(
    *,
    checkout: Path,
    repository: str,
    run: Callable[..., object] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> Path:
    """Clone or refresh the updater-owned public release checkout.

    The production path owns the Git process group so a stalled HTTPS helper
    cannot outlive the timeout and write an error after the shell prompt.
    Existing checkouts are reset to the fetched release because this directory
    is an immutable updater cache, not a user workspace.
    ``run`` remains injectable for unit tests.
    """
    sleep = time.sleep if sleep is None else sleep
    if (checkout / ".git").is_dir():
        commands = [
            [
                "git",
                "-C",
                str(checkout),
                "fetch",
                "--prune",
                "origin",
                "main",
            ],
            [
                "git",
                "-C",
                str(checkout),
                "reset",
                "--hard",
                "FETCH_HEAD",
            ],
        ]
    else:
        checkout.parent.mkdir(parents=True, exist_ok=True)
        commands = [["git", "clone", repository, str(checkout)]]
    for retry in range(CHECKOUT_REFRESH_MAX_RETRIES + 1):
        try:
            for command in commands:
                if run is not None:
                    run(
                        command,
                        check=True,
                        timeout=CHECKOUT_REFRESH_TIMEOUT_SECONDS,
                    )
                else:
                    _refresh_checkout_process(command)
            return checkout
        except (RuntimeError, subprocess.SubprocessError) as exc:
            if retry == CHECKOUT_REFRESH_MAX_RETRIES:
                raise
            delay = CHECKOUT_REFRESH_RETRY_DELAYS_SECONDS[retry]
            print(
                "Retrying agent-meeting checkout refresh "
                f"({retry + 1}/{CHECKOUT_REFRESH_MAX_RETRIES}) "
                f"in {delay}s after: {exc}",
                flush=True,
            )
            sleep(delay)
    return checkout


def _refresh_checkout_process(command: list[str]) -> None:
    """Run Git in its own process group and report one bounded failure."""
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=os.name == "posix",
    )
    try:
        _stdout, stderr = process.communicate(
            timeout=CHECKOUT_REFRESH_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.communicate()
        raise RuntimeError(
            "agent-meeting checkout refresh timed out after "
            f"{CHECKOUT_REFRESH_TIMEOUT_SECONDS} seconds"
        ) from None
    if process.returncode:
        detail = stderr.strip() or "Git failed"
        raise RuntimeError(f"could not refresh agent-meeting checkout: {detail}")


def _run_python_script(
    source_root: Path,
    relative_script: str,
    *,
    run: Callable[..., object],
    arguments: tuple[str, ...] = (),
) -> None:
    run(
        [
            sys.executable,
            str(source_root / relative_script),
            *arguments,
        ],
        check=True,
    )


def release_version(source_root: Path) -> str:
    """Validate the shared public release version before installing it."""
    projects = (
        source_root / "agent-meeting" / "pyproject.toml",
        source_root / "mycodex" / "pyproject.toml",
    )
    versions = {
        str(tomllib.loads(project.read_text(encoding="utf-8"))["project"]["version"])
        for project in projects
    }
    if len(versions) != 1:
        raise ValueError("agent-meeting and mycodex release versions must match")
    version = versions.pop()
    if "+" in version:
        raise ValueError("public release versions must not use local cachebuster suffixes")
    return version


def install_release(
    *,
    source_root: Path,
    meeting_home: Path,
    targets: Iterable[str],
    run: Callable[..., object] = subprocess.run,
) -> None:
    """Install one release and refresh only its selected integrations."""
    targets = tuple(dict.fromkeys(targets))
    invalid_targets = set(targets).difference(ALL_TARGETS)
    if invalid_targets:
        raise ValueError(f"unknown update target(s): {', '.join(sorted(invalid_targets))}")
    if not targets:
        raise ValueError("no installed Claude Code or Codex integration was found")

    release_version(source_root)

    _run_python_script(
        source_root,
        "installers/shared/install-agent-meeting-package.py",
        run=run,
        arguments=("--meeting-home", str(meeting_home)),
    )
    _run_python_script(
        source_root,
        "installers/shared/migrate-agent-meeting-legacy-layout.py",
        run=run,
    )

    if TARGET_CLAUDE_CODE in targets:
        _run_python_script(
            source_root,
            "installers/shared/register-claude-marketplace.py",
            run=run,
        )

    if TARGET_CODEX in targets:
        configure = meeting_home / "bin" / "am-configure-codex-user-environment"
        if sys.platform.startswith("win"):
            configure = configure.with_suffix(".exe")
        run([str(configure)], check=True)
        _run_python_script(
            source_root,
            "installers/shared/register-codex-marketplace.py",
            run=run,
        )
        daemon = meeting_home / "bin" / "am-codexd"
        if sys.platform.startswith("win"):
            daemon = daemon.with_suffix(".exe")
        run([str(daemon), "update", "--defer-if-active"], check=True)


def active_runtime_version(meeting_home: Path) -> str | None:
    """Return the version selected by the atomic activation record, if any."""
    state_file = meeting_home / "active-runtime.json"
    try:
        payload = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = payload.get("version")
    return str(version) if version else None
