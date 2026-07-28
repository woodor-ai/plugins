#!/usr/bin/env python3
"""
Launch one live Codex session through the machine-wide am-codexd daemon.

The launcher owns only the foreground TUI lease. The daemon owns the shared
Codex app-server, central meeting subscriptions, ordered message queues, and
thread mappings. Exiting one launcher releases one lease and never stops the
daemon or app-server.
"""

import argparse
import json
import os
import re
import signal
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlparse

from mycodex import __version__

HOME = Path.home()
DATA = Path(os.environ.get("MEETING_HOME") or (HOME / ".agent-meeting"))
CODEX_DIR = DATA / "codex"
LAUNCHER_JSON = CODEX_DIR / "launcher.json"
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
IS_WINDOWS = sys.platform.startswith("win")
if IS_WINDOWS:
    from mycodex.operating_systems.windows import codex_terminal_title
else:
    from mycodex.operating_systems.macos import codex_terminal_title
if __package__:
    DAEMON_COMMAND = Path(
        shutil.which("am-codexd")
        or (
            DATA
            / "bin"
            / ("am-codexd.exe" if IS_WINDOWS else "am-codexd")
        )
    )
else:
    DAEMON_COMMAND = PLUGIN_ROOT / "bin" / "am-codexd"
BROKER_API_PORT = int(os.environ.get("MEETING_BROKER_API_PORT", "8788"))
BROKER_BASE = f"http://127.0.0.1:{BROKER_API_PORT}"

sys.path.insert(0, str(DATA / "bin"))
try:
    import am_common
except ImportError:
    try:
        from agent_meeting.messaging import project_identity

        class _PackagedAmIdentity:
            validate_proj = staticmethod(project_identity.validate_project)
            _project_root = staticmethod(project_identity._project_root)

            @staticmethod
            def proj_cache_set(root, project):
                project_identity.proj_cache_set(
                    root,
                    project,
                    meeting_home=str(DATA),
                )

            @staticmethod
            def resolve_authoritative_project(cwd, explicit_project):
                return project_identity.resolve_authoritative_project(
                    cwd,
                    explicit_project,
                    meeting_home=str(DATA),
                )

        am_common = _PackagedAmIdentity()
    except ImportError:
        am_common = None


def log(message):
    print(f"[mycodex] {time.strftime('%H:%M:%S')} {message}", flush=True)


def default_control_url():
    try:
        return (
            json.loads(LAUNCHER_JSON.read_text(encoding="utf-8")).get("control_url")
            or ""
        ).strip()
    except Exception:
        return ""


def default_name():
    host = re.sub(
        r"[^A-Za-z0-9-]", "-", socket.gethostname().split(".")[0]
    ).strip("-") or "host"
    return f"codex-{host}"[:20]


def venv_python():
    if IS_WINDOWS:
        candidate = DATA / "venv" / "Scripts" / "python.exe"
    else:
        candidate = DATA / "venv" / "bin" / "python"
    return str(candidate if candidate.exists() else Path(sys.executable))


def installed_plugin_version():
    try:
        manifest = json.loads(
            (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(
                encoding="utf-8"
            )
        )
        return str(manifest.get("version") or "unknown")
    except Exception:
        return __version__


def broker_request(method, path, body=None, params=None, timeout=45):
    url = BROKER_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            payload = {"error": raw.decode("utf-8", "replace") or str(exc)}
        raise RuntimeError(payload.get("error") or str(exc))
    return json.loads(raw.decode("utf-8")) if raw else {}


def ensure_daemon():
    if not DAEMON_COMMAND.exists():
        raise RuntimeError(f"am-codexd command not found at {DAEMON_COMMAND}")
    if __package__:
        command = [str(DAEMON_COMMAND), "update"]
    else:
        command = [venv_python(), str(DAEMON_COMMAND), "update"]
    result = subprocess.run(command, capture_output=True, text=True)
    detail = (result.stderr or result.stdout or "").strip()
    if result.returncode != 0:
        raise RuntimeError(detail or "am-codexd update failed")
    status = broker_request("GET", "/health", timeout=2)
    if not status.get("ok"):
        raise RuntimeError("am-codexd is not healthy after update")
    expected = installed_plugin_version()
    running = str(status.get("version") or "unknown")
    if running != expected:
        raise RuntimeError(
            f"am-codexd version mismatch after update: "
            f"running {running}, installed {expected}"
        )
    if detail:
        log(detail)


def build_codex_launch_cmd(proxy_url):
    return ["codex", "--remote", proxy_url]


DEFAULT_TITLE = "codex"
TITLE_REFRESH_INTERVAL_S = 5.0


def title_text(name, project, control_url):
    label = name if (not project or project == "*") else f"{name}@{project}"
    hostport = ""
    if control_url:
        parsed = urlparse(control_url)
        if parsed.hostname and parsed.port:
            hostport = f"{parsed.hostname}:{parsed.port}"
    return f"{label} | {hostport}" if hostport else label


def set_terminal_title(title):
    codex_terminal_title.set_title(title)


class TitlePinner:
    def __init__(self, title):
        self.title = title
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        set_terminal_title(self.title)
        self.thread.start()

    def run(self):
        while not self.stop_event.wait(TITLE_REFRESH_INTERVAL_S):
            set_terminal_title(self.title)

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=2)


