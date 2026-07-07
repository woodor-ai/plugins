#!/usr/bin/env python3
"""
agent-meeting: Codex bridge daemon (form-B "live session wake").

Bridges incoming agent-meeting messages into a live codex interactive session
(hosted on a codex app-server thread) and relays the model's reply back.

Architecture (design review, 2026-07-07):
  * INBOUND + HEARTBEAT — forked from monitor.py's WS /subscribe kernel. One
    persistent WS to the agent-meeting control: send X-Meeting-Name, receive
    pushed `type:msg` frames, answer daemon pings / send our own pings so the
    control keeps `last_seen` fresh (this is the session's online heartbeat).
    A subscribe frame carries only the sender, not the body, so on each frame
    we `meeting read <name> <peer> --since <cursor>` to fetch the body.
  * CODEX SIDE — strictly poll, per the 4 concurrency constraints:
      1. global serial FIFO (one worker thread; one message at a time)
      2. per-message ws connect to the codex app-server, closed after use
      3. idle double-read before injecting (only inject when the thread is idle)
      4. turn/start stores result.turn.id, then the reply is read back by that
         EXACT turn.id (never turns[-1]) so a shared thread never mis-attributes.

Single control endpoint (cross-review P0-1): the control host:port is resolved
ONCE at startup (CONTROL_URL from runtime.json wins; else mDNS/LAN discovery via
`meeting controls`). The WS /subscribe socket AND every `meeting` CLI call share
that one endpoint, so inbound and read/send can never split-brain across two
controls. Only plaintext http:// (→ plaintext ws) is supported for now; https://
is a hard error, not a silent fallback.

No-loss inbound (cross-review P1-4): per-peer read cursors are persisted to disk
and, on every (re)connect, known peers are caught up with a `read --since
<cursor>` so messages that arrived while disconnected are not dropped.

Mapping file (written by codex-register.py at session start):
  ~/.agent-meeting/codex/sessions/<name>.json
    = {name, session_id, ws_addr, cwd, source, ts}   (session_id == thread id)

Usage:
  codex-bridge.py <name>

Requires `websockets` in the agent-meeting venv (bootstrap installs it).
"""

import argparse
import asyncio
import base64
import hashlib
import json
import os
import queue
import random
import select
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

try:
    import websockets  # noqa: F401  (used inside the asyncio codex client)
except ImportError:
    sys.stderr.write("codex-bridge: missing dependency `websockets` in the agent-meeting venv.\n")
    sys.exit(3)

# ---------------------------------------------------------------------------
# Paths / identity
# ---------------------------------------------------------------------------
_parser = argparse.ArgumentParser(prog="codex-bridge.py")
_parser.add_argument("name", help="agent-meeting session name to bridge")
_args = _parser.parse_args()

SELF = _args.name
HOME = Path.home()
DATA = Path(os.environ.get("MEETING_HOME") or (HOME / ".agent-meeting"))
CODEX_DIR = DATA / "codex"
RUNTIME_JSON = CODEX_DIR / "runtime.json"
MAPPING_FILE = CODEX_DIR / "sessions" / f"{SELF}.json"
CURSOR_FILE = CODEX_DIR / "cursors" / f"{SELF}.json"
MEETING_CLI = DATA / "bin" / "meeting"

BRIDGE_START = int(time.time())  # created_at floor for FIRST contact with a peer

# idle / turn polling knobs
_IDLE_GAP_S = 2.0
_IDLE_MAX_WAIT_S = 90.0
_TURN_POLL_S = 1.5
_TURN_TIMEOUT_S = 120.0
_CONNECT_RETRIES = 2       # retry a failed connect (before injection) this many times
_CONNECT_RETRY_GAP_S = 2.0


def _fatal(msg: str, code: int = 4):
    sys.stderr.write(f"codex-bridge {SELF}: FATAL: {msg}\n")
    sys.stderr.flush()
    sys.exit(code)


def _log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    sys.stderr.write(f"[codex-bridge {SELF}] {ts} {msg}\n")
    sys.stderr.flush()


