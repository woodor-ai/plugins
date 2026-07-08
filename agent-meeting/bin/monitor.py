#!/usr/bin/env python3
"""
Cross-platform monitor for an agent-meeting session.

Behavior:
  - On startup, calls `meeting online` to write this session into the
    central sessions table (project derived from cwd). On exit, calls
    `meeting offline`.
  - Liveness is tracked via WS pong: the daemon updates last_seen on pong.
  - Connects WS to daemon /subscribe, receives pushed frames, emits
    stdout lines for Claude Code task notifications.
  - WS handshake sends X-Meeting-Name and X-Meeting-Project headers.

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

if sys.platform.startswith("win"):
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

_parser = argparse.ArgumentParser(prog="monitor.py", add_help=True)
_parser.add_argument("name", help="session name to monitor")
_parser.add_argument("--director", action="store_true", default=False,
                     help="register this session as director role (default: worker)")
_parser.add_argument("--global", dest="is_global", action="store_true", default=False,
                     help="register as global identity (project='*'), skips cwd project derivation")
_args = _parser.parse_args()

SELF = _args.name
IS_DIRECTOR = _args.director
IS_GLOBAL = _args.is_global
HOME = Path.home()
_MEETING_HOME_ENV = os.environ.get("MEETING_HOME")
DATA = Path(_MEETING_HOME_ENV) if _MEETING_HOME_ENV else HOME / ".agent-meeting"
MEETING_CLI = DATA / "bin" / "meeting"

STATUSLINE_DIR = DATA / "statusline"
_CWD = os.getcwd()
SESSION_ID = os.environ.get("CLAUDE_CODE_SESSION_ID")


def _badge_key(session_id, cwd: str) -> str:
    if session_id:
        return hashlib.sha1(session_id.encode("utf-8", "replace")).hexdigest()[:16]
    return hashlib.sha1(
        os.path.normcase(os.path.normpath(cwd)).encode("utf-8", "replace")
    ).hexdigest()[:16]


STATUSLINE_FILE = STATUSLINE_DIR / _badge_key(SESSION_ID, _CWD)
_CWD_STATUSLINE_FILE = STATUSLINE_DIR / _badge_key(None, _CWD)

RUN_DIR = DATA / "run"
PID_FILE = RUN_DIR / f"{SELF}.pid"


def _derive_project(cwd: str) -> str:
    """Derive a stable project name from the main git working tree.

    Uses ``--git-common-dir`` (not ``--show-toplevel``) so a git worktree resolves
    to its MAIN repo identity instead of the worktree directory name. Otherwise a
    session running inside a worktree gets a per-command identity that diverges
    from its registered/online identity, and peer messages land in a room it can
    never read. Falls back to the cwd basename for non-git directories.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            common_dir = result.stdout.strip()
            if common_dir:
                # common_dir is the main repo's .git dir; its parent is the repo root
                name = os.path.basename(os.path.dirname(os.path.normpath(common_dir)))
                if name:
                    return "_" if name == "*" else name
    except Exception:
        pass
    name = os.path.basename(os.path.normpath(cwd))
    return "_" if name == "*" else name


# Derive project once at startup from cwd — stored for WS handshake
_PROJECT = "*" if IS_GLOBAL else _derive_project(_CWD)


def _run_meeting(*extra_args):
    env = os.environ.copy()
    if sys.platform.startswith("win"):
        cli = DATA / "bin" / "meeting.cmd"
        cmd = [str(cli)] + list(extra_args)
    else:
        cmd = [str(MEETING_CLI)] + list(extra_args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=15, env=env)


# ---------- register/unregister + cleanup ----------


def _discover_control_info() -> dict:
    try:
        r = _run_meeting("controls", "--json")
        if r.returncode != 0 or not r.stdout.strip():
            return {}
        controls = json.loads(r.stdout)
        if not controls:
            return {}
        c = next((x for x in controls if x.get("is_current")), controls[0])
        host = c.get("host") or c.get("ip") or ""
        ip_port = f"{c.get('ip', '')}:{c.get('port', '')}"
        return {"host": host, "ip_port": ip_port}
    except Exception:
        return {}


def _register():
    extra = ["--director"] if IS_DIRECTOR else []
    if IS_GLOBAL:
        extra.append("--global")
    # Best-effort: this runs on EVERY ws reconnect (see the connect loop), and a
    # reconnect often coincides with the control having just restarted — TCP is
    # back up but the daemon is still busy, so `online` can hang the full 15s and
    # raise TimeoutExpired. That must NOT kill the monitor (it would drop the
    # session to historical until a human restarts it — exactly the daemon-restart
    # case this re-register exists to cover). Swallow any failure; the next
    # reconnect cycle retries.
    try:
        _run_meeting("online", SELF, "--cwd", _CWD, "--force", *extra)
    except Exception as e:
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        sys.stderr.write(f"[meeting {_display_id}] {ts} re-register failed ({type(e).__name__}); "
                         f"will retry on next reconnect\n")
        sys.stderr.flush()
    try:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(os.getpid()))
    except Exception:
        pass
    try:
        STATUSLINE_DIR.mkdir(parents=True, exist_ok=True)
        ctrl = _discover_control_info()
        payload = {"name": SELF, "project": _PROJECT,
                   "control_host": ctrl.get("host", ""), "control_ip_port": ctrl.get("ip_port", "")}
        STATUSLINE_FILE.write_text(json.dumps(payload), encoding="utf-8")
    except Exception:
        pass
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
    try:
        PID_FILE.unlink()
    except Exception:
        pass
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
for sig in (signal.SIGINT, signal.SIGTERM):
    try:
        signal.signal(sig, lambda *a: sys.exit(0))
    except (ValueError, OSError):
        pass

