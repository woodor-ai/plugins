"""Authenticated JSON requests to the central agent-am message hub."""

from __future__ import annotations

import http.client
import json
import socket
import urllib.error
import urllib.parse
import urllib.request


def request_once(
    method: str,
    host_url: str,
    path: str,
    params: dict | None,
    body: dict | None,
    *,
    auth_token: str | None,
    opener,
):
    url = host_url + path
    if params:
        url += "?" + urllib.parse.urlencode(params)

    headers = {}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"

    request = urllib.request.Request(
        url,
        method=method,
        data=data,
        headers=headers,
    )
    with opener.open(request, timeout=10) as response:
        content_type = response.headers.get("Content-Type", "")
        raw = response.read()
        if "application/json" in content_type:
            return json.loads(raw.decode("utf-8"))
        return raw.decode("utf-8")


def request_with_rediscovery(
    method: str,
    host_url: str,
    path: str,
    params: dict | None,
    body: dict | None,
    *,
    read_auth_token,
    invalidate_host_cache,
    discover_host,
    opener,
):
    broken_connection_errors = (
        http.client.RemoteDisconnected,
        http.client.IncompleteRead,
        ConnectionError,
        socket.timeout,
        TimeoutError,
    )

    def send(target_host: str):
        return request_once(
            method,
            target_host,
            path,
            params,
            body,
            auth_token=read_auth_token(),
            opener=opener,
        )

    try:
        return send(host_url)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise SystemExit(
            f"central am-msgd error: HTTP {error.code}: {detail}"
        )
    except urllib.error.URLError as error:
        invalidate_host_cache()
        raise SystemExit(
            f"central am-msgd unreachable at {host_url}: {error.reason}"
        )
    except broken_connection_errors as error:
        invalidate_host_cache()
        new_host = discover_host()
        if new_host is None:
            raise SystemExit(
                "central am-msgd connection broken and no message hub found "
                f"on retry: {error}"
            )
        try:
            return send(new_host)
        except Exception as retry_error:
            raise SystemExit(
                f"central am-msgd unreachable after retry: {retry_error}"
            ) from retry_error
