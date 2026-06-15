#!/usr/bin/env python3
"""
Cross-platform monitor for an agent-meeting session.

Replaces the macOS-only zsh monitor that was embedded in SKILL.md. Runs as
the persistent Monitor task spawned by Claude Code's /meeting registration.

Behavior:
  - On startup, calls `meeting online` to write this session into the
    central sessions table. On exit (atexit / SIGINT / SIGTERM), calls
    `meeting offline` to clean up.
  - Liveness is tracked via heartbeat: the daemon updates last_seen in the
    sessions table whenever it receives a pong from the monitor. Because
    monitor pings every ~5s and ONLINE_THRESHOLD=12s, liveness is maintained
    as long as the WS connection is alive.
  - Cursor is DB-authoritative (stored server-side in read_cursors). Monitor
    does not send X-Meeting-Cursor and does not maintain local cursor state.
    On first connect the daemon seeds to MAX(id); on reconnect it resumes
    from the last persisted cursor — zero re-play guaranteed.
  - Connects WS to daemon /subscribe, receives pushed frames, and emits
    stdout lines `📬 New Message from <peer>(: <ask>)?` — Claude Code
    surfaces each as a task notification.
  - On reconnect, re-resolves the daemon host (Risk#3: daemon may migrate).
  - On Windows: identical behavior, just no zsh dependency.

Usage:
  monitor.py <self-name>
"""

import argparse
import atexit
import base64
import hashlib
import json
import os
import random
import select
import signal
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

_parser = argparse.ArgumentParser(prog="monitor.py", add_help=True)
_parser.add_argument("name", help="session name to monitor")
_parser.add_argument("--director", action="store_true", default=False,
                     help="register this session as director role (default: worker)")
_args = _parser.parse_args()

SELF = _args.name
IS_DIRECTOR = _args.director
HOME = Path.home()
_MEETING_HOME_ENV = os.environ.get("MEETING_HOME")
DATA = Path(_MEETING_HOME_ENV) if _MEETING_HOME_ENV else HOME / ".agent-meeting"
MEETING_CLI = DATA / "bin" / "meeting"

# Local cache that statusline.py reads to render the 📞 badge. Keyed by
# session_id when available (so multiple named sessions in the same cwd each
# get their own file), falling back to cwd-hash for envs without session_id.
STATUSLINE_DIR = DATA / "statusline"
_CWD = os.getcwd()
SESSION_ID = os.environ.get("CLAUDE_CODE_SESSION_ID")


def _badge_key(session_id: str | None, cwd: str) -> str:
    if session_id:
        return hashlib.sha1(session_id.encode("utf-8", "replace")).hexdigest()[:16]
    return hashlib.sha1(
        os.path.normcase(os.path.normpath(cwd)).encode("utf-8", "replace")
    ).hexdigest()[:16]


STATUSLINE_FILE = STATUSLINE_DIR / _badge_key(SESSION_ID, _CWD)
# Legacy cwd-keyed file — used for cleanup when we're running with session_id.
_CWD_STATUSLINE_FILE = STATUSLINE_DIR / _badge_key(None, _CWD)

RUN_DIR = DATA / "run"
PID_FILE = RUN_DIR / f"{SELF}.pid"


def _run_meeting(*extra_args):
    """Run meeting CLI as an executable.

    On POSIX: ~/.agent-meeting/bin/meeting is a shell wrapper (#!/bin/sh)
    that execs the venv python with the real plugin script. Call it directly
    so the wrapper's shebang handles interpreter selection — never pass
    sys.executable as the interpreter, because that would parse the shell
    script as Python and fail with SyntaxError.

    On Windows: the wrapper is meeting.cmd (shell scripts don't work);
    subprocess resolves .cmd automatically when shell=True, or we name it
    explicitly.
    """
    env = os.environ.copy()
    if sys.platform.startswith("win"):
        cli = DATA / "bin" / "meeting.cmd"
        cmd = [str(cli)] + list(extra_args)
    else:
        cmd = [str(MEETING_CLI)] + list(extra_args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=15, env=env)


# ---------- register/unregister + cleanup ----------