def _derive_project(cwd: str) -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                           cwd=cwd, capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            name = os.path.basename(r.stdout.strip())
            return "_" if name == "*" else name
    except Exception:
        pass
    name = os.path.basename(os.path.normpath(cwd))
    return "_" if name == "*" else name


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


_RT = _read_json(RUNTIME_JSON)
CONTROL_URL = (_RT.get("control_url") or "").strip()
_MAP0 = _read_json(MAPPING_FILE)
CWD = _MAP0.get("cwd") or _RT.get("cwd") or str(HOME)
PROJECT = _derive_project(CWD)

# Startup sanity: the meeting CLI must exist (P1-3: fail loudly, not per-call).
if not MEETING_CLI.exists():
    _fatal(f"meeting CLI not found at {MEETING_CLI}", code=5)


# ---------------------------------------------------------------------------
# meeting CLI runner — venv python + extensionless script (NOT meeting.cmd),
# run from the session's registered cwd so `self` resolves under the right
# project (cx-test@ft, not cx-test@<launch-dir-project>).
# ---------------------------------------------------------------------------
def _run_meeting(*extra, timeout=20, use_host=True):
    env = os.environ.copy()
    cmd = [sys.executable, str(MEETING_CLI)] + list(extra)
    if use_host and CTRL_BASE:
        cmd += ["--host", CTRL_BASE]
    run_cwd = CWD if os.path.isdir(CWD) else None
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          env=env, cwd=run_cwd)


# ---------------------------------------------------------------------------
# P0-1: resolve the ONE control endpoint at startup. WS + all meeting calls
# share it. CONTROL_URL wins; else discover via `meeting controls`.
# ---------------------------------------------------------------------------
CTRL_HOST = ""
CTRL_PORT = 0
CTRL_BASE = ""   # e.g. http://10.0.0.114:8765 — passed as --host to every meeting call


def _discover_control_endpoint():
    """Return (host, port, base_url) via mDNS/LAN discovery, or None."""
    try:
        # controls discovery does not take --host
        r = _run_meeting("controls", "--json", use_host=False)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        controls = json.loads(r.stdout)
        if not controls:
            return None
        c = next((x for x in controls if x.get("is_current")), controls[0])
        ip = c.get("ip", "")
        port = int(c.get("port", 0) or 0)
        if not ip or not port:
            return None
        return ip, port, f"http://{ip}:{port}"
    except Exception:
        return None


def _resolve_control():
    global CTRL_HOST, CTRL_PORT, CTRL_BASE
    if CONTROL_URL:
        u = urlparse(CONTROL_URL)
        if u.scheme in ("https", "wss"):
            _fatal("https/wss control endpoint is not supported yet (plaintext ws only). "
                   f"Set a http:// control_url in runtime.json (got {CONTROL_URL!r}).")
        if u.scheme not in ("http", "ws"):
            _fatal(f"unrecognized control_url scheme {u.scheme!r} in {CONTROL_URL!r}")
        host = u.hostname
        port = u.port or 80
        if not host:
            _fatal(f"control_url has no host: {CONTROL_URL!r}")
        CTRL_HOST, CTRL_PORT = host, port
        CTRL_BASE = f"http://{host}:{port}"
        return
    disc = _discover_control_endpoint()
    if not disc:
        _fatal("no control_url in runtime.json and LAN discovery found no control. "
               "A tailnet-only target must set control_url in runtime.json.", code=6)
    CTRL_HOST, CTRL_PORT, CTRL_BASE = disc


# ---------------------------------------------------------------------------
# per-peer cursor persistence (P1-4)
# ---------------------------------------------------------------------------
def _load_cursors() -> dict:
    return {k: int(v) for k, v in _read_json(CURSOR_FILE).items()} if CURSOR_FILE.exists() else {}


