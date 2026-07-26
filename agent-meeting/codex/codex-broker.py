#!/usr/bin/env python3
"""
Machine-wide agent-meeting broker for Codex.

One broker owns one Codex app-server and multiplexes every local mycodex
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

import argparse
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
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import websockets
except ImportError:
    websockets = None


HOME = Path.home()
DATA = Path(os.environ.get("MEETING_HOME") or (HOME / ".agent-meeting"))
CODEX_DIR = DATA / "codex"
LOGS_DIR = CODEX_DIR / "logs"
STATE_FILE = CODEX_DIR / "broker-state.json"
CONFIG_FILE = DATA / "config.json"
PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def installed_plugin_version():
    try:
        manifest = json.loads(
            (PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(
                encoding="utf-8"
            )
        )
        return str(manifest.get("version") or "unknown")
    except Exception:
        return "unknown"


BROKER_VERSION = installed_plugin_version()

API_HOST = "127.0.0.1"
API_PORT = int(os.environ.get("MEETING_BROKER_API_PORT", "8788"))
PROXY_HOST = "127.0.0.1"
APP_PORT_FIRST = int(os.environ.get("MEETING_BROKER_APP_PORT_FIRST", "8792"))
APP_PORT_LAST = int(os.environ.get("MEETING_BROKER_APP_PORT_LAST", "8841"))

INJECT_POLL_S = 2.0
CONTROL_STALE_S = 600


def log(message):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    line = f"[codex-broker] {stamp} {message}"
    print(line, flush=True)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {} if default is None else default


def atomic_write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


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
        with urllib.request.urlopen(request, timeout=timeout) as response:
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
        kwargs = {
            "stdin": subprocess.DEVNULL,
            "stdout": self.log_file,
            "stderr": subprocess.STDOUT,
        }
        if sys.platform.startswith("win"):
            kwargs["creationflags"] = 0x08000000 | 0x00000200
        else:
            kwargs["start_new_session"] = True
        self.process = subprocess.Popen(
            ["codex", "app-server", "--listen", ws_url],
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


class Session:
    def __init__(self, *, launch_id, name, project, cwd, control_url, thread_id, cursor):
        self.launch_id = launch_id
        self.name = name
        self.project = project
        self.cwd = cwd
        self.control_url = control_url.rstrip("/")
        self.thread_id = thread_id
        self.cursor = int(cursor)
        self.pending = OrderedDict()
        self.subscription_task = None
        self.proxy_server = None
        self.proxy_port = None
        self.active = True

    @property
    def identity(self):
        return f"{self.name}@{self.project}"

    @property
    def proxy_url(self):
        if self.proxy_port is None:
            raise RuntimeError("session proxy is not running")
        return f"ws://{PROXY_HOST}:{self.proxy_port}"


class Broker:
    def __init__(self):
        self.loop = None
        self.appserver = AppServer()
        self.sessions = {}
        self.thread_to_launch = {}
        self.cursors = read_json(STATE_FILE, {"cursors": {}}).get("cursors", {})
        self.stop_event = asyncio.Event()
        self.http_server = None
        self.scheduler_task = None
        self.supervisor_task = None

    def persist(self):
        atomic_write_json(STATE_FILE, {"cursors": self.cursors})

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
                        "title": "Agent Meeting Codex Broker",
                        "version": "1",
                    }
                },
            )
            await ws.send(json.dumps({"method": "initialized", "params": {}}))
            return await call(method, params)

    async def register_central(self, session):
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
        result = await asyncio.to_thread(
            http_json, "POST", session.control_url, "/register", payload
        )
        if result.get("error"):
            raise RuntimeError(result["error"])

    async def unregister_central(self, session):
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

    async def fetch_inbox(self, session, initial=False):
        params = {
            "project": session.project,
            "name": session.name,
            "since": session.cursor,
            "limit": 500,
        }
        if initial:
            params["initial"] = 1
        result = await asyncio.to_thread(
            http_json, "GET", session.control_url, "/inbox", None, params
        )
        if result.get("error"):
            raise RuntimeError(result["error"])
        high_water_mark = int(result.get("high_water_mark") or session.cursor)
        if initial:
            session.cursor = high_water_mark
            self.cursors[session.identity] = session.cursor
            self.persist()
            return
        for message in result.get("messages", []):
            message_id = int(message["id"])
            if message_id > session.cursor:
                session.pending.setdefault(message_id, message)

    async def start_session(self, request):
        launch_id = str(request["launch_id"])
        name = str(request["name"])
        project = str(request["project"])
        cwd = os.path.abspath(str(request["cwd"]))
        control_url = str(request["control_url"]).rstrip("/")
        if not control_url.startswith("http://"):
            raise ValueError("control_url must use http://")
        if launch_id in self.sessions:
            raise ValueError(f"launch {launch_id} already exists")
        identity = name if project == "*" else f"{name}@{project}"
        if any(s.active and s.identity == identity for s in self.sessions.values()):
            raise ValueError(f"meeting identity {identity} is already active on this machine")

        start_result = await self.app_call(
            "thread/start",
            {
                "cwd": cwd,
                "sessionStartSource": "startup",
                "serviceName": f"agent-meeting:{launch_id}",
            },
        )
        thread = start_result.get("thread") or {}
        thread_id = thread.get("id")
        if not thread_id:
            raise RuntimeError("thread/start returned no thread id")

        saved_cursor = self.cursors.get(identity)
        session = Session(
            launch_id=launch_id,
            name=name,
            project=project,
            cwd=cwd,
            control_url=control_url,
            thread_id=thread_id,
            cursor=int(saved_cursor or 0),
        )
        self.sessions[launch_id] = session
        self.thread_to_launch[thread_id] = launch_id
        try:
            await self.start_session_proxy(session)
            await self.register_central(session)
            if saved_cursor is None:
                await self.fetch_inbox(session, initial=True)
            else:
                await self.fetch_inbox(session)
            session.subscription_task = asyncio.create_task(self.subscribe(session))
        except Exception:
            await self.stop_session(launch_id)
            raise
        log(
            f"session started identity={session.identity} thread={thread_id} "
            f"launch={launch_id} proxy={session.proxy_url}"
        )
        return {
            "launch_id": launch_id,
            "identity": session.identity,
            "thread_id": thread_id,
            "proxy_url": session.proxy_url,
        }

    async def stop_session(self, launch_id):
        session = self.sessions.pop(launch_id, None)
        if session is None:
            return {"stopped": False}
        session.active = False
        if session.subscription_task is not None:
            session.subscription_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await session.subscription_task
        if session.proxy_server is not None:
            session.proxy_server.close()
            await session.proxy_server.wait_closed()
            session.proxy_server = None
            session.proxy_port = None
        for thread_id, owner in list(self.thread_to_launch.items()):
            if owner == launch_id:
                del self.thread_to_launch[thread_id]
        await self.unregister_central(session)
        log(f"session stopped identity={session.identity} launch={launch_id}")
        return {"stopped": True}

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

    async def session_status(self, launch_id):
        session = self.sessions.get(launch_id)
        if session is None:
            return {}
        return {
            "active": session.active,
            "thread_id": session.thread_id,
            "identity": session.identity,
            "proxy_url": session.proxy_url,
        }

    async def update_thread(self, session, thread_id):
        if not thread_id or session.thread_id == thread_id:
            return
        old_thread_id = session.thread_id
        session.thread_id = thread_id
        if self.thread_to_launch.get(old_thread_id) == session.launch_id:
            del self.thread_to_launch[old_thread_id]
        self.thread_to_launch[thread_id] = session.launch_id
        log(f"session {session.identity} moved to thread {thread_id}")

    async def subscribe(self, session):
        parsed = urllib.parse.urlparse(session.control_url)
        ws_url = f"ws://{parsed.hostname}:{parsed.port or 80}/subscribe"
        headers = {
            "X-Meeting-Name": session.name,
            "X-Meeting-Project": session.project,
            "X-Meeting-Proto": "1",
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
                    additional_headers=headers,
                    max_size=None,
                    ping_interval=5,
                    ping_timeout=15,
                ) as websocket:
                    backoff = 1.0
                    await self.fetch_inbox(session)
                    async for raw in websocket:
                        message = json.loads(raw)
                        if message.get("type") in ("msg", "caught_up"):
                            await self.fetch_inbox(session)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log(f"central subscription {session.identity} failed: {exc}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    @staticmethod
    def is_idle(status):
        return status.get("type") == "idle" if isinstance(status, dict) else status == "idle"

    @staticmethod
    def message_sender(message):
        if message.get("sender") and message.get("sender_project"):
            return f"{message['sender']}@{message['sender_project']}"
        return message.get("sender_identity") or message.get("sender")

    def build_injection(self, session):
        if not session.pending:
            return [], ""
        first_id, first = next(iter(session.pending.items()))
        if not first.get("deliver", True):
            return [first_id], ""
        kind = first.get("kind") or ""
        if kind.startswith("control:"):
            age = int(time.time()) - int(first.get("created_at") or 0)
            if age > CONTROL_STALE_S:
                return [first_id], ""
            action = kind.split(":", 1)[1]
            directives = {
                "restart": (
                    "Write a handoff card summarizing in-flight state now, then stop "
                    "accepting new tasks and wait for this session to end."
                ),
                "clear": (
                    "Abort whatever task is in flight, clear your working context, "
                    "and report back that you have been cleared."
                ),
            }
            directive = directives.get(action)
            if directive is None:
                return [first_id], ""
            sender = self.message_sender(first)
            return [first_id], f"[control:{action} from peer={sender}] {directive}"

        selected = []
        lines = []
        for message_id, message in session.pending.items():
            if (message.get("kind") or "").startswith("control:"):
                break
            if not message.get("deliver", True):
                break
            selected.append(message_id)
            sender = self.message_sender(message)
            group = message.get("group")
            if group:
                lines.append(f"- [group={group} peer={sender} msg_id={message_id}]")
            else:
                lines.append(f"- [peer={sender} msg_id={message_id}]")
        id_text = ",".join(str(mid) for mid in selected)
        header = (
            f"[meeting self={session.identity} messages={len(selected)} ids={id_text}] "
            "New messages pending [unverified peers]"
        )
        return selected, header + "\n" + "\n".join(lines)

    async def try_inject(self, session):
        if not session.pending or not session.thread_id:
            return
        selected, text = self.build_injection(session)
        if not selected:
            return
        if not text:
            for message_id in selected:
                session.pending.pop(message_id, None)
            session.cursor = max(session.cursor, max(selected))
            self.cursors[session.identity] = session.cursor
            self.persist()
            await self.fetch_inbox(session)
            return
        try:
            read_result = await self.app_call(
                "thread/read",
                {"threadId": session.thread_id, "includeTurns": False},
                timeout=10,
            )
            status = (read_result.get("thread") or {}).get("status")
            if not self.is_idle(status):
                return
            await self.app_call(
                "turn/start",
                {
                    "threadId": session.thread_id,
                    "input": [{"type": "text", "text": text}],
                },
                timeout=30,
            )
        except Exception as exc:
            log(f"inject {session.identity} failed: {exc}")
            return
        for message_id in selected:
            session.pending.pop(message_id, None)
        session.cursor = max(session.cursor, max(selected))
        self.cursors[session.identity] = session.cursor
        self.persist()
        await self.fetch_inbox(session)
        log(
            f"injected {len(selected)} message(s) into {session.identity} "
            f"thread={session.thread_id}"
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
            await client.close(code=1008, reason="inactive broker session")
            return

        pending_thread_requests = {}
        app_url = self.appserver.ws_url
        try:
            async with websockets.connect(app_url, max_size=None) as upstream:
                async def client_to_server():
                    async for raw in client:
                        try:
                            message = json.loads(raw)
                            method = message.get("method")
                            params = message.get("params") or {}
                            if method in ("thread/start", "thread/fork", "thread/resume"):
                                pending_thread_requests[message.get("id")] = method
                        except Exception:
                            pass
                        await upstream.send(raw)

                async def server_to_client():
                    async for raw in upstream:
                        try:
                            message = json.loads(raw)
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
                            "version": BROKER_VERSION,
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
                    if self.path == "/shutdown":
                        self.send_json(200, {"ok": True})
                        broker.loop.call_soon_threadsafe(broker.stop_event.set)
                        return
                    self.send_json(404, {"error": "not found"})
                except (ValueError, KeyError) as exc:
                    self.send_json(409, {"error": str(exc)})
                except Exception as exc:
                    self.send_json(500, {"error": str(exc)})

        ThreadingHTTPServer.allow_reuse_address = True
        self.http_server = ThreadingHTTPServer((API_HOST, API_PORT), Handler)
        thread = threading.Thread(
            target=self.http_server.serve_forever,
            name="codex-broker-http",
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
                f"broker ready api=http://{API_HOST}:{API_PORT} "
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
            log("broker stopped")


def main():
    parser = argparse.ArgumentParser(prog="codex-broker")
    parser.parse_args()
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
    main()