_register()

_display_id = SELF if _PROJECT == "*" else f"{SELF}@{_PROJECT}"
print(f"[meeting {_display_id}] monitor started (pid={os.getpid()})", flush=True)


# ---------- WS client helpers ----------

def _ws_make_key() -> tuple[str, str]:
    raw_key = base64.b64encode(os.urandom(16)).decode()
    accept = base64.b64encode(
        hashlib.sha1((raw_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
    ).decode()
    return raw_key, accept


def _ws_send_masked(sock: socket.socket, opcode: int, payload: bytes):
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


def _read_token():
    config_path = DATA / "config.json"
    try:
        with open(config_path) as f:
            return json.load(f).get("auth_token") or None
    except Exception:
        return None


def _resolve_ws_host():
    info = _discover_control_info()
    ip_port = info.get("ip_port", "")
    if not ip_port or ":" not in ip_port:
        return None
    try:
        ip, port_str = ip_port.rsplit(":", 1)
        return ip, int(port_str)
    except Exception:
        return None


def _ws_connect():
    """Open TCP connection and perform WS handshake. Returns connected socket or None.

    Sends X-Meeting-Name and X-Meeting-Project headers (required by new daemon).
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
        f"X-Meeting-Project: {_PROJECT}",
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


def _emit_message(peer: str, ask, group=None, mentioned: bool = False):
    """Print the harness-facing notification line. Format is frozen -- do not change."""
    at_tag = " @you" if (group and mentioned) else ""
    location = f" in group {group}{at_tag}" if group else ""
    if ask:
        clean = ask.replace("\r", " ").replace("\n", " ")
        if len(clean) > 100:
            clean = clean[:100] + "..."
        print(f"New Message from {peer}{location} [unverified peer]: {clean}", flush=True)
    else:
        print(f"New Message from {peer}{location} [unverified peer]", flush=True)


# ---------- main WS loop ----------

_WS_PING_INTERVAL = 5
_WS_DEAD_TIMEOUT = 15
_BACKOFF_BASE = 1.0
_BACKOFF_MAX = 30.0
_BACKOFF_JITTER = 0.20

backoff = _BACKOFF_BASE

while True:
    sock = _ws_connect()
    if sock is None:
        jitter = random.uniform(1 - _BACKOFF_JITTER, 1 + _BACKOFF_JITTER)
        delay = min(backoff * jitter, _BACKOFF_MAX)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        sys.stderr.write(f"[meeting {SELF}] {ts} reconnect in {delay:.1f}s\n")
        sys.stderr.flush()
        time.sleep(delay)
        backoff = min(backoff * 2, _BACKOFF_MAX)
        continue

    backoff = _BACKOFF_BASE
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    sys.stderr.write(f"[meeting {_display_id}] {ts} ws connected\n")
    sys.stderr.flush()
    # Re-register on every reconnect so role/cwd are correct after daemon restart/wipe.
    _register()

    last_frame_time = time.time()
    last_ping_time = time.time()
    disconnected = False

    while not disconnected:
        try:
            readable, _, _ = select.select([sock], [], [], 1.0)
        except Exception:
            disconnected = True
            break

        now = time.time()

        if now - last_frame_time > _WS_DEAD_TIMEOUT:
            ts = time.strftime("%Y-%m-%dT%H:%M:%S")
            sys.stderr.write(f"[meeting {SELF}] {ts} no daemon frame for {_WS_DEAD_TIMEOUT}s, reconnecting\n")
            sys.stderr.flush()
            disconnected = True
            break

        if now - last_ping_time >= _WS_PING_INTERVAL:
            try:
                _ws_send_masked(sock, 0x9, b"ping")
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
                sender_project = msg.get("sender_project", "")
                ask = msg.get("ask") or None
                group = msg.get("group") or None
                # suppress self-sent messages
                if sender == SELF and sender_project == _PROJECT:
                    continue
                if "mention" in msg:
                    if not msg["mention"]:
                        continue
                    _emit_message(sender, ask, group, mentioned=True)
                else:
                    _emit_message(sender, ask, group)

            elif msg.get("type") == "caught_up":
                cursor_val = msg.get("cursor")
                ts = time.strftime("%Y-%m-%dT%H:%M:%S")
                sys.stderr.write(f"[meeting {SELF}] {ts} caught_up cursor={cursor_val}\n")
                sys.stderr.flush()

        elif opcode == 0x9:  # ping from daemon
            try:
                _ws_send_masked(sock, 0xA, payload)
            except Exception:
                disconnected = True

        elif opcode == 0xA:  # pong from daemon
            pass

        elif opcode == 0x8:  # close
            disconnected = True

        else:
            try:
                _ws_send_masked(sock, 0x8, b"")
            except Exception:
                pass
            disconnected = True

    try:
        sock.close()
    except Exception:
        pass

    jitter = random.uniform(1 - _BACKOFF_JITTER, 1 + _BACKOFF_JITTER)
    delay = min(backoff * jitter, _BACKOFF_MAX)
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    sys.stderr.write(f"[meeting {SELF}] {ts} reconnecting in {delay:.1f}s\n")
    sys.stderr.flush()
    time.sleep(delay)
    backoff = min(backoff * 2, _BACKOFF_MAX)
