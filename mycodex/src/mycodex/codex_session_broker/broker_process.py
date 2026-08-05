#!/usr/bin/env python3
"""
Machine-wide agent-meeting daemon for Codex.

One daemon owns one Codex app-server and multiplexes every local mycodex
session onto separate Codex threads. It also replaces the old per-session
codex-bridge process: each meeting identity has its own central subscription,
ordered inbox cursor, pending batch, and Codex injection target.

The broker exposes:

* localhost HTTP on 127.0.0.1:8788 for launcher/session management and
  CODEX_THREAD_ID -> meeting identity lookup;
* one ephemeral localhost WebSocket port per active session as a session-aware
  proxy between that Codex TUI and the shared app-server.

The proxy is load-bearing. It observes thread/start and thread/resume traffic,
so /clear, /compact, resume, and fork can update the meeting identity mapping
without a shared runtime.json or a SessionStart hook.

Codex CLI accepts only ``ws://host:port`` for ``--remote`` (no URL path).
Giving every lease its own loopback listener preserves session routing without
creating another process or another app-server.
"""

import asyncio
import contextlib
import json
import os
import platform
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mycodex import __version__
from mycodex.codex_session_broker import (
    hub_inbox_delivery,
    session_lease_registry,
    tui_websocket_proxy,
)
from mycodex.operating_systems import codex_cli_command

try:
    import websockets
except ImportError:
    websockets = None


# The central am-msgd is a LAN or private-overlay control plane.  System proxy
# discovery is unsuitable here: it can route private control traffic through a
# local proxy, and recent ``websockets`` releases require the optional
# ``python-socks`` package before they can even connect to that proxy.  Keep
# this traffic direct, matching the am CLI and Claude subscription client.
CENTRAL_HTTP_PROXY_HANDLER = urllib.request.ProxyHandler({})
CENTRAL_HTTP_OPENER = urllib.request.build_opener(CENTRAL_HTTP_PROXY_HANDLER)


def codex_app_server_environment(base_environment=None):
    """Mark the shared app-server as a mycodex-managed Codex runtime.

    Codex runs the agent-meeting SessionStart hook in the app-server process.
    It does not provide a per-thread identifier to that hook, so preserve an
    explicit runtime marker for the hook to distinguish this path from a
    standalone Claude Code session.
    """
    environment = dict(os.environ if base_environment is None else base_environment)
    environment["AGENT_MEETING_CODEX_RUNTIME"] = "1"
    return environment


def central_websocket_options(headers):
    """Return transport options for a direct central-hub subscription."""
    return {
        "additional_headers": headers,
        "max_size": None,
        "ping_interval": 5,
        "ping_timeout": 15,
        "proxy": None,
    }

if sys.platform.startswith("win"):
    from mycodex.operating_systems.windows import codex_background_process
else:
    from mycodex.operating_systems.macos import codex_background_process


HOME = Path.home()
DATA = Path(os.environ.get("MEETING_HOME") or (HOME / ".agent-meeting"))
CODEX_DIR = DATA / "codex"
LOGS_DIR = CODEX_DIR / "logs"
LEGACY_STATE_FILE = CODEX_DIR / "broker-state.json"
CONFIG_FILE = DATA / "config.json"
PLUGIN_ROOT = Path(__file__).resolve().parent.parent


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


DAEMON_VERSION = installed_plugin_version()

API_HOST = "127.0.0.1"
API_PORT = int(os.environ.get("MEETING_BROKER_API_PORT", "8788"))
PROXY_HOST = "127.0.0.1"
APP_PORT_FIRST = int(os.environ.get("MEETING_BROKER_APP_PORT_FIRST", "8792"))
APP_PORT_LAST = int(os.environ.get("MEETING_BROKER_APP_PORT_LAST", "8841"))

INJECT_POLL_S = 2.0
CONTROL_STALE_S = 600
STEER_DEBOUNCE_S = float(os.environ.get("MEETING_STEER_DEBOUNCE_S", "3"))
STEER_COOLDOWN_S = float(os.environ.get("MEETING_STEER_COOLDOWN_S", "30"))
STEER_MAX_PER_TURN = int(
    os.environ.get("MEETING_STEER_MAX_PER_TURN", "3")
)
STEER_MAX_MESSAGES = int(
    os.environ.get("MEETING_STEER_MAX_MESSAGES", "100")
)
IDLE_MAX_MESSAGES = int(
    os.environ.get("MEETING_IDLE_MAX_MESSAGES", "100")
)
IDLE_DIGEST_THRESHOLD = int(
    os.environ.get("MEETING_IDLE_DIGEST_THRESHOLD", "10")
)


class NameTakenError(RuntimeError):
    pass


def log(message):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    line = f"[am-codexd] {stamp} {message}"
    print(line, flush=True)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {} if default is None else default


