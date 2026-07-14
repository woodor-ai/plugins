#!/usr/bin/env python3
"""
Shared kernel for agent-meeting's Python-side runtime scripts.

Covers the pieces that were independently forked across bin/meeting,
bin/monitor.py, codex/codex-bridge.py and codex/codex-meeting.py:
  - git-based project name derivation
  - a `meeting` CLI subprocess wrapper
  - control-endpoint discovery via `meeting controls --json`
  - the WS /subscribe client kernel (handshake, ping/pong, backoff reconnect)

Message/frame *semantics* (DM vs group, cursors, control:* instructions, UI
notification formatting) are intentionally NOT here -- those differ between
monitor.py and codex-bridge.py and stay as caller-supplied callbacks.

Import layout: this file must be importable from both runtime layouts:
  - bin/meeting, bin/monitor.py run from <plugin>/bin (source) or
    ~/.agent-meeting/bin (copied runtime) -- this file sits alongside them in
    both places (session-bootstrap.py's ensure_bin_wrappers copies every .py
    file in <plugin>/bin/ into the runtime dir, this one included), so a
    plain `import meeting_common` resolves via Python's own script-directory
    sys.path[0] with no extra wiring.
  - codex/codex-bridge.py and codex/codex-meeting.py run directly from
    <plugin>/codex/ (never copied) and add ~/.agent-meeting/bin to sys.path
    before importing this module.
"""

import base64
import hashlib
import json
import os
import random
import select
import socket
import struct
import subprocess
import sys
import time


MEETING_HOME = os.environ.get("MEETING_HOME") or os.path.expanduser("~/.agent-meeting")


# ---------------------------------------------------------------------------
# Project name derivation
# ---------------------------------------------------------------------------