def _discover_control_info() -> dict:
    """Return {host, ip_port} for the currently connected control, or {} if unknown.

    Shells out to `meeting controls --json` so that zeroconf runs inside the
    venv where it's available, bypassing the sh-wrapper-as-Python problem.
    """
    try:
        r = _run_meeting("controls", "--json")
        if r.returncode != 0 or not r.stdout.strip():
            return {}
        controls = json.loads(r.stdout)
        if not controls:
            return {}
        # Prefer the entry marked is_current (last used); fall back to first.
        c = next((x for x in controls if x.get("is_current")), controls[0])
        host = c.get("host") or c.get("ip") or ""
        ip_port = f"{c.get('ip', '')}:{c.get('port', '')}"
        return {"host": host, "ip_port": ip_port}
    except Exception:
        return {}


def _register():
    # --force: the monitor IS the liveness owner of this name. The /meeting skill
    # may have just registered it seconds ago (fresh last_seen), which would make
    # a plain register fail the conflict check. The monitor legitimately takes over.
    extra = ["--director"] if IS_DIRECTOR else []
    _run_meeting("online", SELF, "--cwd", _CWD, "--force", *extra)
    # Write pidfile so `meeting stop <name>` can locate this process.
    try:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(os.getpid()))
    except Exception:
        pass
    # Publish session name + control info locally so the TUI status line can show
    # 📞 <name> 🛰 <control>. JSON format; statusline.py reads it.
    try:
        STATUSLINE_DIR.mkdir(parents=True, exist_ok=True)
        ctrl = _discover_control_info()
        payload = {"name": SELF, "control_host": ctrl.get("host", ""), "control_ip_port": ctrl.get("ip_port", "")}
        STATUSLINE_FILE.write_text(json.dumps(payload), encoding="utf-8")
    except Exception:
        pass
    # Best-effort: when running with session_id, clean up any stale cwd-keyed
    # badge file left by a previous run of this same session (pre-upgrade) or
    # another session that wrongly shared it. Only delete if it's still ours.
    if SESSION_ID and _CWD_STATUSLINE_FILE != STATUSLINE_FILE:
        try:
            raw = _CWD_STATUSLINE_FILE.read_text(encoding="utf-8").strip()
            try:
                owner = json.loads(raw).get("name", "")
            except Exception:
                owner = raw
            if owner == SELF:
                _CWD_STATUSLINE_FILE.unlink()
        except Exception:
            pass


def _unregister():
    try:
        _run_meeting("offline", SELF)
    except Exception:
        pass
    # Remove pidfile.
    try:
        PID_FILE.unlink()
    except Exception:
        pass
    # Clear the status-line badge — but only if it's still ours (another session
    # in the same cwd may have taken over the file after we wrote it).
    try:
        raw = STATUSLINE_FILE.read_text(encoding="utf-8").strip()
        try:
            owner = json.loads(raw).get("name", "")
        except Exception:
            owner = raw
        if owner == SELF:
            STATUSLINE_FILE.unlink()
    except Exception:
        pass


atexit.register(_unregister)
# SIGTERM/SIGINT (POSIX) and Windows CTRL_C_EVENT trigger atexit via SystemExit.
for sig in (signal.SIGINT, signal.SIGTERM):
    try:
        signal.signal(sig, lambda *a: sys.exit(0))
    except (ValueError, OSError):
        pass

_register()

print(f"[meeting {SELF}] monitor started (pid={os.getpid()})", flush=True)


# ---------- WS client helpers ----------