def _save_cursors(cursors: dict) -> None:
    try:
        CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CURSOR_FILE.with_name(f".{CURSOR_FILE.name}.tmp-{os.getpid()}")
        tmp.write_text(json.dumps(cursors), encoding="utf-8")
        os.replace(tmp, CURSOR_FILE)
    except OSError as e:
        _log(f"cursor persist failed: {e}")


def _register():
    """Ensure the session is registered (idempotent upsert). Heartbeat is kept by
    the WS /subscribe ping-pong; registration must exist."""
    try:
        _run_meeting("online", SELF, "--cwd", CWD, "--force")
    except Exception as e:
        _log(f"re-register failed ({type(e).__name__}); will retry on reconnect")


# ---------------------------------------------------------------------------
# Inbound: fetch bodies via `meeting read`. `known` gates first-contact history.
# ---------------------------------------------------------------------------
def _fetch_new_messages(peer: str, cursor: int, known: bool):
    """Return (rows, new_cursor). rows = [(id, body), ...] ascending, inbound only.

    known=True (peer has a persisted cursor): process everything id>cursor, even
    if created before this process started — that IS the disconnect catch-up.
    known=False (first contact): additionally require created_at >= BRIDGE_START
    so we never replay a long history on first sight of a peer.
    """
    try:
        r = _run_meeting("read", SELF, peer, "--since", str(cursor), "--limit", "50")
    except Exception as e:
        _log(f"meeting read {peer} failed: {e}")
        return [], cursor
    if r.returncode != 0:
        _log(f"meeting read {peer} rc={r.returncode}: {(r.stderr or '').strip()[:160]}")
        return [], cursor
    out = []
    new_cursor = cursor
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        try:
            mid = int(parts[0]); created = int(parts[1])
        except ValueError:
            continue
        sender_id = parts[2]
        body = parts[5].replace("\\n", "\n")
        new_cursor = max(new_cursor, mid)
        if mid <= cursor:
            continue
        if sender_id.split("@", 1)[0] == SELF:   # skip our own replies
            continue
        if not known and created < BRIDGE_START:  # first contact: no history replay
            continue
        out.append((mid, body))
    return out, new_cursor


# ---------------------------------------------------------------------------
# Codex side: inject one message, read the reply back by turn.id (async).
# Raises ConnectionError if the failure happened BEFORE injection (safe to
# retry — the model never ran). Returns None if injected but no reply could be
# read back (NOT safe to retry — would double-inject). Returns text on success.
# ---------------------------------------------------------------------------
class _NotInjected(ConnectionError):
    pass


