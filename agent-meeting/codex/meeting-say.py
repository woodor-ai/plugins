#!/usr/bin/env python3
"""Send an agent-meeting message from the current brokered Codex thread."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


HOME = Path.home()
DATA = Path(os.environ.get("MEETING_HOME") or (HOME / ".agent-meeting"))
MEETING_CLI = DATA / "bin" / "meeting"
BROKER_API_PORT = int(os.environ.get("MEETING_BROKER_API_PORT", "8788"))
BROKER_BASE = f"http://127.0.0.1:{BROKER_API_PORT}"


def meeting_command(*args):
    return ([] if not sys.platform.startswith("win") else [sys.executable]) + [
        str(MEETING_CLI),
        *args,
    ]


def broker_identity(thread_id):
    if not thread_id:
        return {}
    url = BROKER_BASE + "/identity?" + urllib.parse.urlencode({"thread_id": thread_id})
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, ValueError):
        return {}


def main():
    parser = argparse.ArgumentParser(prog="meeting-say")
    parser.add_argument("--self", dest="self_identity", default="")
    parser.add_argument("--control-url", default="")
    parser.add_argument("peer")
    parser.add_argument("body", nargs="*")
    args = parser.parse_args()

    body = " ".join(args.body) if args.body else (
        sys.stdin.read() if not sys.stdin.isatty() else ""
    )
    if not body.strip():
        raise SystemExit("meeting-say: empty body")

    broker = broker_identity(os.environ.get("CODEX_THREAD_ID", ""))
    self_identity = args.self_identity or broker.get("identity") or ""
    control_url = args.control_url or broker.get("control_url") or ""
    if not self_identity:
        raise SystemExit(
            "meeting-say: the current CODEX_THREAD_ID is not registered with the "
            "local broker; pass --self name@project explicitly"
        )
    if not control_url:
        raise SystemExit(
            "meeting-say: no central control URL is available; pass --control-url"
        )

    file_descriptor, body_path = tempfile.mkstemp(
        suffix=".txt", prefix="meeting-say-"
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as body_file:
            body_file.write(body)
        command = meeting_command(
            "send",
            self_identity,
            args.peer,
            f"--body-file={body_path}",
            "--kind=回应",
            "--host",
            control_url,
        )
        kwargs = (
            {"creationflags": 0x08000000}
            if sys.platform.startswith("win")
            else {}
        )
        result = subprocess.run(command, capture_output=True, text=True, **kwargs)
        if result.stdout:
            sys.stdout.write(result.stdout)
        if result.returncode != 0:
            sys.stderr.write(result.stderr or "meeting-say: send failed\n")
            raise SystemExit(result.returncode)
    finally:
        try:
            os.unlink(body_path)
        except OSError:
            pass


if __name__ == "__main__":
    main()
