#!/usr/bin/env python3
"""
Launch one live Codex session through the machine-wide am-codexd daemon.

The launcher owns only the foreground TUI lease. The daemon owns the shared
Codex app-server, central meeting subscriptions, ordered message queues, and
thread mappings. Exiting one launcher releases one lease and never stops the
daemon or app-server.
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import signal
import shutil
import socket
import socketserver
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

from agent_meeting.clients import endpoint, hub_discovery
from agent_meeting.clients.am_process_client import run_am_cli
from agent_meeting.lifecycle_control.terminals import current_terminal_handle
from agent_meeting.messaging import project_identity

from mycodex import __version__
from mycodex.operating_systems import codex_cli_command

HOME = Path.home()
DATA = Path(os.environ.get("MEETING_HOME") or (HOME / ".agent-meeting"))
IS_WINDOWS = sys.platform.startswith("win")
if IS_WINDOWS:
    from mycodex.operating_systems.windows import codex_terminal_title
else:
    from mycodex.operating_systems.macos import codex_terminal_title
DAEMON_COMMAND = Path(
    shutil.which("am-codexd")
    or (
        DATA
        / "bin"
        / ("am-codexd.exe" if IS_WINDOWS else "am-codexd")
    )
)
AM_COMMAND = DATA / "bin" / ("am.exe" if IS_WINDOWS else "am")
BROKER_API_PORT = int(os.environ.get("MEETING_BROKER_API_PORT", "8788"))
BROKER_BASE = f"http://127.0.0.1:{BROKER_API_PORT}"
WRAPPER_DIR = DATA / "control" / "wrappers"

def log(message):
    print(f"[amcodex] {time.strftime('%H:%M:%S')} {message}", flush=True)


def default_control_url():
    control = hub_discovery.discover_control(
        lambda *args: run_am_cli(AM_COMMAND, *args, timeout=10)
    )
    return str(control.get("base_url") or "")


def normalize_am_msgd(value: str, default_port: int = 8765) -> str:
    """Normalize an amcodex am-msgd endpoint to its internal HTTP URL."""
    return endpoint.normalize_am_msgd(value, default_port)


def default_name():
    host = re.sub(
        r"[^A-Za-z0-9-]", "-", socket.gethostname().split(".")[0]
    ).strip("-") or "host"
    return f"codex-{host}"[:20]


def installed_plugin_version():
    return __version__


def daemon_versions_compatible(running: str, installed: str) -> bool:
    """Allow an active daemon to span patch releases within one minor line."""

    def major_minor(version: str) -> tuple[int, int] | None:
        release = version.split("+", 1)[0].split("-", 1)[0]
        parts = release.split(".")
        if len(parts) < 2 or not all(part.isdigit() for part in parts[:2]):
            return None
        return int(parts[0]), int(parts[1])

    running_line = major_minor(running)
    return (
        running_line is not None
        and running_line == major_minor(installed)
    )


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
    command = [str(DAEMON_COMMAND), "update", "--defer-if-active"]
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
        sessions = int(status.get("sessions") or 0)
        if sessions and daemon_versions_compatible(running, expected):
            if detail:
                log(detail)
            log(
                f"continuing with compatible am-codexd {running}; "
                f"{expected} will activate after {sessions} active "
                f"session(s) exit"
            )
            return
        raise RuntimeError(
            f"am-codexd version mismatch after update: "
            f"running {running}, installed {expected}"
        )
    if detail:
        log(detail)


CODEX_MODELS = {
    "sol": "gpt-5.6-sol",
    "terra": "gpt-5.6-terra",
}
CODEX_EFFORTS = ("xhigh", "high", "medium")


def build_codex_launch_cmd(
    proxy_url,
    *,
    model="gpt-5.6-sol",
    effort="high",
):
    """Build the managed Codex TUI invocation with its session settings."""
    return [
        "codex",
        "--remote",
        proxy_url,
        "--model",
        model,
        "--config",
        f'model_reasoning_effort="{effort}"',
        "--config",
        "tui.terminal_title=[]",
    ]


DEFAULT_TITLE = "codex"


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


class Launcher:
    def __init__(
        self,
        name,
        project,
        control_url,
        *,
        model="gpt-5.6-sol",
        effort="high",
    ):
        self.name = name
        self.project = project
        self.control_url = control_url
        self.model = model
        self.effort = effort
        self.launch_id = uuid.uuid4().hex
        self.session = None
        self.torn_down = False
        self.auth_token = secrets.token_urlsafe(32)
        self.started_at = int(time.time())
        self.process = None
        self.restart_requested = False
        self.stop_requested = False
        self.lock = threading.RLock()
        self.control_server = None
        self.control_thread = None
        self.descriptor_path = WRAPPER_DIR / f"amcodex-{self.launch_id}.json"

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

    def descriptor(self):
        with self.lock:
            process = self.process
            command = (
                build_codex_launch_cmd(
                    self.session["proxy_url"],
                    model=self.model,
                    effort=self.effort,
                )
                if self.session is not None
                else []
            )
            tty = None
            if not IS_WINDOWS and sys.stdin.isatty():
                try:
                    tty = os.ttyname(sys.stdin.fileno())
                except OSError:
                    pass
            terminal_handle = {
                **current_terminal_handle(),
                "tty": tty,
            }
            return {
                "schema_version": 1,
                "wrapper": "amcodex",
                "platform": "codex",
                "name": self.name,
                "project": self.project,
                "identity": f"{self.name}@{self.project}",
                "instance_id": self.launch_id,
                "launch_id": self.launch_id,
                "wrapper_pid": os.getpid(),
                "child_pid": (
                    process.pid
                    if process is not None and process.poll() is None
                    else None
                ),
                "cwd": os.getcwd(),
                "tty": tty,
                "terminal_handle": terminal_handle,
                "started_at": self.started_at,
                "status": (
                    "restarting"
                    if self.restart_requested
                    else "stopping"
                    if self.stop_requested
                    else "running"
                    if process is not None and process.poll() is None
                    else "exited"
                ),
                "control": {
                    "transport": "tcp",
                    "host": "127.0.0.1",
                    "port": (
                        self.control_server.server_address[1]
                        if self.control_server is not None
                        else None
                    ),
                    "token": self.auth_token,
                },
                "launch_recipe": {
                    "executable": command[0] if command else "codex",
                    "args_persisted": False,
                    "args_sha256": hashlib.sha256(
                        "\0".join(command[1:]).encode("utf-8")
                    ).hexdigest(),
                },
                "capabilities": [
                    "observe",
                    "interrupt",
                    "exit",
                    "restart_same_terminal",
                    *(
                        ["send_text"]
                        if terminal_handle.get("type") in {"tmux", "iterm2"}
                        else []
                    ),
                ],
            }

    def publish_descriptor(self):
        WRAPPER_DIR.mkdir(parents=True, exist_ok=True)
        try:
            WRAPPER_DIR.chmod(0o700)
        except OSError:
            pass
        temporary = self.descriptor_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self.descriptor(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, self.descriptor_path)

    def interrupt(self, count=2):
        with self.lock:
            process = self.process
        if process is None or process.poll() is not None:
            return False
        delivered = False
        for index in range(max(1, count)):
            try:
                if IS_WINDOWS:
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    process.send_signal(signal.SIGINT)
                delivered = True
            except (OSError, ProcessLookupError):
                if not delivered:
                    return False
                break
            if index + 1 < count:
                time.sleep(0.2)
        return delivered

    def request_exit(self):
        with self.lock:
            self.stop_requested = True
            self.restart_requested = False
            process = self.process
        self.publish_descriptor()
        delivered = self.interrupt(2)
        if not delivered:
            return False
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process is None or process.poll() is not None:
                return True
            time.sleep(0.1)
        return False

    def request_restart(self):
        with self.lock:
            self.restart_requested = True
            self.stop_requested = False
            previous = self.process
            previous_pid = previous.pid if previous is not None else None
        self.publish_descriptor()
        delivered = self.interrupt(2)
        if not delivered:
            return False
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            with self.lock:
                current = self.process
            if (
                current is not None
                and current.pid != previous_pid
                and current.poll() is None
            ):
                return True
            time.sleep(0.1)
        return False

    def start_control_server(self):
        launcher = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                try:
                    request = json.loads(self.rfile.readline().decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    request = {}
                if request.get("token") != launcher.auth_token:
                    response = {"ok": False, "error": "unauthorized"}
                elif request.get("cmd") == "status":
                    response = {"ok": True, "session": launcher.descriptor()}
                elif request.get("cmd") == "exit":
                    response = {"ok": launcher.request_exit()}
                elif request.get("cmd") == "restart":
                    response = {"ok": launcher.request_restart()}
                else:
                    response = {"ok": False, "error": "unsupported command"}
                self.wfile.write(
                    (json.dumps(response, ensure_ascii=False) + "\n").encode(
                        "utf-8"
                    )
                )

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = False
            daemon_threads = True

        self.control_server = Server(("127.0.0.1", 0), Handler)
        self.control_thread = threading.Thread(
            target=self.control_server.serve_forever,
            name=f"amcodex-control-{self.launch_id}",
            daemon=True,
        )
        self.control_thread.start()

    def run_codex(self):
        logical_command = build_codex_launch_cmd(
            self.session["proxy_url"],
            model=self.model,
            effort=self.effort,
        )
        command = codex_cli_command.resolve(logical_command[1:])
        set_terminal_title(title_text(self.name, self.project, self.control_url))
        log(f"launching Codex foreground; am-msgd={self.control_url}")
        self.start_control_server()
        try:
            while True:
                kwargs = {}
                if IS_WINDOWS:
                    kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
                with self.lock:
                    self.restart_requested = False
                    self.process = subprocess.Popen(command, **kwargs)
                self.publish_descriptor()
                self.process.wait()
                with self.lock:
                    self.process = None
                    should_restart = (
                        self.restart_requested and not self.stop_requested
                    )
                self.publish_descriptor()
                if not should_restart:
                    break
        except FileNotFoundError:
            raise RuntimeError("`codex` was not found on PATH")
        finally:
            if self.control_server is not None:
                self.control_server.shutdown()
                self.control_server.server_close()
                self.control_server = None
            try:
                self.descriptor_path.unlink()
            except FileNotFoundError:
                pass

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
        with self.lock:
            process = self.process
        if process is not None and process.poll() is None:
            self.interrupt(2)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
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
    parser = argparse.ArgumentParser(prog="amcodex")
    parser.add_argument("name", nargs="?", default=None)
    parser.add_argument(
        "--am-msgd",
        default="",
        metavar="HOST[:PORT]",
        help="am-msgd address; defaults to port 8765",
    )
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
    parser.add_argument(
        "--model",
        choices=CODEX_MODELS,
        default="sol",
        help="Codex model variant (default: sol)",
    )
    parser.add_argument(
        "--effort",
        choices=CODEX_EFFORTS,
        default="high",
        help="reasoning effort (default: high)",
    )
    parser.add_argument("--no-codex", action="store_true")
    args = parser.parse_args(argv)

    if args.proj is not None and args.is_global:
        raise SystemExit("--proj and --global are mutually exclusive")

    if args.proj is not None:
        try:
            project = project_identity.validate_project(args.proj)
        except ValueError as exc:
            raise SystemExit(str(exc))
        root = project_identity._project_root(os.getcwd())
        project_identity.proj_cache_set(
            root,
            project,
            meeting_home=str(DATA),
        )
        log(f"cached project identity {project} for {root}")
    elif args.is_global:
        project = "*"
    else:
        project = project_identity.resolve_authoritative_project(
            os.getcwd(),
            None,
            meeting_home=str(DATA),
        )
        if project is None:
            raise SystemExit(
                "no project identity is configured for this repository; "
                "run amcodex <name> --proj <project> once, or use --global"
            )

    endpoint = args.am_msgd or default_control_url()
    if not endpoint.strip():
        raise SystemExit(
            "no central am-msgd is configured; reinstall agent-meeting or pass "
            "--am-msgd <host>[:port]"
        )
    try:
        control_url = normalize_am_msgd(endpoint)
    except ValueError as error:
        raise SystemExit(f"invalid am-msgd endpoint: {error}") from error

    launcher = Launcher(
        args.name or default_name(),
        project,
        control_url,
        model=CODEX_MODELS[args.model],
        effort=args.effort,
    )
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