async def _codex_inject(ws_addr: str, thread_id: str, peer: str, msg_id: int, body: str):
    import websockets as _ws
    try:
        ws = await _ws.connect(ws_addr, max_size=None, open_timeout=10)
    except Exception as e:
        raise _NotInjected(f"connect {ws_addr} failed: {e}")

    pend = {}
    nid = 0
    injected = False

    async def recv_loop():
        # P0-2: on ANY loop end (exception OR normal close), fail every pending
        # future so awaiting callers wake immediately instead of hanging to timeout.
        err = None
        try:
            async for data in ws:
                try:
                    m = json.loads(data)
                except Exception:
                    continue
                if isinstance(m, dict) and "id" in m and "method" not in m:
                    fut = pend.pop(m["id"], None)
                    if fut and not fut.done():
                        fut.set_result(m)
        except Exception as e:
            err = e
        finally:
            if err is None:
                err = ConnectionError("codex app-server connection closed")
            for fut in list(pend.values()):
                if not fut.done():
                    fut.set_exception(err)
            pend.clear()

    task = asyncio.create_task(recv_loop())

    async def call(method, params=None, timeout=30):
        nonlocal nid
        if task.done():
            raise ConnectionError("receiver task ended; connection is dead")
        nid += 1
        rid = nid
        req = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            req["params"] = params
        fut = asyncio.get_event_loop().create_future()
        pend[rid] = fut
        try:
            await ws.send(json.dumps(req))
        except Exception as e:
            pend.pop(rid, None)
            raise ConnectionError(f"send failed: {e}")
        return await asyncio.wait_for(fut, timeout)

    def is_idle(st):
        return st.get("type") == "idle" if isinstance(st, dict) else st == "idle"

    def is_completed(st):
        return st.get("type") == "completed" if isinstance(st, dict) else st == "completed"

    try:
        # --- pre-injection phase (connection errors here are retryable) ---
        try:
            await call("initialize", {"clientInfo": {"name": "codex-bridge", "version": "1"}})
            await call("thread/resume", {"threadId": thread_id})

            deadline = time.monotonic() + _IDLE_MAX_WAIT_S
            while True:
                r1 = await call("thread/read", {"threadId": thread_id, "includeTurns": False})
                st1 = (r1.get("result") or {}).get("thread", {}).get("status")
                if is_idle(st1):
                    await asyncio.sleep(_IDLE_GAP_S)
                    r2 = await call("thread/read", {"threadId": thread_id, "includeTurns": False})
                    st2 = (r2.get("result") or {}).get("thread", {}).get("status")
                    if is_idle(st2):
                        break
                if time.monotonic() > deadline:
                    _log(f"thread {thread_id} not idle after {_IDLE_MAX_WAIT_S}s; skipping msg {msg_id}")
                    return None  # injected=False but not retryable (thread genuinely busy)
                await asyncio.sleep(_IDLE_GAP_S)
        except (ConnectionError, asyncio.TimeoutError) as e:
            raise _NotInjected(str(e))

        # --- inject (constraint 4a): from here a failure is NOT retryable ---
        text = f"[peer={peer} msg_id={msg_id}] {body}"
        r = await call("turn/start", {"threadId": thread_id,
                                      "input": [{"type": "text", "text": text}]}, timeout=60)
        injected = True
        turn_id = (r.get("result") or {}).get("turn", {}).get("id")
        if not turn_id:
            _log(f"turn/start returned no turn.id for msg {msg_id}")
            return None

        # --- read back by EXACT turn.id (constraint 4b) ---
        t_deadline = time.monotonic() + _TURN_TIMEOUT_S
        while time.monotonic() < t_deadline:
            try:
                rr = await call("thread/read", {"threadId": thread_id, "includeTurns": True})
            except (ConnectionError, asyncio.TimeoutError) as e:
                _log(f"read-back lost connection for msg {msg_id} (turn already injected): {e}")
                return None
            turns = (rr.get("result") or {}).get("thread", {}).get("turns") or []
            target = next((t for t in turns if isinstance(t, dict) and t.get("id") == turn_id), None)
            if target and is_completed(target.get("status")):
                texts = [it.get("text") for it in (target.get("items") or [])
                         if isinstance(it, dict) and it.get("type") == "agentMessage" and it.get("text")]
                return "\n".join(texts) if texts else ""
            await asyncio.sleep(_TURN_POLL_S)
        _log(f"turn {turn_id} (msg {msg_id}) not completed in {_TURN_TIMEOUT_S}s")
        return None
    finally:
        task.cancel()
        try:
            await ws.close()
        except Exception:
            pass


def _send_reply(peer: str, reply: str) -> None:
    """Relay the codex reply back to the peer (via --body-file for shell safety)."""
    tmp = CODEX_DIR / f".reply-{peer}-{os.getpid()}.txt"
    try:
        tmp.write_text(reply, encoding="utf-8")
        r = _run_meeting("send", SELF, peer, f"--body-file={tmp}", "--kind=回应")
        if r.returncode != 0:
            _log(f"meeting send {peer} rc={r.returncode}: {(r.stderr or '').strip()[:160]}")
    except Exception as e:
        _log(f"meeting send {peer} failed: {e}")
    finally:
        try:
            tmp.unlink()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Worker: drain the global serial FIFO, one message at a time.
# ---------------------------------------------------------------------------
_Q: "queue.Queue[tuple[str,int,str]]" = queue.Queue()


