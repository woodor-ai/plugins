"""Install or update the default local ``am-msgd`` user service."""

from __future__ import annotations

import json
import platform
from pathlib import Path

from agent_meeting.message_hub import service_configuration
from agent_meeting.operating_systems import message_hub_user_service


def _initial_configuration(
    meeting_home: Path,
) -> service_configuration.MessageHubServiceConfiguration:
    legacy_config = meeting_home / "config.json"
    try:
        payload = json.loads(legacy_config.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        payload = {}
    binds = (
        ("0.0.0.0",)
        if payload.get("is_host") is True
        else service_configuration.DEFAULT_BINDS
    )
    enabled = not (meeting_home / "am-msgd.stopped").exists()
    return service_configuration.MessageHubServiceConfiguration(
        enabled=enabled,
        binds=binds,
    )


def ensure_configuration(
    meeting_home: Path,
) -> service_configuration.MessageHubServiceConfiguration:
    path = service_configuration.default_path(meeting_home)
    with service_configuration.locked(path):
        if path.exists():
            return service_configuration.load(path)
        configuration = _initial_configuration(meeting_home)
        service_configuration.write(path, configuration)
        return configuration


def ensure_local_message_hub_service(
    meeting_home: Path,
    *,
    system_name: str | None = None,
    restart_enabled_service: bool = True,
) -> service_configuration.MessageHubServiceConfiguration:
    """Migrate configuration, install autostart, and apply enabled state."""
    system_name = system_name or platform.system()
    path = service_configuration.default_path(meeting_home)
    configuration = ensure_configuration(meeting_home)

    message_hub_user_service.ensure_installed(
        meeting_home,
        path,
        system_name=system_name,
    )
    if configuration.enabled:
        if restart_enabled_service:
            message_hub_user_service.restart(
                meeting_home,
                path,
                system_name=system_name,
            )
        else:
            message_hub_user_service.start(
                meeting_home,
                path,
                system_name=system_name,
            )
    else:
        message_hub_user_service.stop(
            meeting_home,
            system_name=system_name,
        )
    return configuration