def auth_token():
    return (read_json(CONFIG_FILE).get("auth_token") or "").strip()


def http_json(method, base_url, path, body=None, params=None, timeout=20):
    url = base_url.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    token = auth_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with CENTRAL_HTTP_OPENER.open(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            payload = {"error": raw.decode("utf-8", "replace") or str(exc)}
        payload["_http_status"] = exc.code
        return payload
    return json.loads(raw.decode("utf-8")) if raw else {}


def port_available(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def appserver_healthy(port):
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/healthz", timeout=1.0
        ) as response:
            return 200 <= response.status < 500
    except Exception:
        return False


class AppServer:
    def __init__(self):
        self.process = None
        self.port = None
        self.log_file = None
        self.ready = False

    @property
    def ws_url(self):
        if self.port is None:
            raise RuntimeError("app-server is not running")
        return f"ws://127.0.0.1:{self.port}"

    def start(self):
        if (
            self.ready
            and self.process is not None
            and self.process.poll() is None
            and appserver_healthy(self.port)
        ):
            return
        self.ready = False
        port = next((p for p in range(APP_PORT_FIRST, APP_PORT_LAST + 1) if port_available(p)), None)
        if port is None:
            raise RuntimeError("no free port available for the shared Codex app-server")
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        self.log_file = open(LOGS_DIR / "app-server.log", "a", encoding="utf-8")
        ws_url = f"ws://127.0.0.1:{port}"
        kwargs = codex_background_process.detached_popen_options(
            self.log_file
        )
        kwargs["env"] = codex_app_server_environment()
        self.process = subprocess.Popen(
            codex_cli_command.resolve(
                ["app-server", "--listen", ws_url]
            ),
            **kwargs,
        )
        self.port = port
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"Codex app-server exited during startup (rc={self.process.returncode})"
                )
            if appserver_healthy(port):
                # A child can bind and answer /healthz briefly before a later
                # startup validation fails. Require it to remain healthy across
                # a short stability window before publishing broker readiness.
                time.sleep(0.35)
                if (
                    self.process.poll() is None
                    and appserver_healthy(port)
                ):
                    log(
                        f"shared Codex app-server ready at {ws_url} "
                        f"pid={self.process.pid}"
                    )
                    self.ready = True
                    return
            time.sleep(0.25)
        self.stop()
        raise RuntimeError("Codex app-server did not become healthy within 20 seconds")

    def stop(self):
        process = self.process
        self.ready = False
        self.process = None
        self.port = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
        if self.log_file is not None:
            self.log_file.close()
            self.log_file = None


Session = session_lease_registry.SessionLease