class Launcher:
    def __init__(self, name, project, control_url):
        self.name = name
        self.project = project
        self.control_url = control_url
        self.launch_id = uuid.uuid4().hex
        self.session = None
        self.torn_down = False

    def setup(self):
        ensure_daemon()
        self.session = broker_request(
            "POST",
            "/session/start",
            {
                "launch_id": self.launch_id,
                "name": self.name,
                "project": self.project,
                "cwd": os.getcwd(),
                "control_url": self.control_url,
            },
        )
        log(
            f"registered {self.session['identity']} "
            "(shared app-server; TUI will create the thread)"
        )

    def run_codex(self):
        command = build_codex_launch_cmd(self.session["proxy_url"])
        pinner = TitlePinner(title_text(self.name, self.project, self.control_url))
        pinner.start()
        log(f"launching foreground: {' '.join(command)}")
        try:
            subprocess.run(command)
        except FileNotFoundError:
            raise RuntimeError("`codex` was not found on PATH")
        finally:
            pinner.stop()

    def hold(self, stop_event):
        log("--no-codex: daemon lease active; waiting for SIGINT/SIGTERM")
        while not stop_event.wait(0.5):
            try:
                status = broker_request(
                    "GET",
                    "/session",
                    params={"launch_id": self.launch_id},
                    timeout=2,
                )
            except Exception:
                continue
            if not status.get("active", False):
                raise RuntimeError(
                    status.get("central_error")
                    or "daemon session became inactive"
                )

    def teardown(self):
        if self.torn_down:
            return
        self.torn_down = True
        set_terminal_title(DEFAULT_TITLE)
        if self.session is not None:
            try:
                broker_request(
                    "POST",
                    "/session/stop",
                    {"launch_id": self.launch_id},
                    timeout=20,
                )
                log(f"released {self.session['identity']} (am-codexd remains running)")
            except Exception as exc:
                log(f"session release failed: {exc}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="mycodex")
    parser.add_argument("name", nargs="?", default=None)
    parser.add_argument("--control-url", default="")
    parser.add_argument(
        "--proj",
        default=None,
        help="explicit project identity, cached for this repository",
    )
    parser.add_argument(
        "--global",
        dest="is_global",
        action="store_true",
        help="register a global identity instead of a project identity",
    )
    parser.add_argument("--no-codex", action="store_true")
    args = parser.parse_args(argv)

    if am_common is None:
        raise SystemExit(
            f"am_common is unavailable in {DATA / 'bin'}; reinstall agent-meeting"
        )
    if args.proj is not None and args.is_global:
        raise SystemExit("--proj and --global are mutually exclusive")

    if args.proj is not None:
        try:
            project = am_common.validate_proj(args.proj)
        except ValueError as exc:
            raise SystemExit(str(exc))
        root = am_common._project_root(os.getcwd())
        am_common.proj_cache_set(root, project)
        log(f"cached project identity {project} for {root}")
    elif args.is_global:
        project = "*"
    else:
        project = am_common.resolve_authoritative_project(os.getcwd(), None)
        if project is None:
            raise SystemExit(
                "no project identity is configured for this repository; "
                "run mycodex <name> --proj <project> once, or use --global"
            )

    control_url = (args.control_url or default_control_url()).strip()
    if not control_url:
        raise SystemExit(
            "no central control URL is configured; reinstall agent-meeting or pass "
            "--control-url http://<host>:8765"
        )

    launcher = Launcher(args.name or default_name(), project, control_url)
    stop_event = threading.Event()

    def stop(_signum, _frame):
        stop_event.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(signum, stop)
        except (ValueError, OSError):
            pass

    try:
        launcher.setup()
        if args.no_codex:
            launcher.hold(stop_event)
        else:
            launcher.run_codex()
    except Exception as exc:
        log(f"ERROR: {exc}")
        raise SystemExit(1)
    finally:
        launcher.teardown()


if __name__ == "__main__":
    main()
