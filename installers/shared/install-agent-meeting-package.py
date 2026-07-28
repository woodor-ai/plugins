#!/usr/bin/env python3
"""Build and install immutable agent-meeting and mycodex host packages."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "agent-meeting" / "src"))

from agent_meeting.installation.version_activation import (
    RUNTIME_COMMANDS,
    activate_runtime,
    runtime_command_path,
)
from agent_meeting.installation.message_hub_service_installation import (
    ensure_local_message_hub_service,
)


def _project_version(project_root: Path) -> str:
    manifest = project_root / "pyproject.toml"
    import tomllib

    return str(
        tomllib.loads(manifest.read_text(encoding="utf-8"))["project"][
            "version"
        ]
    )


def _venv_python(runtime_dir: Path, *, is_windows: bool) -> Path:
    if is_windows:
        return runtime_dir / "venv" / "Scripts" / "python.exe"
    return runtime_dir / "venv" / "bin" / "python"


def install_runtime(
    *,
    source_root: Path,
    meeting_home: Path,
    is_windows: bool | None = None,
) -> dict:
    is_windows = (
        sys.platform.startswith("win")
        if is_windows is None
        else is_windows
    )
    agent_meeting_project = source_root / "agent-meeting"
    mycodex_project = source_root / "mycodex"
    agent_meeting_version = _project_version(agent_meeting_project)
    mycodex_version = _project_version(mycodex_project)
    if agent_meeting_version != mycodex_version:
        raise RuntimeError(
            "host package versions differ: "
            f"agent-meeting={agent_meeting_version}, mycodex={mycodex_version}"
        )
    version = agent_meeting_version
    runtimes_dir = meeting_home / "runtimes"
    final_runtime = runtimes_dir / version
    runtimes_dir.mkdir(parents=True, exist_ok=True)

    installing_marker = final_runtime / ".installing"
    if installing_marker.exists():
        # Recover only an exact interrupted version directory that carries our
        # marker. Never clear an unmarked immutable runtime.
        shutil.rmtree(final_runtime)

    if not final_runtime.exists():
        final_runtime.mkdir()
        installing_marker.write_text(
            f"pid={os.getpid()}\n",
            encoding="utf-8",
        )
        try:
            # A Python venv is not relocatable: POSIX shebangs and Windows
            # console launchers embed the interpreter path. Build it directly
            # under the immutable final version path, but keep it invisible to
            # activation until validation succeeds and .installing is removed.
            venv.EnvBuilder(with_pip=True).create(final_runtime / "venv")
            python_executable = _venv_python(
                final_runtime,
                is_windows=is_windows,
            )
            subprocess.run(
                [
                    str(python_executable),
                    "-m",
                    "pip",
                    "install",
                    str(agent_meeting_project),
                    str(mycodex_project),
                ],
                check=True,
            )
            missing = [
                command
                for command in RUNTIME_COMMANDS
                if not runtime_command_path(
                    final_runtime,
                    command,
                    is_windows=is_windows,
                ).is_file()
            ]
            if missing:
                raise RuntimeError(
                    "installed runtime is missing commands: "
                    + ", ".join(missing)
                )
            (final_runtime / "runtime.json").write_text(
                json.dumps(
                    {
                        "version": version,
                        "agent_meeting_project": str(
                            agent_meeting_project.resolve()
                        ),
                        "mycodex_project": str(mycodex_project.resolve()),
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            installing_marker.unlink()
        except Exception:
            shutil.rmtree(final_runtime, ignore_errors=True)
            raise

    return activate_runtime(
        meeting_home=meeting_home,
        version=version,
        is_windows=is_windows,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
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
    args = parser.parse_args(argv)
    payload = install_runtime(
        source_root=args.source_root.resolve(),
        meeting_home=args.meeting_home.resolve(),
    )
    meeting_home = args.meeting_home.resolve()
    if sys.platform.startswith("win"):
        session_start = meeting_home / "bin" / "am-claude-session-start.exe"
        environment = os.environ.copy()
        environment["MEETING_HOME"] = str(meeting_home)
        subprocess.run(
            [str(session_start)],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
    else:
        ensure_local_message_hub_service(meeting_home)
    print(
        f"installed and activated host runtime {payload['version']} "
        f"at {payload['runtime']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