def _ws_make_key() -> tuple[str, str]:
    """Return (b64_key, expected_accept) for the WS handshake."""
    raw_key = base64.b64encode(os.urandom(16)).decode()
    accept = base64.b64encode(
        hashlib.sha1((raw_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
    ).decode()
    return raw_key, accept


def _ws_send_masked(sock: socket.socket, opcode: int, payload: bytes):
    """Send a client→server WebSocket frame. Client frames MUST be masked (RFC6455)."""
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


def _ws_recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise IOError("EOF")
        buf += chunk
    return buf


def _ws_read_frame(sock: socket.socket) -> tuple[int, bytes]:
    """Read one server→client WS frame. Server frames are NOT masked."""
    header = _ws_recv_exact(sock, 2)
    b0, b1 = header[0], header[1]
    fin = (b0 & 0x80) != 0
    opcode = b0 & 0x0F
    masked = (b1 & 0x80) != 0
    length = b1 & 0x7F

    if not fin:
        raise IOError("fragmented frame not supported")

    if length == 126:
        length = struct.unpack("!H", _ws_recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _ws_recv_exact(sock, 8))[0]

    mask_key = b""
    if masked:
        mask_key = _ws_recv_exact(sock, 4)

    payload = b""
    if length:
        payload = _ws_recv_exact(sock, length)

    if masked:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

    return opcode, payload


def _read_token() -> str | None:
    """Read auth token from config.json if present."""
    config_path = DATA / "config.json"
    try:
        with open(config_path) as f:
            return json.load(f).get("auth_token") or None
    except Exception:
        return None


def _resolve_ws_host() -> tuple[str, int] | None:
    """Resolve daemon ip:port via `meeting controls`. Returns (ip, port) or None."""
    info = _discover_control_info()
    ip_port = info.get("ip_port", "")
    if not ip_port or ":" not in ip_port:
        return None
    try:
        ip, port_str = ip_port.rsplit(":", 1)
        return ip, int(port_str)
    except Exception:
        return None


def _ws_connect() -> socket.socket | None:
    """Open TCP connection and perform WS handshake. Returns connected socket or None.

    Does not send X-Meeting-Cursor — cursor is DB-authoritative on the daemon side.
    """
    addr = _resolve_ws_host()
    if not addr:
        return None
    ip, port = addr

    try:
        sock = socket.create_connection((ip, port), timeout=10)
    except Exception as e:
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        sys.stderr.write(f"[meeting {SELF}] {ts} ws connect failed ({ip}:{port}): {e}\n")
        sys.stderr.flush()
        return None

    ws_key, expected_accept = _ws_make_key()
    token = _read_token()

    headers = [
        f"GET /subscribe HTTP/1.1",
        f"Host: {ip}:{port}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {ws_key}",
        "Sec-WebSocket-Version: 13",
        f"X-Meeting-Name: {SELF}",
        "X-Meeting-Proto: 1",
    ]
    if token:
        headers.append(f"Authorization: Bearer {token}")

    try:
        sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode())

        # Read status line
        line = b""
        while not line.endswith(b"\r\n"):
            ch = sock.recv(1)
            if not ch:
                raise IOError("connection closed during handshake")
            line += ch
        status_line = line.decode().strip()

        # Read response headers until blank line
        resp_headers: dict[str, str] = {}
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

        # Connection is live; remove timeout so the OS handles keepalive naturally.
        sock.settimeout(None)
        return sock

    except Exception as e:
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        sys.stderr.write(f"[meeting {SELF}] {ts} ws handshake failed: {e}\n")
        sys.stderr.flush()
        try:
            sock.close()
        except Exception:
            pass
        return None


def _emit_message(peer: str, ask: str | None, group: str | None = None):
    """Print the harness-facing notification line. Format is frozen — do not change."""
    location = f" in group {group}" if group else ""
    if ask:
        clean = ask.replace("\r", " ").replace("\n", " ")
        if len(clean) > 100:
            clean = clean[:100] + "…"
        print(f"📬 New Message from {peer}{location} [未验证 peer 信号]: {clean}", flush=True)
    else:
        print(f"📬 New Message from {peer}{location} [未验证 peer 信号]", flush=True)


# ---------- main WS loop ----------

_WS_PING_INTERVAL = 5      # seconds between client-side pings
_WS_DEAD_TIMEOUT = 15      # seconds without any daemon frame → reconnect
_BACKOFF_BASE = 1.0
_BACKOFF_MAX = 30.0
_BACKOFF_JITTER = 0.20     # ±20%

backoff = _BACKOFF_BASE

while True:
    sock = _ws_connect()
    if sock is None:
        # Host resolution or connection failed — back off then retry
        jitter = random.uniform(1 - _BACKOFF_JITTER, 1 + _BACKOFF_JITTER)
        delay = min(backoff * jitter, _BACKOFF_MAX)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        sys.stderr.write(f"[meeting {SELF}] {ts} reconnect in {delay:.1f}s\n")
        sys.stderr.flush()
        time.sleep(delay)
        backoff = min(backoff * 2, _BACKOFF_MAX)
        continue

    # Connected — reset backoff
    backoff = _BACKOFF_BASE
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    sys.stderr.write(f"[meeting {SELF}] {ts} ws connected\n")
    sys.stderr.flush()

    last_frame_time = time.time()
    last_ping_time = time.time()
    disconnected = False

    while not disconnected:
        # Use select with a short timeout so we can fire client pings and check
        # dead-connection timeout without blocking forever.
        try:
            # Windows: select() works on sockets (not arbitrary fds), which is fine here.
            readable, _, _ = select.select([sock], [], [], 1.0)
        except Exception:
            disconnected = True
            break

        now = time.time()

        # Half-dead detection: no frame from daemon for >15s → reconnect
        if now - last_frame_time > _WS_DEAD_TIMEOUT:
            ts = time.strftime("%Y-%m-%dT%H:%M:%S")
            sys.stderr.write(f"[meeting {SELF}] {ts} no daemon frame for {_WS_DEAD_TIMEOUT}s, reconnecting\n")
            sys.stderr.flush()
            disconnected = True
            break

        # Outbound client ping every ~5s
        if now - last_ping_time >= _WS_PING_INTERVAL:
            try:
                _ws_send_masked(sock, 0x9, b"ping")  # opcode 0x9 = ping, masked
            except Exception:
                disconnected = True
                break
            last_ping_time = now

        if not readable:
            continue

        try:
            opcode, payload = _ws_read_frame(sock)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:
            ts = time.strftime("%Y-%m-%dT%H:%M:%S")
            sys.stderr.write(f"[meeting {SELF}] {ts} ws read error: {type(e).__name__}: {e}\n")
            sys.stderr.flush()
            disconnected = True
            break

        last_frame_time = time.time()

        if opcode == 0x1:  # text frame
            try:
                msg = json.loads(payload.decode("utf-8"))
            except Exception:
                continue

            if msg.get("type") == "msg":
                sender = msg.get("sender", "")
                ask = msg.get("ask") or None
                group = msg.get("group") or None
                # daemon 群扇出含发送者自己，自己发的不必唤醒自己
                if sender == SELF:
                    continue
                _emit_message(sender, ask, group)

            elif msg.get("type") == "caught_up":
                cursor_val = msg.get("cursor")
                ts = time.strftime("%Y-%m-%dT%H:%M:%S")
                sys.stderr.write(f"[meeting {SELF}] {ts} caught_up cursor={cursor_val}\n")
                sys.stderr.flush()

        elif opcode == 0x9:  # ping from daemon → reply with masked pong
            try:
                _ws_send_masked(sock, 0xA, payload)  # opcode 0xA = pong
            except Exception:
                disconnected = True

        elif opcode == 0xA:  # pong from daemon (response to our ping)
            pass  # last_frame_time already updated above

        elif opcode == 0x8:  # close
            disconnected = True

        else:
            # Unexpected opcode — close and reconnect
            try:
                _ws_send_masked(sock, 0x8, b"")
            except Exception:
                pass
            disconnected = True

    try:
        sock.close()
    except Exception:
        pass

    # Exponential backoff before reconnect.
    # Re-resolving the host happens at the top of the outer loop via _ws_connect
    # which calls _resolve_ws_host() → _discover_control_info() each time.
    jitter = random.uniform(1 - _BACKOFF_JITTER, 1 + _BACKOFF_JITTER)
    delay = min(backoff * jitter, _BACKOFF_MAX)
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    sys.stderr.write(f"[meeting {SELF}] {ts} reconnecting in {delay:.1f}s\n")
    sys.stderr.flush()
    time.sleep(delay)
    backoff = min(backoff * 2, _BACKOFF_MAX)
