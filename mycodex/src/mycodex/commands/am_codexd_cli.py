#!/usr/bin/env python3
"""Manage the machine-wide agent-meeting Codex daemon."""

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from mycodex import __version__

HOME = Path.home()
DATA = Path(os.environ.get("MEETING_HOME") or (HOME / ".agent-meeting"))
CODEX_DIR = DATA / "codex"
LOG_PATH = CODEX_DIR / "logs" / "am-codexd.log"
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
DAEMON_MODULE_PATH = PLUGIN_ROOT / "codex" / "am_codexd.py"
API_PORT = int(os.environ.get("MEETING_BROKER_API_PORT", "8788"))
API_BASE = f"http://127.0.0.1:{API_PORT}"
IS_WINDOWS = sys.platform.startswith("win")
if IS_WINDOWS:
    from mycodex.operating_systems.windows import codex_background_process
else:
    from mycodex.operating_systems.macos import codex_background_process


def daemon_module():
    if __package__:
        from mycodex.codex_session_broker import broker_process

        return broker_process
    spec = importlib.util.spec_from_file_location("am_codexd_runtime", DAEMON_MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load daemon module: {DAEMON_MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def installed_version():
    return getattr(daemon_module(), "DAEMON_VERSION", __version__)


def venv_python():
    return codex_background_process.legacy_runtime_python(DATA)


def request(method, path, timeout=2):
    req = urllib.request.Request(
        API_BASE + path,
        data=b"{}" if method == "POST" else None,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        payload = json.loads(raw.decode("utf-8")) if raw else {}
        if path != "/health":
            raise RuntimeError(payload.get("error") or str(exc))
        return payload
    return json.loads(raw.decode("utf-8")) if raw else {}


def status_info():
    try:
        return request("GET", "/health", timeout=1)
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return {}


def wait_until(predicate, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def spawn_daemon():
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(LOG_PATH, "a", encoding="utf-8")
    kwargs = codex_background_process.detached_popen_options(log_file)
    if __package__:
        command = [
            venv_python(),
            "-m",
            "mycodex.commands.am_codexd_cli",
            "_serve",
        ]
    else:
        command = [venv_python(), str(Path(__file__).resolve()), "_serve"]
    return subprocess.Popen(command, **kwargs)


def print_status(info):
    expected = installed_version()
    if not info:
        print("status: stopped")
        print(f"installed version: {expected}")
        print(f"api: {API_BASE}")
        return
    print(f"status: {'running' if info.get('ok') else 'unhealthy'}")
    print(f"pid: {info.get('pid', 'unknown')}")
    print(f"running version: {info.get('version', 'unknown')}")
    print(f"installed version: {expected}")
    print(f"active sessions: {int(info.get('sessions') or 0)}")
    print(f"api: {API_BASE}")


def start():
    info = status_info()
    expected = installed_version()
    if info:
        running = str(info.get("version") or "unknown")
        if running != expected:
            raise RuntimeError(
                f"am-codexd {running} is already running; "
                f"run `am-codexd update` to activate {expected}"
            )
        if not info.get("ok"):
            if wait_until(lambda: bool(status_info().get("ok"))):
                info = status_info()
                running = str(info.get("version") or "unknown")
                if running != expected:
                    raise RuntimeError(
                        f"am-codexd {running} won the startup race; "
                        f"run `am-codexd update` to activate {expected}"
                    )
                print(
                    f"am-codexd {running} is already running "
                    f"(pid {info.get('pid', 'unknown')})"
                )
                return
            raise RuntimeError(f"am-codexd {running} did not become healthy")
        print(f"am-codexd {running} is already running (pid {info.get('pid', 'unknown')})")
        return

    process = spawn_daemon()

    def ready():
        return bool(status_info().get("ok"))

    if not wait_until(ready):
        if process.poll() is not None:
            raise RuntimeError(
                f"am-codexd exited during startup (rc={process.returncode}); "
                f"see {LOG_PATH}"
            )
        raise RuntimeError(f"am-codexd did not become healthy; see {LOG_PATH}")
    info = status_info()
    running = str(info.get("version") or "unknown")
    if running != expected:
        raise RuntimeError(
            f"am-codexd {running} won the startup race; "
            f"run `am-codexd update` to activate {expected}"
        )
    print(f"started am-codexd {running} (pid {info.get('pid')})")


def stop():
    info = status_info()
    if not info:
        print("am-codexd is not running")
        return
    sessions = int(info.get("sessions") or 0)
    if sessions:
        raise RuntimeError(
            f"cannot stop am-codexd while {sessions} mycodex session(s) are active"
        )
    request("POST", "/shutdown", timeout=3)
    if not wait_until(lambda: not status_info(), timeout=12):
        raise RuntimeError("am-codexd did not stop")
    print("stopped am-codexd")


def restart():
    stop()
    start()


def update():
    info = status_info()
    expected = installed_version()
    if not info:
        start()
        return
    running = str(info.get("version") or "unknown")
    if running == expected and info.get("ok"):
        print(f"am-codexd is already up to date ({expected})")
        return
    if running == expected:
        def expected_version_is_healthy():
            current = status_info()
            return (
                bool(current.get("ok"))
                and str(current.get("version") or "unknown") == expected
            )

        if wait_until(expected_version_is_healthy):
            print(f"am-codexd is already up to date ({expected})")
            return
    sessions = int(info.get("sessions") or 0)
    if sessions:
        raise RuntimeError(
            f"cannot update am-codexd from {running} to {expected} while "
            f"{sessions} mycodex session(s) are active"
        )
    if running == expected:
        print(f"restarting unhealthy am-codexd {running}")
    else:
        print(f"updating am-codexd {running} -> {expected}")
    stop()
    start()


def build_parser():
    parser = argparse.ArgumentParser(
        prog="am-codexd",
        description="Manage the machine-wide agent-meeting Codex daemon.",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("status", help="show daemon status and version")
    subparsers.add_parser("start", help="start the daemon")
    subparsers.add_parser("stop", help="stop the daemon when no sessions are active")
    subparsers.add_parser("restart", help="restart the daemon")
    subparsers.add_parser(
        "update",
        help="activate the currently installed agent-meeting version",
    )
    return parser


def main(argv=None):
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list[:1] == ["_serve"]:
        daemon_module().serve()
        return 0

    parser = build_parser()
    args = parser.parse_args(args_list)
    if args.command is None:
        parser.print_help()
        return 0
    actions = {
        "status": lambda: print_status(status_info()),
        "start": start,
        "stop": stop,
        "restart": restart,
        "update": update,
    }
    try:
        actions[args.command]()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