class Broker:
    def __init__(self):
        self.loop = None
        self.appserver = AppServer()
        self.sessions = {}
        self.thread_to_launch = {}
        self.legacy_cursors = (
            read_json(LEGACY_STATE_FILE, {"cursors": {}}).get("cursors") or {}
        )
        self.stop_event = asyncio.Event()
        self.accepting_sessions = True
        self.http_server = None
        self.scheduler_task = None
        self.supervisor_task = None

    async def app_call(self, method, params=None, timeout=30):
        ws_url = self.appserver.ws_url
        async with websockets.connect(ws_url, max_size=None, open_timeout=10) as ws:
            next_id = 1

            async def call(call_method, call_params=None, call_timeout=timeout):
                nonlocal next_id
                request_id = next_id
                next_id += 1
                request = {"id": request_id, "method": call_method}
                if call_params is not None:
                    request["params"] = call_params
                await ws.send(json.dumps(request))
                while True:
                    raw = await asyncio.wait_for(ws.recv(), call_timeout)
                    message = json.loads(raw)
                    if message.get("id") != request_id:
                        continue
                    if "error" in message:
                        error = message["error"]
                        raise RuntimeError(
                            f"{call_method} failed: {error.get('message') or error}"
                        )
                    return message.get("result") or {}

            await call(
                "initialize",
                {
                    "clientInfo": {
                        "name": "agent_meeting_broker",
                        "title": "Agent Meeting Codex Daemon",
                        "version": "1",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            )
            await ws.send(json.dumps({"method": "initialized", "params": {}}))
            return await call(method, params)

    @staticmethod
    def reconcile_central_cursor(session, central_cursor):
        hub_inbox_delivery.reconcile_central_cursor(
            session,
            central_cursor,
        )

    async def register_central(self, session, timeout=20):
        payload = {
            "project": session.project,
            "name": session.name,
            "cwd": session.cwd,
            "force": False,
            "role": "worker",
            "host": socket.gethostname(),
            "os": platform.system().lower(),
            "instance": session.launch_id,
            "client_version": read_json(CONFIG_FILE).get("plugin_version"),
        }
        legacy_cursor = self.legacy_cursors.get(session.identity)
        if legacy_cursor is None and session.project == "*":
            legacy_cursor = self.legacy_cursors.get(session.name)
        if legacy_cursor is not None:
            payload["legacy_cursor"] = legacy_cursor
        request_task = asyncio.create_task(
            asyncio.to_thread(
                http_json,
                "POST",
                session.control_url,
                "/register",
                payload,
                None,
                timeout,
            )
        )
        session.central_register_task = request_task
        try:
            # Cancelling the subscription must not discard the only handle to
            # a worker-thread request that may still commit centrally.
            result = await asyncio.shield(request_task)
        finally:
            if (
                request_task.done()
                and session.central_register_task is request_task
            ):
                session.central_register_task = None
        if result.get("error"):
            if result.get("code") == "name_taken":
                raise NameTakenError(result["error"])
            raise RuntimeError(result["error"])
        session.central_registered = True
        session.central_error = None
        self.reconcile_central_cursor(session, result["cursor"])
        return session.cursor

    async def unregister_central(self, session):
        # Cancelling an asyncio.to_thread registration does not stop its worker
        # thread. Always issue an instance-bound delete so a late commit cannot
        # leave a ghost lease after the local session has stopped.
        await asyncio.to_thread(
            http_json,
            "POST",
            session.control_url,
            "/unregister",
            {
                "project": session.project,
                "name": session.name,
                "instance": session.launch_id,
            },
        )
        session.central_registered = False

    async def fetch_inbox(self, session):
        async with session.delivery_lock:
            params = {
                "project": session.project,
                "name": session.name,
                "instance": session.launch_id,
                "limit": 500,
            }
            result = await asyncio.to_thread(
                http_json, "GET", session.control_url, "/inbox", None, params
            )
            if result.get("error"):
                raise RuntimeError(result["error"])
            self.reconcile_central_cursor(session, result["cursor"])
            for message in result.get("messages", []):
                message_id = int(message["id"])
                if message_id > session.cursor:
                    session.pending.setdefault(message_id, message)

    async def acknowledge(self, session, message_ids):
        async with session.delivery_lock:
            expected_cursor = session.cursor
            through = max(message_ids)
            result = await asyncio.to_thread(
                http_json,
                "POST",
                session.control_url,
                "/ack",
                {
                    "project": session.project,
                    "name": session.name,
                    "instance": session.launch_id,
                    "expected_cursor": expected_cursor,
                    "through": through,
                },
            )
            if result.get("error"):
                if result.get("code") == "cursor_conflict":
                    current_cursor = int(result["cursor"])
                    if current_cursor >= through:
                        session.cursor = current_cursor
                        return
                    if current_cursor == expected_cursor:
                        raise RuntimeError("ack outcome is not yet visible")
                    raise RuntimeError(
                        "central cursor advanced only partway through the batch "
                        f"({expected_cursor} -> {current_cursor} < {through})"
                    )
                raise RuntimeError(result["error"])
            session.cursor = int(result["cursor"])

    async def start_session(self, request):
        if not self.accepting_sessions:
            raise ValueError("am-codexd is shutting down")
        launch_id = str(request["launch_id"])
        name = str(request["name"])
        project = str(request["project"])
        cwd = os.path.abspath(str(request["cwd"]))
        control_url = str(request["control_url"]).rstrip("/")
        if not control_url.startswith("http://"):
            raise ValueError("control_url must use http://")
        if launch_id in self.sessions:
            raise ValueError(f"launch {launch_id} already exists")
        identity = f"{name}@{project}"
        if any(s.active and s.identity == identity for s in self.sessions.values()):
            raise ValueError(f"meeting identity {identity} is already active on this machine")

        session = Session(
            launch_id=launch_id,
            name=name,
            project=project,
            cwd=cwd,
            control_url=control_url,
            thread_id=None,
            cursor=None,
            proxy_host=PROXY_HOST,
        )
        self.sessions[launch_id] = session
        try:
            await self.start_session_proxy(session)
            try:
                await self.register_central(session, timeout=2)
                await self.fetch_inbox(session)
            except NameTakenError:
                await self.stop_session(launch_id, unregister=False)
                raise
            except Exception as exc:
                session.central_error = str(exc)
                log(
                    f"central unavailable for {session.identity}; "
                    f"starting local lease and retrying in background: {exc}"
                )
            session.subscription_task = asyncio.create_task(self.subscribe(session))
        except Exception:
            await self.stop_session(launch_id, unregister=False)
            raise
        log(
            f"session started identity={session.identity} launch={launch_id} "
            f"proxy={session.proxy_url}; waiting for TUI thread/start"
        )
        return {
            "launch_id": launch_id,
            "identity": session.identity,
            "thread_id": None,
            "proxy_url": session.proxy_url,
        }

    async def stop_session(self, launch_id, unregister=True):
        session = self.sessions.pop(launch_id, None)
        if session is None:
            return {"stopped": False}
        session.active = False
        if session.subscription_task is not None:
            session.subscription_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await session.subscription_task
        register_task = session.central_register_task
        if register_task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await register_task
            if session.central_register_task is register_task:
                session.central_register_task = None
        if session.proxy_server is not None:
            session.proxy_server.close()
            await session.proxy_server.wait_closed()
            session.proxy_server = None
            session.proxy_port = None
        for thread_id, owner in list(self.thread_to_launch.items()):
            if owner == launch_id:
                del self.thread_to_launch[thread_id]
        if unregister:
            try:
                await self.unregister_central(session)
            except Exception as exc:
                log(f"central unregister {session.identity} failed: {exc}")
        log(f"session stopped identity={session.identity} launch={launch_id}")
        return {"stopped": True}

    async def request_shutdown(self):
        if self.sessions:
            raise ValueError(
                f"cannot stop am-codexd while {len(self.sessions)} "
                "amcodex session(s) are active"
            )
        self.accepting_sessions = False
        self.stop_event.set()
        return {"ok": True}

    async def identity_for_thread(self, thread_id):
        launch_id = self.thread_to_launch.get(thread_id)
        session = self.sessions.get(launch_id)
        if session is None:
            return {}
        return {
            "identity": session.identity,
            "name": session.name,
            "project": session.project,
            "control_url": session.control_url,
            "launch_id": launch_id,
        }

    async def runtime_snapshot(self, session):
        if not session.thread_id:
            return {
                "runtime_state": "unknown",
                "compactions": 0,
                "context_utilization_pct": None,
            }
        try:
            result = await self.app_call(
                "thread/read",
                {"threadId": session.thread_id, "includeTurns": True},
                timeout=5,
            )
        except Exception:
            return {
                "runtime_state": "unknown",
                "compactions": 0,
                "context_utilization_pct": None,
            }
        thread = result.get("thread") or {}
        status = thread.get("status")
        if self.is_idle(status):
            runtime_state = "idle"
        else:
            status_type = status.get("type") if isinstance(status, dict) else status
            runtime_state = (
                "working"
                if status_type in {"active", "running", "working"}
                else str(status_type or "unknown")
            )
        token_usage = session.token_usage or {}
        context_window = token_usage.get("modelContextWindow")
        current_tokens = (token_usage.get("last") or {}).get("totalTokens")
        utilization = None
        if context_window and current_tokens is not None:
            utilization = round(100 * int(current_tokens) / int(context_window), 2)
        return {
            "runtime_state": runtime_state,
            "compactions": self.compaction_count(thread),
            "context_utilization_pct": utilization,
        }

    async def session_status(self, launch_id, *, include_runtime=False):
        session = self.sessions.get(launch_id)
        if session is None:
            return {}
        result = {
            "active": session.active,
            "thread_id": session.thread_id,
            "identity": session.identity,
            "proxy_url": session.proxy_url,
            "central_registered": session.central_registered,
            "central_error": session.central_error,
            "ingress_paused": session.ingress_paused,
            "pending_messages": len(session.pending),
            "active_turn_id": session.active_turn_id,
            "steers_this_turn": session.steer_count,
            "steer_limit_per_turn": STEER_MAX_PER_TURN,
        }
        if include_runtime:
            result.update(await self.runtime_snapshot(session))
        return result

    async def sessions_status(self):
        return [
            {
                "launch_id": launch_id,
                "name": session.name,
                "project": session.project,
                "identity": session.identity,
                "cwd": session.cwd,
                **(await self.session_status(launch_id, include_runtime=True)),
            }
            for launch_id, session in self.sessions.items()
        ]

    async def pause_ingress(self, launch_id):
        session = self.sessions.get(launch_id)
        if session is None:
            raise ValueError("unknown session")
        token = uuid.uuid4().hex
        session.ingress_pause_tokens.add(token)
        log(f"meeting ingress paused identity={session.identity} launch={launch_id}")
        return {"ok": True, "pause_token": token}

    async def resume_ingress(self, launch_id, pause_token):
        session = self.sessions.get(launch_id)
        if session is None:
            raise ValueError("unknown session")
        if pause_token not in session.ingress_pause_tokens:
            raise ValueError("invalid pause token")
        session.ingress_pause_tokens.remove(pause_token)
        log(f"meeting ingress resumed identity={session.identity} launch={launch_id}")
        return {"ok": True, "ingress_paused": session.ingress_paused}

    @staticmethod
    def compaction_count(thread):
        total = 0
        for turn in thread.get("turns") or []:
            for item in turn.get("items") or []:
                if item.get("type") in {"contextCompaction", "compact"}:
                    total += 1
        return total

    async def compact_session(self, launch_id, pause_token):
        session = self.sessions.get(launch_id)
        if session is None:
            raise ValueError("unknown session")
        if pause_token not in session.ingress_pause_tokens:
            raise ValueError("matching ingress pause token is required")
        if not session.thread_id:
            raise ValueError("session thread is not ready")
        before = await self.app_call(
            "thread/read",
            {"threadId": session.thread_id, "includeTurns": True},
            timeout=10,
        )
        thread = before.get("thread") or {}
        if not self.is_idle(thread.get("status")):
            raise ValueError("session is not idle")
        previous_count = self.compaction_count(thread)
        await self.app_call(
            "thread/compact/start",
            {"threadId": session.thread_id},
            timeout=10,
        )
        deadline = asyncio.get_running_loop().time() + 60
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.25)
            current = await self.app_call(
                "thread/read",
                {"threadId": session.thread_id, "includeTurns": True},
                timeout=10,
            )
            current_thread = current.get("thread") or {}
            if (
                self.compaction_count(current_thread) > previous_count
                and self.is_idle(current_thread.get("status"))
            ):
                return {
                    "ok": True,
                    "compactions": self.compaction_count(current_thread),
                }
        raise ValueError("context compaction did not complete within 60 seconds")

    async def handoff_session(self, launch_id, pause_token):
        session = self.sessions.get(launch_id)
        if session is None:
            raise ValueError("unknown session")
        if pause_token not in session.ingress_pause_tokens:
            raise ValueError("matching ingress pause token is required")
        if not session.thread_id:
            raise ValueError("session thread is not ready")
        before = await self.app_call(
            "thread/read",
            {"threadId": session.thread_id, "includeTurns": True},
            timeout=10,
        )
        thread = before.get("thread") or {}
        if not self.is_idle(thread.get("status")):
            raise ValueError("session is not idle")
        previous_turns = len(thread.get("turns") or [])
        handoff_path = Path(session.cwd) / ".codex" / "handoff-pending.md"
        previous_mtime = (
            handoff_path.stat().st_mtime_ns if handoff_path.exists() else 0
        )
        await self.app_call(
            "turn/start",
            {
                "threadId": session.thread_id,
                "input": [
                    {
                        "type": "text",
                        "text": (
                            "[am-ctld lifecycle request] Use the installed "
                            "woodor handoff skill now. Write the Codex handoff "
                            "card for the current work, then stop after reporting "
                            "that the handoff is complete."
                        ),
                    }
                ],
            },
            timeout=10,
        )
        deadline = asyncio.get_running_loop().time() + 180
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.5)
            current = await self.app_call(
                "thread/read",
                {"threadId": session.thread_id, "includeTurns": True},
                timeout=10,
            )
            current_thread = current.get("thread") or {}
            if not self.is_idle(current_thread.get("status")):
                continue
            if len(current_thread.get("turns") or []) <= previous_turns:
                continue
            if not handoff_path.exists() or handoff_path.stat().st_size == 0:
                continue
            if handoff_path.stat().st_mtime_ns <= previous_mtime:
                continue
            return {
                "ok": True,
                "handoff_path": str(handoff_path),
                "ingress_paused": True,
            }
        raise ValueError("handoff did not complete within 180 seconds")

    async def update_thread(self, session, thread_id):
        if not thread_id or session.thread_id == thread_id:
            return
        old_thread_id = session.thread_id
        session.thread_id = thread_id
        if (
            old_thread_id
            and self.thread_to_launch.get(old_thread_id) == session.launch_id
        ):
            del self.thread_to_launch[old_thread_id]
        self.thread_to_launch[thread_id] = session.launch_id
        log(f"session {session.identity} moved to thread {thread_id}")

    @staticmethod
    def runtime_instructions(session):
        return tui_websocket_proxy.runtime_instructions(session)

    @classmethod
    def scope_client_request(cls, session, message):
        """Bind thread lifecycle requests to the launcher's runtime context.

        A shared app-server keeps the cwd of the launcher that originally
        spawned its process. Remote TUIs may omit cwd from thread/start, which
        would otherwise make every later session inherit that first cwd.

        The shared app-server also cannot carry per-session environment
        variables. Pass the meeting identity and control URL as thread-scoped
        developer instructions and turn application context so the agent can
        use explicit CLI arguments even when a collaboration mode supplies its
        own developer instructions.
        """
        return tui_websocket_proxy.scope_client_request(session, message)

    async def subscribe(self, session):
        parsed = urllib.parse.urlparse(session.control_url)
        ws_url = f"ws://{parsed.hostname}:{parsed.port or 80}/subscribe"
        headers = {
            "X-Meeting-Name": session.name,
            "X-Meeting-Project": session.project,
            "X-Meeting-Proto": "1",
            "X-Meeting-Mode": "notify",
            "X-Meeting-Instance": session.launch_id,
        }
        token = auth_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        backoff = 1.0
        while session.active:
            try:
                await self.register_central(session)
                async with websockets.connect(
                    ws_url,
                    **central_websocket_options(headers),
                ) as websocket:
                    backoff = 1.0
                    await self.fetch_inbox(session)
                    async for raw in websocket:
                        message = json.loads(raw)
                        if message.get("type") == "notify":
                            await self.fetch_inbox(session)
            except asyncio.CancelledError:
                return
            except NameTakenError as exc:
                session.central_error = str(exc)
                session.active = False
                if session.proxy_server is not None:
                    session.proxy_server.close()
                    await session.proxy_server.wait_closed()
                for client in list(session.proxy_clients):
                    await client.close(
                        code=1011,
                        reason="meeting identity is already registered",
                    )
                log(
                    f"central registration {session.identity} rejected: {exc}"
                )
                return
            except Exception as exc:
                session.central_error = str(exc)
                log(f"central subscription {session.identity} failed: {exc}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    @staticmethod
    def is_idle(status):
        return status.get("type") == "idle" if isinstance(status, dict) else status == "idle"

    @staticmethod
    def message_sender(message):
        return hub_inbox_delivery.message_sender(message)

    def build_injection(self, session):
        return hub_inbox_delivery.build_injection(
            session,
            control_stale_seconds=CONTROL_STALE_S,
            max_messages=IDLE_MAX_MESSAGES,
        )

    @staticmethod
    def build_steer_injection(session):
        return hub_inbox_delivery.build_steer_injection(
            session,
            max_messages=STEER_MAX_MESSAGES,
        )

    @staticmethod
    def active_turn_from_thread(thread):
        for turn in reversed(thread.get("turns") or []):
            if turn.get("status") == "inProgress" and turn.get("id"):
                return str(turn["id"])
        return None

    @staticmethod
    def reset_steer_state(session, active_turn_id=None):
        session.active_turn_id = active_turn_id
        session.steer_turn_id = active_turn_id
        session.steer_count = 0
        session.last_steer_at = 0.0
        session.steer_pending_since = None

    async def finish_ack(self, session):
        selected = session.awaiting_ack
        if not selected:
            return True
        try:
            await self.acknowledge(session, selected)
        except Exception as exc:
            log(f"ack {session.identity} failed: {exc}")
            return False
        for message_id in selected:
            session.pending.pop(message_id, None)
        session.awaiting_ack = None
        try:
            await self.fetch_inbox(session)
        except Exception as exc:
            log(f"post-ack fetch {session.identity} failed: {exc}")
        return True

    async def try_inject(self, session):
        if session.ingress_paused:
            return
        if session.awaiting_ack:
            await self.finish_ack(session)
            return
        if not session.pending or not session.thread_id:
            return
        selected, text = self.build_injection(session)
        if not selected:
            return
        if not text:
            session.awaiting_ack = selected
            await self.finish_ack(session)
            return
        steer_attempted = False
        try:
            read_result = await self.app_call(
                "thread/read",
                {"threadId": session.thread_id, "includeTurns": False},
                timeout=10,
            )
            thread = read_result.get("thread") or {}
            status = thread.get("status")
            if session.ingress_paused:
                return
            if self.is_idle(status):
                self.reset_steer_state(session)
                if len(selected) > IDLE_DIGEST_THRESHOLD:
                    selected, text = self.build_steer_injection(session)
                await self.app_call(
                    "turn/start",
                    {
                        "threadId": session.thread_id,
                        "input": [{"type": "text", "text": text}],
                        "additionalContext": {
                            "agent-meeting-runtime": {
                                "kind": "application",
                                "value": self.runtime_instructions(session),
                            }
                        },
                    },
                    timeout=30,
                )
                injection_kind = "turn/start"
            else:
                active_turn_id = session.active_turn_id
                if not active_turn_id:
                    active_result = await self.app_call(
                        "thread/read",
                        {
                            "threadId": session.thread_id,
                            "includeTurns": True,
                        },
                        timeout=10,
                    )
                    active_thread = active_result.get("thread") or {}
                    active_turn_id = self.active_turn_from_thread(active_thread)
                    if active_turn_id:
                        session.active_turn_id = active_turn_id
                if not active_turn_id:
                    return
                if session.steer_turn_id != active_turn_id:
                    self.reset_steer_state(session, active_turn_id)
                now = time.monotonic()
                if session.steer_pending_since is None:
                    session.steer_pending_since = now
                    return
                if now - session.steer_pending_since < STEER_DEBOUNCE_S:
                    return
                if session.steer_count >= STEER_MAX_PER_TURN:
                    return
                if now - session.last_steer_at < STEER_COOLDOWN_S:
                    return
                steer_selected, steer_text = self.build_steer_injection(session)
                if not steer_selected:
                    return
                if not steer_text:
                    session.awaiting_ack = steer_selected
                    await self.finish_ack(session)
                    return
                steer_attempted = True
                await self.app_call(
                    "turn/steer",
                    {
                        "threadId": session.thread_id,
                        "expectedTurnId": active_turn_id,
                        "input": [{"type": "text", "text": steer_text}],
                        "additionalContext": {
                            "agent-meeting-runtime": {
                                "kind": "application",
                                "value": self.runtime_instructions(session),
                            }
                        },
                    },
                    timeout=30,
                )
                selected = steer_selected
                session.steer_count += 1
                session.last_steer_at = now
                session.steer_pending_since = None
                injection_kind = "turn/steer"
        except Exception as exc:
            if steer_attempted:
                session.active_turn_id = None
                session.last_steer_at = time.monotonic()
                session.steer_pending_since = session.last_steer_at
                session.steer_count = STEER_MAX_PER_TURN
            log(f"inject {session.identity} failed: {exc}")
            return
        session.awaiting_ack = selected
        await self.finish_ack(session)
        log(
            f"injected {len(selected)} message(s) via {injection_kind} into "
            f"{session.identity} thread={session.thread_id}"
        )

    async def scheduler(self):
        while not self.stop_event.is_set():
            for session in list(self.sessions.values()):
                if session.active:
                    await self.try_inject(session)
            await asyncio.sleep(INJECT_POLL_S)

    async def supervise_appserver(self):
        while not self.stop_event.is_set():
            await asyncio.sleep(2)
            process = self.appserver.process
            if process is not None and process.poll() is None:
                continue
            log("shared Codex app-server exited; restarting")
            await asyncio.to_thread(self.appserver.start)

    async def start_session_proxy(self, session):
        async def handler(client):
            await self.proxy(session, client)

        server = await websockets.serve(
            handler,
            PROXY_HOST,
            0,
            max_size=None,
            compression=None,
        )
        try:
            port = server.sockets[0].getsockname()[1]
        except Exception:
            server.close()
            await server.wait_closed()
            raise
        session.proxy_server = server
        session.proxy_port = port

    async def proxy(self, session, client):
        if not session.active:
            await client.close(code=1008, reason="inactive daemon session")
            return

        session.proxy_clients.add(client)
        pending_thread_requests = {}
        pending_turn_requests = set()
        app_url = self.appserver.ws_url
        try:
            async with websockets.connect(app_url, max_size=None) as upstream:
                async def client_to_server():
                    async for raw in client:
                        try:
                            message = json.loads(raw)
                            message = self.scope_client_request(session, message)
                            method = message.get("method")
                            if method in ("thread/start", "thread/fork", "thread/resume"):
                                pending_thread_requests[message.get("id")] = method
                            if method == "turn/start" and message.get("id") is not None:
                                pending_turn_requests.add(message["id"])
                            raw = json.dumps(message)
                        except Exception:
                            pass
                        await upstream.send(raw)

                async def server_to_client():
                    async for raw in upstream:
                        try:
                            message = json.loads(raw)
                            if (
                                message.get("method")
                                == "thread/tokenUsage/updated"
                            ):
                                params = message.get("params") or {}
                                if params.get("threadId") == session.thread_id:
                                    session.token_usage = params.get("tokenUsage")
                            if message.get("method") == "turn/started":
                                params = message.get("params") or {}
                                if params.get("threadId") == session.thread_id:
                                    turn_id = (params.get("turn") or {}).get("id")
                                    if turn_id:
                                        self.reset_steer_state(session, str(turn_id))
                            if message.get("method") == "turn/completed":
                                params = message.get("params") or {}
                                if params.get("threadId") == session.thread_id:
                                    turn_id = (params.get("turn") or {}).get("id")
                                    if (
                                        turn_id
                                        and str(turn_id) == session.active_turn_id
                                    ):
                                        self.reset_steer_state(session)
                            if message.get("id") in pending_turn_requests:
                                pending_turn_requests.discard(message.get("id"))
                                if "error" not in message:
                                    result = message.get("result") or {}
                                    turn_id = (
                                        (result.get("turn") or {}).get("id")
                                        or result.get("turnId")
                                    )
                                    if turn_id:
                                        self.reset_steer_state(
                                            session,
                                            str(turn_id),
                                        )
                            if message.get("id") in pending_thread_requests:
                                pending_thread_requests.pop(message.get("id"), None)
                                if "error" not in message:
                                    thread_id = (
                                        ((message.get("result") or {}).get("thread") or {}).get("id")
                                    )
                                    await self.update_thread(session, thread_id)
                        except Exception:
                            pass
                        await client.send(raw)

                tasks = [
                    asyncio.create_task(client_to_server()),
                    asyncio.create_task(server_to_client()),
                ]
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                for task in done:
                    task.result()
        except Exception as exc:
            log(f"proxy {session.identity} disconnected: {exc}")
        finally:
            session.proxy_clients.discard(client)

    def start_http(self):
        broker = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def read_body(self):
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                return json.loads(raw.decode("utf-8"))

            def send_json(self, status, payload):
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def invoke(self, coroutine):
                future = asyncio.run_coroutine_threadsafe(coroutine, broker.loop)
                return future.result(timeout=40)

            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path == "/health":
                    appserver_ready = (
                        broker.appserver.ready
                        and
                        broker.appserver.process is not None
                        and broker.appserver.process.poll() is None
                        and broker.appserver.port is not None
                    )
                    return self.send_json(
                        200 if appserver_ready else 503,
                        {
                            "ok": appserver_ready,
                            "version": DAEMON_VERSION,
                            "pid": os.getpid(),
                            "sessions": len(broker.sessions),
                            "appserver_url": (
                                broker.appserver.ws_url if appserver_ready else None
                            ),
                            "proxy_mode": "per-session-loopback-port",
                        },
                    )
                if parsed.path == "/identity":
                    query = urllib.parse.parse_qs(parsed.query)
                    thread_id = query.get("thread_id", [""])[0]
                    result = self.invoke(broker.identity_for_thread(thread_id))
                    return self.send_json(200 if result else 404, result or {"error": "unknown thread"})
                if parsed.path == "/session":
                    query = urllib.parse.parse_qs(parsed.query)
                    launch_id = query.get("launch_id", [""])[0]
                    result = self.invoke(broker.session_status(launch_id))
                    return self.send_json(200 if result else 404, result or {"error": "unknown session"})
                if parsed.path == "/sessions":
                    return self.send_json(
                        200, {"sessions": self.invoke(broker.sessions_status())}
                    )
                return self.send_json(404, {"error": "not found"})

            def do_POST(self):
                try:
                    if self.path == "/session/start":
                        result = self.invoke(broker.start_session(self.read_body()))
                        return self.send_json(200, result)
                    if self.path == "/session/stop":
                        body = self.read_body()
                        result = self.invoke(broker.stop_session(str(body["launch_id"])))
                        return self.send_json(200, result)
                    if self.path == "/session/ingress/pause":
                        body = self.read_body()
                        result = self.invoke(
                            broker.pause_ingress(str(body["launch_id"]))
                        )
                        return self.send_json(200, result)
                    if self.path == "/session/ingress/resume":
                        body = self.read_body()
                        result = self.invoke(
                            broker.resume_ingress(
                                str(body["launch_id"]),
                                str(body["pause_token"]),
                            )
                        )
                        return self.send_json(200, result)
                    if self.path == "/session/compact":
                        body = self.read_body()
                        result = self.invoke(
                            broker.compact_session(
                                str(body["launch_id"]),
                                str(body["pause_token"]),
                            )
                        )
                        return self.send_json(200, result)
                    if self.path == "/session/handoff":
                        body = self.read_body()
                        result = self.invoke(
                            broker.handoff_session(
                                str(body["launch_id"]),
                                str(body["pause_token"]),
                            )
                        )
                        return self.send_json(200, result)
                    if self.path == "/shutdown":
                        result = self.invoke(broker.request_shutdown())
                        return self.send_json(200, result)
                    self.send_json(404, {"error": "not found"})
                except (ValueError, KeyError) as exc:
                    self.send_json(409, {"error": str(exc)})
                except Exception as exc:
                    self.send_json(500, {"error": str(exc)})

        ThreadingHTTPServer.allow_reuse_address = True
        self.http_server = ThreadingHTTPServer((API_HOST, API_PORT), Handler)
        thread = threading.Thread(
            target=self.http_server.serve_forever,
            name="am-codexd-http",
            daemon=True,
        )
        thread.start()

    async def run(self):
        if websockets is None:
            raise RuntimeError(
                "the agent-meeting runtime is missing the websockets package; reinstall it"
            )
        self.loop = asyncio.get_running_loop()
        # Claim the broker API port before starting the expensive app-server
        # child. Concurrent mycodex launchers can race to spawn the broker; the
        # loser fails here without briefly creating a second app-server process.
        self.start_http()
        try:
            await asyncio.to_thread(self.appserver.start)
            log(
                f"daemon ready api=http://{API_HOST}:{API_PORT} "
                "proxy=per-session-loopback-port"
            )
            self.scheduler_task = asyncio.create_task(self.scheduler())
            self.supervisor_task = asyncio.create_task(self.supervise_appserver())
            await self.stop_event.wait()
        finally:
            for task in (self.scheduler_task, self.supervisor_task):
                if task is not None:
                    task.cancel()
            for launch_id in list(self.sessions):
                await self.stop_session(launch_id)
            if self.http_server is not None:
                self.http_server.shutdown()
                self.http_server.server_close()
            await asyncio.to_thread(self.appserver.stop)
            log("daemon stopped")


def serve():
    broker = Broker()

    async def run():
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, broker.stop_event.set)
            except (NotImplementedError, RuntimeError):
                pass
        await broker.run()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    serve()
