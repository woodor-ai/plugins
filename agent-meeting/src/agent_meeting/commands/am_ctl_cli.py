"""Public ``am-ctl`` lifecycle control CLI."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from agent_meeting.lifecycle_control.user_service import (
    start_lifecycle_control_service,
    stop_lifecycle_control_service,
)

API_URL = f"http://127.0.0.1:{os.environ.get('AM_CTLD_PORT', '8789')}"


def _control_home() -> Path:
    meeting_home = Path(
        os.environ.get("MEETING_HOME") or (Path.home() / ".agent-meeting")
    )
    return meeting_home / "control"


def _token() -> str:
    try:
        return (_control_home() / "token").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _request(method: str, path: str, body: dict | None = None, timeout=3) -> dict:
    payload = (
        json.dumps(body, ensure_ascii=False).encode("utf-8")
        if body is not None
        else None
    )
    request = urllib.request.Request(
        API_URL + path,
        data=payload,
        method=method,
        headers={
            "Authorization": f"Bearer {_token()}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read().decode("utf-8")).get("error")
        except Exception:
            detail = str(error)
        raise RuntimeError(detail or str(error)) from error


def _healthy() -> bool:
    try:
        return bool(_request("GET", "/health", timeout=1).get("ok"))
    except Exception:
        return False


def start_service() -> int:
    if _healthy():
        print("am-ctld is already running")
        return 0
    home = _control_home()
    meeting_home = home.parent
    if start_lifecycle_control_service(meeting_home):
        for _ in range(50):
            if _healthy():
                print("am-ctld started")
                return 0
            time.sleep(0.1)
        print("am-ctld service failed to start", file=sys.stderr)
        return 1
    home.mkdir(parents=True, exist_ok=True)
    log = open(home / "am-ctld.log", "a", encoding="utf-8")
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": log,
        "stderr": log,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(
        [
            sys.executable,
            "-m",
            "agent_meeting.lifecycle_control.controller_process",
        ],
        **kwargs,
    )
    for _ in range(50):
        if _healthy():
            print("am-ctld started")
            return 0
        time.sleep(0.1)
    print("am-ctld failed to start; see ~/.agent-meeting/control/am-ctld.log", file=sys.stderr)
    return 1


def stop_service() -> int:
    if not _healthy():
        print("am-ctld is not running")
        return 0
    meeting_home = _control_home().parent
    if not stop_lifecycle_control_service(meeting_home):
        _request("POST", "/shutdown", {})
    for _ in range(50):
        if not _healthy():
            print("am-ctld stopped")
            return 0
        time.sleep(0.1)
    print("am-ctld did not stop in time", file=sys.stderr)
    return 1


def print_status(*, json_output: bool = False) -> int:
    if not _healthy():
        if json_output:
            print(json.dumps({"ok": False, "running": False, "sessions": []}))
            return 1
        print("am-ctld: stopped")
        return 1
    health = _request("GET", "/health")
    sessions = _request("GET", "/sessions").get("sessions", [])
    if json_output:
        print(
            json.dumps(
                {
                    "ok": True,
                    "running": True,
                    "pid": health.get("pid"),
                    "sessions": sessions,
                },
                ensure_ascii=False,
            )
        )
        return 0
    print(
        f"am-ctld: running pid={health.get('pid')} "
        f"sessions={len(sessions)}"
    )
    for item in sessions:
        identity = item.get("identity") or item.get("instance_id")
        print(
            f"{identity}\t{item.get('platform')}\t{item.get('state')}\t"
            f"{item.get('cwd') or ''}"
        )
    return 0


def agent_command(args) -> int:
    if not _healthy():
        raise RuntimeError("am-ctld is not running")
    result = _request(
        "POST",
        "/agent/action",
        {"name": args.name, "project": args.proj, "cmd": args.cmd},
        timeout=240,
    )
    if args.cmd == "status":
        print(json.dumps(result["session"], ensure_ascii=False, indent=2))
    else:
        print(json.dumps(result, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="am-ctl")
    subparsers = parser.add_subparsers(dest="command", required=True)
    status = subparsers.add_parser("status")
    status.add_argument("--json", action="store_true")
    for command in ("start", "stop", "restart", "help", "update"):
        subparsers.add_parser(command)
    agent = subparsers.add_parser("agent")
    agent.add_argument("--name", required=True)
    agent.add_argument("--proj", required=True)
    agent.add_argument(
        "--cmd",
        required=True,
        choices=("status", "compact", "clear", "handoff", "exit", "restart"),
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            return print_status(json_output=args.json)
        if args.command == "start":
            return start_service()
        if args.command == "stop":
            return stop_service()
        if args.command == "restart":
            stopped = stop_service()
            return stopped or start_service()
        if args.command == "update":
            return subprocess.call(["am-update"])
        if args.command == "help":
            parser.print_help()
            return 0
        if args.command == "agent":
            return agent_command(args)
    except (RuntimeError, OSError, urllib.error.URLError) as error:
        print(f"am-ctl: {error}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