def _project_root(cwd: str) -> str:
    """Resolve the stable identity root for cwd: the main git repo's root dir
    (parent of ``--git-common-dir``, so a worktree converges on its main repo
    instead of the worktree directory), or the normalized cwd for non-git dirs.

    Shared by derive_project() and `meeting online`'s explicit --proj cache
    write, so the two never compute "the root" two different ways.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            common_dir = result.stdout.strip()
            if common_dir:
                return os.path.dirname(os.path.normpath(common_dir))
    except Exception:
        pass
    return os.path.normpath(cwd)


def proj_cache_path(root: str) -> str:
    """Path to the cached explicit --proj declaration for a given repo root."""
    key = hashlib.sha1(os.path.normpath(root).encode("utf-8", "replace")).hexdigest()[:16]
    return os.path.join(MEETING_HOME, "projcache", key)


def proj_cache_get(root: str):
    """Return the cached proj for root, or None if never declared."""
    try:
        with open(proj_cache_path(root)) as f:
            val = f.read().strip()
            return val or None
    except Exception:
        return None


def proj_cache_set(root: str, proj: str) -> None:
    """Cache an explicit --proj declaration for root. Best-effort."""
    try:
        path = proj_cache_path(root)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(proj)
    except Exception:
        pass


def derive_project(cwd: str) -> str:
    """Derive this session's project identity for cwd.

    1. Resolve the repo root (see _project_root).
    2. If an explicit --proj was ever declared for that root (`meeting online
       --proj ...`), return the cached value -- explicit declaration always
       wins over folder-based guessing.
    3. Otherwise fall back to the root's home-relative path (e.g.
       /Users/tommyclaw/AIAgent/wda-v3 -> ~/AIAgent/wda-v3 on macOS/Linux; the
       full path as-is on Windows). This deliberately erases the username so
       the same relative repo layout matches across machines, unlike the old
       basename-only derivation which split identity when the same repo was
       cloned into differently-named folders.

    Never returns "*" (reserved for --global); maps to "_" instead.
    """
    root = _project_root(cwd)

    cached = proj_cache_get(root)
    if cached:
        return cached

    if sys.platform.startswith("win"):
        name = root
    else:
        home = os.path.expanduser("~")
        if root == home:
            name = "~"
        elif root.startswith(home + os.sep):
            name = "~" + root[len(home):]
        else:
            name = root

    return "_" if name == "*" else name


# ---------------------------------------------------------------------------
# `meeting` CLI subprocess wrapper
# ---------------------------------------------------------------------------

def run_meeting_cli(cli_path, *args, python=None, host=None, cwd=None, timeout=15, env=None):
    """Invoke the `meeting` CLI as a subprocess. Returns a subprocess.CompletedProcess.

    python: interpreter to exec cli_path with (venv python / sys.executable), or
        None to invoke cli_path directly -- its own shebang (POSIX) or .cmd
        wrapper (Windows) picks the interpreter. monitor.py uses None because
        cli_path there is already such a wrapper; codex-bridge.py and
        codex-meeting.py pass an explicit interpreter because they invoke the
        extensionless script directly (bypassing cmd.exe's `<`/`>` mangling).
    host: optional control base URL, appended as `--host <host>`.
    Raises whatever subprocess.run raises (e.g. TimeoutExpired) -- callers that
    want a swallow-errors mode catch it themselves.
    """
    cmd = ([str(python)] if python else []) + [str(cli_path)] + list(args)
    if host:
        cmd += ["--host", host]
    kw = {"creationflags": 0x08000000} if sys.platform.startswith("win") else {}  # CREATE_NO_WINDOW
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
        cwd=cwd, env=(env if env is not None else os.environ.copy()), **kw,
    )


# ---------------------------------------------------------------------------
# Control endpoint discovery (via `meeting controls --json`)
# ---------------------------------------------------------------------------

def discover_control(run_meeting) -> dict:
    """Query `meeting controls --json` and return the current (or first)
    control's connection info, or {} if none is found / the call fails.

    run_meeting: callable(*args) -> subprocess.CompletedProcess | None, already
        bound to the caller's specific way of invoking the CLI (venv python,
        --host, cwd, ...) -- see run_meeting_cli.

    Returns {"ip", "port", "host", "ip_port", "base_url"} on success.
    """
    try:
        r = run_meeting("controls", "--json")
        if r is None or r.returncode != 0 or not r.stdout.strip():
            return {}
        controls = json.loads(r.stdout)
        if not controls:
            return {}
        c = next((x for x in controls if x.get("is_current")), controls[0])
        ip = c.get("ip") or ""
        port = c.get("port") or ""
        host = c.get("host") or ip
        return {
            "ip": ip,
            "port": port,
            "host": host,
            "ip_port": f"{ip}:{port}",
            "base_url": f"http://{ip}:{port}" if ip and port else "",
        }
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Auth token
# ---------------------------------------------------------------------------

def read_auth_token(data_dir):
    """Read `auth_token` from <data_dir>/config.json, or None."""
    try:
        with open(os.path.join(str(data_dir), "config.json")) as f:
            return json.load(f).get("auth_token") or None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# WS frame primitives (RFC 6455 client-side framing; text/ping/pong/close
# only -- fragmented frames are not supported, matching the daemon's framing)
# ---------------------------------------------------------------------------

def ws_make_key() -> "tuple[str, str]":
    raw_key = base64.b64encode(os.urandom(16)).decode()
    accept = base64.b64encode(
        hashlib.sha1((raw_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
    ).decode()
    return raw_key, accept


def ws_send_masked(sock: socket.socket, opcode: int, payload: bytes) -> None:
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    length = len(payload)
    if length < 126:
        header = struct.pack("!BB", 0x80 | opcode, 0x80 | length)
    elif length < 65536:
        header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, length)
    else:
        header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, length)
    sock.sendall(header + mask + masked)


def ws_recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise IOError("EOF")
        buf += chunk
    return buf


def ws_read_frame(sock: socket.socket) -> "tuple[int, bytes]":
    header = ws_recv_exact(sock, 2)
    b0, b1 = header[0], header[1]
    fin = (b0 & 0x80) != 0
    opcode = b0 & 0x0F
    masked = (b1 & 0x80) != 0
    length = b1 & 0x7F

    if not fin:
        raise IOError("fragmented frame not supported")

    if length == 126:
        length = struct.unpack("!H", ws_recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", ws_recv_exact(sock, 8))[0]

    mask_key = ws_recv_exact(sock, 4) if masked else b""
    payload = ws_recv_exact(sock, length) if length else b""
    if masked:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return opcode, payload


# ---------------------------------------------------------------------------
# WS /subscribe client kernel
# ---------------------------------------------------------------------------

class WSSubscribeClient:
    """Reusable WS /subscribe client: handshake, ping/pong keepalive,
    dead-connection detection, and backoff reconnect. Frame *handling* is left
    to the caller via on_text/on_connect callbacks -- this class knows nothing
    about message semantics (DM vs group, cursors, control instructions, ...).
    """

    PING_INTERVAL = 5
    DEAD_TIMEOUT = 15
    BACKOFF_BASE = 1.0
    BACKOFF_MAX = 30.0
    BACKOFF_JITTER = 0.20

    def __init__(self, *, self_name, project, resolve_addr, read_token, on_text,
                 on_connect=None, log=None):
        """
        resolve_addr: callable() -> (ip, port) | None. Called on every connect
            attempt -- pass a fixed-tuple closure for a once-resolved endpoint
            (codex-bridge.py, which resolves the control ONCE at startup) or a
            fresh-discovery closure to re-resolve on every reconnect
            (monitor.py).
        project: callable() -> str, the X-Meeting-Project handshake header.
            Called on every connect attempt -- pass a fixed-value closure for a
            static project (monitor.py, whose own cwd never changes) or a
            fresh-derivation closure (codex-bridge.py, whose session mapping
            file's cwd can be updated underneath a long-lived process).
        read_token: callable() -> str | None, the bearer token for Authorization.
        on_text: callable(msg: dict) -- called for every decoded JSON text frame.
        on_connect: callable() | None -- called once per successful handshake,
            before entering the read loop (e.g. re-register, catch up cursors).
        log: callable(str) | None -- receives one-line diagnostic messages
            (no prefix/timestamp -- the caller formats those, matching each
            script's existing log style).
        """
        self.self_name = self_name
        self.project = project
        self.resolve_addr = resolve_addr
        self.read_token = read_token
        self.on_text = on_text
        self.on_connect = on_connect
        self.log = log or (lambda msg: None)

    def _connect(self):
        addr = self.resolve_addr()
        if not addr:
            return None
        ip, port = addr

        try:
            sock = socket.create_connection((ip, port), timeout=10)
        except Exception as e:
            self.log(f"ws connect failed ({ip}:{port}): {e}")
            return None

        ws_key, expected_accept = ws_make_key()
        token = self.read_token()

        headers = [
            "GET /subscribe HTTP/1.1",
            f"Host: {ip}:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {ws_key}",
            "Sec-WebSocket-Version: 13",
            f"X-Meeting-Name: {self.self_name}",
            f"X-Meeting-Project: {self.project()}",
            "X-Meeting-Proto: 1",
        ]
        if token:
            headers.append(f"Authorization: Bearer {token}")

        try:
            sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode())

            line = b""
            while not line.endswith(b"\r\n"):
                ch = sock.recv(1)
                if not ch:
                    raise IOError("connection closed during handshake")
                line += ch
            status_line = line.decode().strip()

            resp_headers = {}
            while True:
                hline = b""
                while not hline.endswith(b"\r\n"):
                    ch = sock.recv(1)
                    if not ch:
                        raise IOError("connection closed reading headers")
                    hline += ch
                hline = hline.decode().strip()
                if not hline:
                    break
                if ":" in hline:
                    k, _, v = hline.partition(":")
                    resp_headers[k.strip().lower()] = v.strip()

            if "101" not in status_line:
                raise IOError(f"WS handshake rejected: {status_line}")

            got_accept = resp_headers.get("sec-websocket-accept", "")
            if got_accept != expected_accept:
                raise IOError(f"Sec-WebSocket-Accept mismatch: {got_accept!r}")

            sock.settimeout(None)
            return sock

        except Exception as e:
            self.log(f"ws handshake failed: {e}")
            try:
                sock.close()
            except Exception:
                pass
            return None

    def _jitter_delay(self, backoff: float) -> float:
        jitter = random.uniform(1 - self.BACKOFF_JITTER, 1 + self.BACKOFF_JITTER)
        return min(backoff * jitter, self.BACKOFF_MAX)

    def run_forever(self) -> None:
        backoff = self.BACKOFF_BASE
        while True:
            sock = self._connect()
            if sock is None:
                delay = self._jitter_delay(backoff)
                self.log(f"reconnect in {delay:.1f}s")
                time.sleep(delay)
                backoff = min(backoff * 2, self.BACKOFF_MAX)
                continue

            backoff = self.BACKOFF_BASE
            self.log("ws connected")
            if self.on_connect:
                self.on_connect()

            disconnected = False
            last_frame_time = time.time()
            last_ping_time = time.time()

            while not disconnected:
                try:
                    readable, _, _ = select.select([sock], [], [], 1.0)
                except Exception:
                    disconnected = True
                    break

                now = time.time()

                if now - last_frame_time > self.DEAD_TIMEOUT:
                    self.log(f"no daemon frame for {self.DEAD_TIMEOUT}s, reconnecting")
                    disconnected = True
                    break

                if now - last_ping_time >= self.PING_INTERVAL:
                    try:
                        ws_send_masked(sock, 0x9, b"ping")
                    except Exception:
                        disconnected = True
                        break
                    last_ping_time = now

                if not readable:
                    continue

                try:
                    opcode, payload = ws_read_frame(sock)
                except (KeyboardInterrupt, SystemExit):
                    raise
                except Exception as e:
                    self.log(f"ws read error: {type(e).__name__}: {e}")
                    disconnected = True
                    break

                last_frame_time = time.time()

                if opcode == 0x1:  # text frame
                    try:
                        msg = json.loads(payload.decode("utf-8"))
                    except Exception:
                        continue
                    self.on_text(msg)

                elif opcode == 0x9:  # ping from daemon
                    try:
                        ws_send_masked(sock, 0xA, payload)
                    except Exception:
                        disconnected = True

                elif opcode == 0xA:  # pong from daemon
                    pass

                elif opcode == 0x8:  # close
                    disconnected = True

                else:
                    try:
                        ws_send_masked(sock, 0x8, b"")
                    except Exception:
                        pass
                    disconnected = True

            try:
                sock.close()
            except Exception:
                pass

            delay = self._jitter_delay(backoff)
            self.log(f"reconnecting in {delay:.1f}s")
            time.sleep(delay)
            backoff = min(backoff * 2, self.BACKOFF_MAX)