def _inject_with_retry(ws_addr, thread_id, peer, msg_id, body):
    """Return reply text or None. Retries ONLY pre-injection connect failures."""
    attempt = 0
    while True:
        try:
            return asyncio.run(_codex_inject(ws_addr, thread_id, peer, msg_id, body))
        except _NotInjected as e:
            attempt += 1
            if attempt > _CONNECT_RETRIES:
                _log(f"msg {msg_id}: give up after {attempt} connect attempts: {e}")
                return None
            _log(f"msg {msg_id}: pre-injection failure ({e}); retry {attempt}/{_CONNECT_RETRIES}")
            time.sleep(_CONNECT_RETRY_GAP_S)
        except Exception as e:
            _log(f"msg {msg_id}: unexpected inject error: {type(e).__name__}: {e}")
            return None


def _worker():
    while True:
        peer, msg_id, body = _Q.get()
        try:
            mapping = _read_json(MAPPING_FILE)
            thread_id = mapping.get("session_id")
            ws_addr = mapping.get("ws_addr")
            if not thread_id or not ws_addr:
                _log(f"no mapping (session_id/ws_addr) for msg {msg_id} from {peer}; skipping")
                _send_reply(peer, f"[codex-bridge] 会话未就绪（无映射），未处理 msg_id={msg_id}。")
                continue
            _log(f"injecting msg {msg_id} from {peer} -> thread {thread_id}")
            reply = _inject_with_retry(ws_addr, thread_id, peer, msg_id, body)
            if reply is None:
                _send_reply(peer, f"[codex-bridge] 注入超时/失败，未取到回复（msg_id={msg_id}）。")
            else:
                _send_reply(peer, reply)
                _log(f"replied to {peer} for msg {msg_id} ({len(reply)} chars)")
        except Exception as e:
            _log(f"worker error on msg {msg_id} from {peer}: {type(e).__name__}: {e}")
        finally:
            _Q.task_done()


# ---------------------------------------------------------------------------
# WS /subscribe kernel (forked from monitor.py) — inbound + heartbeat.
# Uses the single resolved control endpoint (CTRL_HOST/CTRL_PORT).
# ---------------------------------------------------------------------------
def _read_token():
    try:
        with open(DATA / "config.json") as f:
            return json.load(f).get("auth_token") or None
    except Exception:
        return None


