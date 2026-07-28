"""Resolve the active message hub through the public ``am`` client."""

from __future__ import annotations

import json


def discover_control(run_meeting) -> dict:
    try:
        result = run_meeting("controls", "--json")
        if result is None or result.returncode != 0 or not result.stdout.strip():
            return {}
        controls = json.loads(result.stdout)
        if not controls:
            return {}
        control = next(
            (item for item in controls if item.get("is_current")),
            controls[0],
        )
        ip = control.get("ip") or ""
        port = control.get("port") or ""
        host = control.get("host") or ip
        return {
            "ip": ip,
            "port": port,
            "host": host,
            "ip_port": f"{ip}:{port}",
            "base_url": f"http://{ip}:{port}" if ip and port else "",
        }
    except Exception:
        return {}
