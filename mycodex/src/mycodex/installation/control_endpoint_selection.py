"""Select and persist the agent-meeting control used by mycodex."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Callable

from agent_meeting.clients.am_process_client import run_am_cli


def parse_discovered_controls(payload: str) -> str:
    try:
        controls = json.loads(payload)
        if not controls:
            return ""
        selected = next(
            (item for item in controls if item.get("is_current")),
            controls[0],
        )
        ip = selected.get("ip", "")
        port = selected.get("port", "")
        return f"http://{ip}:{port}" if ip and port else ""
    except Exception:
        return ""


def discover_control(am_command: Path) -> str:
    if not am_command.exists():
        return ""
    try:
        result = run_am_cli(
            am_command,
            "controls",
            "--json",
            timeout=10,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return ""
        return parse_discovered_controls(result.stdout)
    except Exception:
        return ""


def launcher_configuration_path(meeting_home: Path) -> Path:
    return meeting_home / "codex" / "launcher.json"


def read_saved_control(meeting_home: Path) -> str:
    try:
        return str(
            json.loads(
                launcher_configuration_path(meeting_home).read_text(
                    encoding="utf-8"
                )
            ).get("control_url")
            or ""
        ).strip()
    except Exception:
        return ""


def local_control_url(meeting_home: Path) -> str:
    try:
        payload = json.loads(
            meeting_home.joinpath("am-msgd.json").read_text(
                encoding="utf-8"
            )
        )
        port = int(payload.get("port", 8765))
    except Exception:
        port = 8765
    return f"http://127.0.0.1:{port}"


def control_is_healthy(control_url: str) -> bool:
    try:
        with urllib.request.urlopen(
            control_url.rstrip("/") + "/health",
            timeout=2,
        ) as response:
            return bool(
                json.loads(response.read().decode("utf-8")).get("ok")
            )
    except Exception:
        return False


def select_control(
    *,
    meeting_home: Path,
    discovered: str,
    explicit: str,
    prompt: Callable[[str, str], str],
    health_check: Callable[[str], bool] = control_is_healthy,
) -> str:
    if explicit:
        return explicit
    if discovered:
        return discovered
    saved = read_saved_control(meeting_home)
    # A persisted remote hub is an authority choice. Do not silently replace
    # it with the unrelated local database merely because the network is down.
    if saved:
        return saved
    local = local_control_url(meeting_home)
    if health_check(local):
        return local
    return prompt("control URL (http://x.x.x.x:8765)", "")


def write_launcher_default(
    meeting_home: Path,
    control_url: str,
) -> None:
    if not control_url:
        return
    path = launcher_configuration_path(meeting_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    payload["control_url"] = control_url
    path.write_text(
        json.dumps(payload, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
