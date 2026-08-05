"""Console-free Windows entrypoints for agent-meeting user services."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import sys
import traceback
from typing import Callable

from agent_meeting.commands.am_msgd_cli import main as am_msgd_cli_main
from agent_meeting.lifecycle_control.controller_process import (
    main as am_ctld_cli_main,
)


SERVICE_LOG_OPTION = "--service-log"


def _run_service_entrypoint(
    main: Callable[[list[str]], int],
    argv: list[str] | None = None,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        option_index = arguments.index(SERVICE_LOG_OPTION)
        log_path = Path(arguments[option_index + 1])
    except (ValueError, IndexError):
        return 2
    del arguments[option_index : option_index + 2]

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8", buffering=1) as output:
        with redirect_stdout(output), redirect_stderr(output):
            try:
                return main(arguments)
            except Exception:
                traceback.print_exc()
                return 1


def am_ctld_service_main() -> int:
    return _run_service_entrypoint(am_ctld_cli_main)


def am_msgd_service_main() -> int:
    return _run_service_entrypoint(am_msgd_cli_main)


__all__ = [
    "SERVICE_LOG_OPTION",
    "am_ctld_service_main",
    "am_msgd_service_main",
]
