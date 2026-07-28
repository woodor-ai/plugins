"""Console entrypoint that launches the Claude session monitor module."""

from __future__ import annotations

import runpy


def main() -> None:
    runpy.run_module(
        "agent_meeting.ai_platforms.claude_code.session_message_monitor",
        run_name="__main__",
    )
