"""Resolve the active message hub through the public ``am`` client."""

from __future__ import annotations

import json


def discover_control(run_meeting) -> dict:
    try:
        result = run_meeting("msgd", "--json")
        if result is None or result.returncode != 0 or not result.stdout.strip():
            return {}
        nodes = json.loads(result.stdout)
        if not nodes:
            return {}
        node = next(
            (item for item in nodes if item.get("is_current")),
            nodes[0],
        )
        ip = node.get("ip") or ""
        port = node.get("port") or ""
        host = node.get("host") or ip
        return {
            "ip": ip,
            "port": port,
            "host": host,
            "ip_port": f"{ip}:{port}",
            "base_url": f"http://{ip}:{port}" if ip and port else "",
        }
    except Exception:
        return {}
