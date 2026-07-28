#!/usr/bin/env python3
"""Compatibility facade for the packaged agent-meeting client modules.

New code imports the domain modules under ``agent_meeting`` directly. This
file remains as a compatibility facade for copied monitors and Codex launchers
that import ``am_common`` from ``~/.agent-meeting/bin``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _resolve_source_root() -> Path:
    local_source = Path(__file__).resolve().parent.parent / "src"
    if local_source.is_dir():
        return local_source

    activation = Path(__file__).resolve().parent.parent / ".bin-plugin-root"
    if activation.is_file():
        lines = activation.read_text(encoding="utf-8").splitlines()
        if lines:
            activated_source = Path(lines[0]).parent / "src"
            if activated_source.is_dir():
                return activated_source

    raise RuntimeError(
        "agent-meeting package source not found; rerun the agent-meeting installer"
    )


_SOURCE_ROOT = _resolve_source_root()
sys.path.insert(0, str(_SOURCE_ROOT))

from agent_meeting.clients.client_configuration import (  # noqa: E402
    read_auth_token,
    read_plugin_version,
)
from agent_meeting.clients.hub_discovery import discover_control  # noqa: E402
from agent_meeting.clients.hub_subscription_client import (  # noqa: E402
    HubSubscriptionClient,
    read_frame,
    receive_exact,
    send_masked_frame,
    websocket_key,
)
from agent_meeting.clients.am_process_client import (  # noqa: E402
    run_am_cli,
)
from agent_meeting.messaging import project_identity  # noqa: E402


MEETING_HOME = os.environ.get("MEETING_HOME") or os.path.expanduser(
    "~/.agent-meeting"
)


_project_root = project_identity._project_root
validate_proj = project_identity.validate_project
pidfile_stem = project_identity.monitor_pidfile_stem


def proj_cache_path(root: str) -> str:
    return project_identity.proj_cache_path(root, meeting_home=MEETING_HOME)


def proj_cache_get(root: str):
    return project_identity.proj_cache_get(root, meeting_home=MEETING_HOME)


def proj_cache_set(root: str, project: str) -> None:
    project_identity.proj_cache_set(
        root,
        project,
        meeting_home=MEETING_HOME,
    )


def proj_cache_entries() -> list[dict]:
    return project_identity.proj_cache_entries(meeting_home=MEETING_HOME)


def proj_cache_clear(root: str) -> bool:
    return project_identity.proj_cache_clear(root, meeting_home=MEETING_HOME)


def proj_cache_clear_all() -> int:
    return project_identity.proj_cache_clear_all(meeting_home=MEETING_HOME)


def resolve_authoritative_project(cwd: str, explicit_project: str | None):
    return project_identity.resolve_authoritative_project(
        cwd,
        explicit_project,
        meeting_home=MEETING_HOME,
    )


def derive_project(cwd: str) -> str:
    return project_identity.derive_project(cwd, meeting_home=MEETING_HOME)


# Legacy names remain only on this compatibility surface.
ws_make_key = websocket_key
ws_send_masked = send_masked_frame
ws_recv_exact = receive_exact
ws_read_frame = read_frame
WSSubscribeClient = HubSubscriptionClient