def _ws_make_key():
    raw = base64.b64encode(os.urandom(16)).decode()
    accept = base64.b64encode(
        hashlib.sha1((raw + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
    ).decode()
    return raw, accept


def _ws_send_masked(sock, opcode, payload: bytes):
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    n = len(payload)
    if n < 126:
        header = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
    elif n < 65536:
        header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
    else:
        header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
    sock.sendall(header + mask + masked)


def _ws_recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise IOError("EOF")
        buf += chunk
    return buf


def _ws_read_frame(sock):
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
    mask_key = _ws_recv_exact(sock, 4) if masked else b""
    payload = _ws_recv_exact(sock, length) if length else b""
    if masked:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def _ws_connect():
    try:
        sock = socket.create_connection((CTRL_HOST, CTRL_PORT), timeout=10)
    except Exception as e:
        _log(f"ws connect failed ({CTRL_HOST}:{CTRL_PORT}): {e}")
        return None
    ws_key, expected_accept = _ws_make_key()
    token = _read_token()
    headers = [
        "GET /subscribe HTTP/1.1",
        f"Host: {CTRL_HOST}:{CTRL_PORT}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {ws_key}",
        "Sec-WebSocket-Version: 13",
        f"X-Meeting-Name: {SELF}",
        f"X-Meeting-Project: {PROJECT}",
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
                raise IOError("closed during handshake")
            line += ch
        status_line = line.decode().strip()
        resp = {}
        while True:
            hline = b""
            while not hline.endswith(b"\r\n"):
                ch = sock.recv(1)
                if not ch:
                    raise IOError("closed reading headers")
                hline += ch
            hline = hline.decode().strip()
            if not hline:
                break
            if ":" in hline:
                k, _, v = hline.partition(":")
                resp[k.strip().lower()] = v.strip()
        if "101" not in status_line:
            raise IOError(f"handshake rejected: {status_line}")
        if resp.get("sec-websocket-accept", "") != expected_accept:
            raise IOError("Sec-WebSocket-Accept mismatch")
        sock.settimeout(None)
        return sock
    except Exception as e:
        _log(f"ws handshake failed: {e}")
        try:
            sock.close()
        except Exception:
            pass
        return None


_WS_PING_INTERVAL = 5
_WS_DEAD_TIMEOUT = 15
_BACKOFF_BASE = 1.0
_BACKOFF_MAX = 30.0
_BACKOFF_JITTER = 0.20

_cursors: dict = {}
_known_peers: set = set()


def _process_peer(peer: str):
    """Fetch new inbound from peer, enqueue, persist cursor."""
    known = peer in _known_peers
    rows, _cursors[peer] = _fetch_new_messages(peer, _cursors.get(peer, 0), known)
    _known_peers.add(peer)
    if rows:
        _save_cursors(_cursors)
        for mid, body in rows:
            _log(f"queue msg {mid} from {peer}")
            _Q.put((peer, mid, body))


def _subscribe_loop():
    backoff = _BACKOFF_BASE
    while True:
        sock = _ws_connect()
        if sock is None:
            delay = min(backoff * random.uniform(1 - _BACKOFF_JITTER, 1 + _BACKOFF_JITTER), _BACKOFF_MAX)
            time.sleep(delay)
            backoff = min(backoff * 2, _BACKOFF_MAX)
            continue
        backoff = _BACKOFF_BASE
        _log(f"ws /subscribe connected to {CTRL_BASE}")
        _register()
        # P1-4: catch up known peers for anything sent while we were disconnected.
        for peer in list(_known_peers):
            _process_peer(peer)

        last_frame = time.time()
        last_ping = time.time()
        disconnected = False
        while not disconnected:
            try:
                readable, _, _ = select.select([sock], [], [], 1.0)
            except Exception:
                break
            now = time.time()
            if now - last_frame > _WS_DEAD_TIMEOUT:
                _log("no frame for dead-timeout; reconnecting")
                break
            if now - last_ping >= _WS_PING_INTERVAL:
                try:
                    _ws_send_masked(sock, 0x9, b"ping")
                except Exception:
                    break
                last_ping = now
            if not readable:
                continue
            try:
                opcode, payload = _ws_read_frame(sock)
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as e:
                _log(f"ws read error: {type(e).__name__}: {e}")
                break
            last_frame = time.time()

            if opcode == 0x1:  # text
                try:
                    msg = json.loads(payload.decode("utf-8"))
                except Exception:
                    continue
                if msg.get("type") == "msg":
                    sender = msg.get("sender", "")
                    sender_project = msg.get("sender_project", "")
                    if sender == SELF and sender_project == PROJECT:
                        continue  # self-sent
                    _process_peer(sender)
            elif opcode == 0x9:  # ping -> pong
                try:
                    _ws_send_masked(sock, 0xA, payload)
                except Exception:
                    break
            elif opcode == 0xA:  # pong
                pass
            elif opcode == 0x8:  # close
                break

        try:
            sock.close()
        except Exception:
            pass
        delay = min(backoff * random.uniform(1 - _BACKOFF_JITTER, 1 + _BACKOFF_JITTER), _BACKOFF_MAX)
        _log(f"reconnecting in {delay:.1f}s")
        time.sleep(delay)
        backoff = min(backoff * 2, _BACKOFF_MAX)


def main():
    _resolve_control()
    _cursors.update(_load_cursors())
    _known_peers.update(_cursors.keys())
    _log(f"starting (project={PROJECT}, cwd={CWD}, control={CTRL_BASE}, "
         f"known_peers={sorted(_known_peers)})")
    _register()
    t = threading.Thread(target=_worker, name="codex-inject-worker", daemon=True)
    t.start()
    try:
        _subscribe_loop()
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
