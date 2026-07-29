"""Public CLI for the local ``am-msgd`` service and daemon."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from agent_meeting.message_hub import service_configuration
from agent_meeting.message_hub.message_hub_process import serve
from agent_meeting.operating_systems import message_hub_user_service


def _meeting_home() -> Path:
    configured = os.environ.get("MEETING_HOME")
    return Path(configured) if configured else Path.home() / ".agent-meeting"


def _configuration_path(meeting_home: Path) -> Path:
    return service_configuration.default_path(meeting_home)


def _admin_token(meeting_home: Path) -> str:
    try:
        return (
            meeting_home
            .joinpath("am-msgd.admin-token")
            .read_text(encoding="utf-8")
            .strip()
        )
    except FileNotFoundError as error:
        raise RuntimeError(
            "am-msgd admin token is missing; start the service first"
        ) from error


def _business_token(meeting_home: Path) -> str | None:
    try:
        payload = json.loads(
            meeting_home.joinpath("config.json").read_text(encoding="utf-8")
        )
    except (FileNotFoundError, ValueError):
        return None
    return payload.get("auth_token") or None


def _request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
    headers: dict | None = None,
    timeout: float = 3,
):
    data = None
    actual_headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        actual_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers=actual_headers,
    )
    try:
        # Every management request is intentionally local. Ignoring proxy
        # environment variables also keeps IPv6 loopback (`::1`) local on
        # systems whose NO_PROXY value only covers 127.0.0.1.
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({})
        )
        with opener.open(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        try:
            message = json.loads(detail).get("error") or detail
        except ValueError:
            message = detail
        raise RuntimeError(f"am-msgd HTTP {error.code}: {message}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"am-msgd unavailable: {error.reason}") from error


def _base_url(configuration) -> str:
    return f"http://127.0.0.1:{configuration.port}"


def _listener_label(address: str, port: int) -> str:
    address = service_configuration.normalize_ip(address)
    return (
        f"[{address}]:{port}"
        if ":" in address
        else f"{address}:{port}"
    )


def _health(configuration) -> dict | None:
    try:
        return _request_json(
            f"{_base_url(configuration)}/health",
            timeout=1,
        )
    except RuntimeError:
        return None


def _connected_agents(
    meeting_home: Path,
    configuration,
) -> list[dict]:
    headers = {}
    token = _business_token(meeting_home)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    rows = _request_json(
        f"{_base_url(configuration)}/list",
        headers=headers,
    )
    agents = [
        {
            "address": row.get("host") or "-",
            "name": row["name"],
            "proj": row["project"],
        }
        for row in rows
        if row.get("status") == "online"
    ]
    agents.sort(
        key=lambda agent: (
            agent["proj"],
            agent["name"],
            agent["address"],
        )
    )
    return agents


def _admin_request(
    meeting_home: Path,
    configuration,
    path: str,
    *,
    method: str,
    payload: dict | None = None,
):
    return _request_json(
        f"{_base_url(configuration)}{path}",
        method=method,
        payload=payload,
        headers={
            "X-Am-Msgd-Admin-Token": _admin_token(meeting_home),
        },
    )


def _print_listener_snapshot(snapshot: dict) -> None:
    for listener in snapshot.get("active_listeners", []):
        print(f"active\t{listener}")
    for listener, error in snapshot.get("listener_errors", {}).items():
        print(f"failed\t{listener}\t{error}")


def _mutate_configuration(args, meeting_home: Path) -> int:
    path = _configuration_path(meeting_home)
    with service_configuration.locked(path):
        configuration = service_configuration.load(path, create=True)
        first_lan_bind = False
        if args.bind_address:
            requested_address = service_configuration.normalize_ip(
                args.bind_address
            )
            first_lan_bind = (
                requested_address not in configuration.binds
                and not ipaddress.ip_address(requested_address).is_loopback
                and all(
                    ipaddress.ip_address(address).is_loopback
                    for address in configuration.binds
                )
            )
        health = _health(configuration)
        if health:
            if args.bind_address:
                snapshot = _admin_request(
                    meeting_home,
                    configuration,
                    "/_admin/listeners",
                    method="POST",
                    payload={"address": args.bind_address},
                )
            elif args.unbind_address:
                encoded = urllib.parse.quote(
                    service_configuration.normalize_ip(args.unbind_address),
                    safe="",
                )
                snapshot = _admin_request(
                    meeting_home,
                    configuration,
                    f"/_admin/listeners/{encoded}",
                    method="DELETE",
                )
            else:
                snapshot = _admin_request(
                    meeting_home,
                    configuration,
                    "/_admin/local-only",
                    method="POST",
                )
            _print_listener_snapshot(snapshot)
            if first_lan_bind:
                print(
                    "warning: am-msgd is now reachable beyond loopback; "
                    "configure an auth token before using an untrusted network",
                    file=sys.stderr,
                )
            return 0

        if args.bind_address:
            updated = service_configuration.with_added_bind(
                configuration,
                args.bind_address,
            )
        elif args.unbind_address:
            updated = service_configuration.with_removed_bind(
                configuration,
                args.unbind_address,
            )
        else:
            updated = service_configuration.local_only(configuration)
        service_configuration.write(path, updated)
    if first_lan_bind:
        print(
            "warning: the saved configuration exposes am-msgd beyond "
            "loopback on the next start",
            file=sys.stderr,
        )
    print("am-msgd is stopped; configuration saved for the next start")
    return 0


def _set_enabled(
    meeting_home: Path,
    enabled: bool,
) -> service_configuration.MessageHubServiceConfiguration:
    path = _configuration_path(meeting_home)
    with service_configuration.locked(path):
        configuration = service_configuration.load(path, create=True)
        configuration = service_configuration.with_enabled(
            configuration,
            enabled,
        )
        service_configuration.write(path, configuration)
    return configuration


def _run_lifecycle(action: str, meeting_home: Path) -> int:
    path = _configuration_path(meeting_home)
    if action == "stop":
        _set_enabled(meeting_home, False)
        message_hub_user_service.stop(meeting_home)
        print("am-msgd stopped; autostart disabled")
        return 0

    configuration = _set_enabled(meeting_home, True)
    if action == "start":
        message_hub_user_service.start(meeting_home, path)
        success_message = "am-msgd started; autostart enabled"
    else:
        message_hub_user_service.restart(meeting_home, path)
        success_message = "am-msgd restarted; autostart enabled"
    deadline = time.time() + 8
    while time.time() < deadline:
        if _health(configuration):
            print(success_message)
            return 0
        time.sleep(0.1)
    raise RuntimeError(
        "am-msgd service manager returned success but /health is unreachable"
    )


def _status(args, meeting_home: Path) -> int:
    path = _configuration_path(meeting_home)
    configuration = service_configuration.load(path, create=False)
    health = _health(configuration)
    payload = {
        "service": (
            "running"
            if health
            else ("stopped" if not configuration.enabled else "unreachable")
        ),
        "autostart": configuration.enabled,
        "authentication": (
            "enabled" if _business_token(meeting_home) else "open"
        ),
        "service_manager": message_hub_user_service.service_state(meeting_home),
        "port": configuration.port,
        "configured_listeners": [
            _listener_label(address, configuration.port)
            for address in configuration.binds
        ],
        "connected_agents": [],
    }
    if health:
        payload.update(
            {
                "version": health.get("version"),
                "instance_id": health.get("instance_id"),
                "active_listeners": health.get("active_listeners", []),
                "listener_errors": health.get("listener_errors", {}),
                "mdns": health.get("mdns", "off"),
            }
        )
        try:
            payload["connected_agents"] = _connected_agents(
                meeting_home,
                configuration,
            )
        except RuntimeError as error:
            payload["connected_agents"] = []
            payload["agents_error"] = str(error)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(f"service:    {payload['service']}")
        print(
            "autostart:  "
            + ("enabled" if payload["autostart"] else "disabled")
        )
        print(f"auth:       {payload['authentication']}")
        print(f"manager:    {payload['service_manager']}")
        if payload.get("version"):
            print(f"version:    {payload['version']}")
        print(f"port:       {payload['port']}")
        for listener in payload.get("active_listeners", []):
            print(f"listener:   {listener} active")
        active = set(payload.get("active_listeners", []))
        for listener in payload["configured_listeners"]:
            if listener not in active:
                error = payload.get("listener_errors", {}).get(listener)
                state = f"failed ({error})" if error else "configured"
                print(f"listener:   {listener} {state}")
        agents = payload.get("connected_agents", [])
        print(f"agents:     {len(agents)} connected")
        if agents:
            print("ADDRESS\tNAME\tPROJ")
            for agent in agents:
                print(
                    f"{agent['address']}\t{agent['name']}\t{agent['proj']}"
                )
        if payload.get("agents_error"):
            print(f"agents:     unavailable ({payload['agents_error']})")
    if not health:
        return 1
    return (
        2
        if payload.get("listener_errors") or payload.get("agents_error")
        else 0
    )


def _agent_list(args, meeting_home: Path) -> int:
    configuration = service_configuration.load(
        _configuration_path(meeting_home),
        create=False,
    )
    headers = {}
    token = _business_token(meeting_home)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        rows = _request_json(
            f"{_base_url(configuration)}/list",
            headers=headers,
        )
    except RuntimeError as error:
        print(f"{error}; run `am-msgd start`", file=sys.stderr)
        return 1
    order = {"online": 0, "empty": 1, "historical": 2}
    output = [
        {
            "name": row["name"],
            "proj": row["project"],
            "status": row["status"],
        }
        for row in rows
    ]
    output.sort(
        key=lambda row: (
            order.get(row["status"], 99),
            row["proj"],
            row["name"],
        )
    )
    if args.json:
        print(json.dumps(output, ensure_ascii=False))
    else:
        print("NAME\tPROJ\tSTATUS")
        for row in output:
            print(f"{row['name']}\t{row['proj']}\t{row['status']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="am-msgd")
    mutation = parser.add_mutually_exclusive_group()
    mutation.add_argument("--bind", dest="bind_address")
    mutation.add_argument("--unbind", dest="unbind_address")
    mutation.add_argument("--local-only", action="store_true")

    subparsers = parser.add_subparsers(dest="command")
    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--config", type=Path, default=None)
    serve_parser.add_argument("--port", type=int, default=None)
    serve_parser.add_argument(
        "--bind",
        dest="serve_binds",
        action="append",
        default=None,
    )
    serve_parser.add_argument("--no-mdns", action="store_true")

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--json", action="store_true")
    subparsers.add_parser("start")
    subparsers.add_parser("stop")
    subparsers.add_parser("restart")
    list_parser = subparsers.add_parser("agent-list")
    list_parser.add_argument("--json", action="store_true")
    return parser


def _dispatch(args, parser, meeting_home: Path) -> int:
    if args.command == "serve":
        serve(
            configuration_path=args.config,
            port=args.port,
            binds=(
                tuple(args.serve_binds)
                if args.serve_binds is not None
                else None
            ),
            no_mdns=args.no_mdns,
        )
        return 0
    if args.command == "status":
        return _status(args, meeting_home)
    if args.command in {"start", "stop", "restart"}:
        return _run_lifecycle(args.command, meeting_home)
    if args.command == "agent-list":
        return _agent_list(args, meeting_home)
    if args.bind_address or args.unbind_address or args.local_only:
        return _mutate_configuration(args, meeting_home)
    parser.error(
        "choose serve, status, start, stop, restart, agent-list, "
        "--bind, --unbind, or --local-only"
    )
    return 2


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _dispatch(args, parser, _meeting_home())
    except (RuntimeError, ValueError) as error:
        print(f"am-msgd: {error}", file=sys.stderr)
        return 1


__all__ = ["build_parser", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
