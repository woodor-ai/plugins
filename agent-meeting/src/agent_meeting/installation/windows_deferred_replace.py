"""Finish Windows launcher replacements after running processes release them."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path


DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
RETRY_SECONDS = 3600


def _atomic_write_json(path: Path, payload: dict) -> None:
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    temporary.write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def schedule_replacements(
    *,
    runtime_dir: Path,
    meeting_home: Path,
    replacements: list[tuple[Path, Path]],
) -> Path:
    control_dir = meeting_home / "control"
    control_dir.mkdir(parents=True, exist_ok=True)
    manifest = control_dir / (
        f"pending-windows-launcher-replacements.{os.getpid()}.json"
    )
    _atomic_write_json(
        manifest,
        {
            "replacements": [
                {"source": str(source), "destination": str(destination)}
                for source, destination in replacements
            ]
        },
    )
    scripts_dir = runtime_dir / "venv" / "Scripts"
    python = scripts_dir / "pythonw.exe"
    if not python.is_file():
        python = scripts_dir / "python.exe"
    subprocess.Popen(
        [
            str(python),
            "-m",
            "agent_meeting.installation.windows_deferred_replace",
            "--manifest",
            str(manifest),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
    return manifest


def apply_replacements(
    manifest: Path,
    *,
    replace=os.replace,
    sleep=time.sleep,
    monotonic=time.monotonic,
    timeout: float = RETRY_SECONDS,
) -> bool:
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    pending = [
        (Path(item["source"]), Path(item["destination"]))
        for item in payload["replacements"]
    ]
    deadline = monotonic() + timeout
    while pending:
        remaining = []
        for source, destination in pending:
            try:
                replace(source, destination)
            except PermissionError:
                remaining.append((source, destination))
        pending = remaining
        if not pending:
            manifest.unlink(missing_ok=True)
            return True
        if monotonic() >= deadline:
            return False
        sleep(0.1)
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    return 0 if apply_replacements(args.manifest) else 1


if __name__ == "__main__":
    raise SystemExit(main())
