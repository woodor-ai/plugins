"""Delete the installation after the invoking executable has exited."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path


MANIFEST_NAME = "install-manifest.json"


def validate_target(meeting_home: Path) -> Path:
    target = meeting_home.resolve()
    if target == Path(target.anchor) or target == Path.home().resolve():
        raise RuntimeError(f"refusing unsafe uninstall target: {target}")
    manifest = target / MANIFEST_NAME
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot validate uninstall target: {target}") from exc
    if payload.get("meeting_home") != str(target):
        raise RuntimeError(f"install manifest does not own uninstall target: {target}")
    return target


def delete_installation(
    meeting_home: Path,
    *,
    retry_seconds: float = 600,
) -> None:
    target = validate_target(meeting_home)
    deadline = time.monotonic() + retry_seconds
    while True:
        try:
            shutil.rmtree(target)
            return
        except FileNotFoundError:
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)


def _base_python() -> Path:
    executable = Path(getattr(sys, "_base_executable", sys.executable))
    if sys.platform.startswith("win"):
        pythonw = executable.with_name("pythonw.exe")
        if pythonw.exists():
            return pythonw
    return executable


def schedule_cleanup(meeting_home: Path) -> Path:
    target = validate_target(meeting_home)
    helper = Path(tempfile.gettempdir()) / (
        f"agent-meeting-uninstall-{uuid.uuid4().hex}.py"
    )
    shutil.copy2(Path(__file__), helper)
    command = [str(_base_python()), str(helper), str(target), "--self-delete"]
    options: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform.startswith("win"):
        options["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    else:
        options["start_new_session"] = True
    subprocess.Popen(command, **options)
    return helper


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("meeting_home", type=Path)
    parser.add_argument("--self-delete", action="store_true")
    args = parser.parse_args(argv)
    delete_installation(args.meeting_home)
    if args.self_delete:
        try:
            Path(__file__).unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
