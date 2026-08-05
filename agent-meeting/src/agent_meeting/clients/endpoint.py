"""Normalize agent-meeting service endpoints."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse


def normalize_am_msgd(value: str, default_port: int = 8765) -> str:
    """Normalize an am-msgd endpoint to its internal HTTP URL."""
    raw = (value or "").strip()
    if not raw:
        return ""

    if "://" not in raw:
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            endpoint = f"http://{raw}"
        else:
            endpoint = (
                f"http://[{address}]"
                if address.version == 6
                else f"http://{address}"
            )
    else:
        endpoint = raw

    parsed = urlparse(endpoint)
    if parsed.scheme.lower() != "http":
        raise ValueError("am-msgd only supports HTTP endpoints")
    if not parsed.hostname:
        raise ValueError("am-msgd endpoint must include a host")
    if parsed.username or parsed.password:
        raise ValueError("am-msgd endpoint must not include credentials")
    if (
        parsed.path not in ("", "/")
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("am-msgd endpoint must not include a path or query")
    if parsed.netloc.endswith(":"):
        raise ValueError("am-msgd endpoint has an empty port")
    try:
        port = parsed.port or default_port
    except ValueError as error:
        raise ValueError(f"invalid am-msgd port: {error}") from error
    if not 1 <= port <= 65535:
        raise ValueError("am-msgd port must be between 1 and 65535")

    host = parsed.hostname
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        rendered_host = host
    else:
        rendered_host = f"[{address}]" if address.version == 6 else str(address)
    return f"http://{rendered_host}:{port}"
