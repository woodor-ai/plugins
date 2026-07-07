#!/usr/bin/env python3
"""
agent-meeting: Codex bridge daemon (form-B "live session wake").

Bridges incoming agent-meeting messages into a live codex interactive session
(hosted on a codex app-server thread) and relays the model's reply back.

Architecture (decided with the design review, 2026-07-07):
  * INBOUND + HEARTBEAT — forked from monitor.py's WS /subscribe kernel. One
    persistent WS to the agent-meeting control: send X-Meeting-Name, receive
    pushed `type:msg` frames, answer daemon pings / send our own pings so the
    control keeps `last_seen` fresh (this is the session's online heartbeat).
    A subscribe frame carries only the sender, not the body, so on each frame
    we `meeting read <name> <peer> --since <cursor>` to fetch the body (exactly
    what monitor relies on). Messages are gated to created_at >= bridge start
    so a fresh bridge never replays history.
  * CODEX SIDE — strictly poll, per the 4 concurrency constraints:
      1. global serial FIFO (one worker thread; one message at a time)
      2. per-message ws connect to the codex app-server, closed after use
      3. idle double-read before injecting (only inject when the thread is idle)
      4. turn/start stores result.turn.id, then the reply is read back by that
         EXACT turn.id (never turns[-1]) so a shared thread never mis-attributes.

Mapping file (written by codex-register.py at session start):
  ~/.agent-meeting/codex/sessions/<name>.json
    = {name, session_id, ws_addr, cwd, source, ts}
  session_id == the codex thread id we thread/resume.

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

try:
    import websockets  # noqa: F401  (used inside asyncio codex client)
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
MEETING_CLI = DATA / "bin" / "meeting"

BRIDGE_START = int(time.time())  # only inject messages that arrive after we start

# idle / turn polling knobs
_IDLE_GAP_S = 2.0          # seconds between the two idle reads
_IDLE_MAX_WAIT_S = 90.0    # give up waiting for idle after this
_TURN_POLL_S = 1.5         # thread/read poll interval while awaiting the reply
_TURN_TIMEOUT_S = 120.0    # give up on a turn after this


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


def _read_runtime() -> dict:
    try:
        return json.loads(RUNTIME_JSON.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _read_mapping() -> dict:
    try:
        return json.loads(MAPPING_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


_RT = _read_runtime()
CONTROL_URL = (_RT.get("control_url") or "").strip()
# cwd for (re)registration — prefer mapping's cwd, fall back to runtime/home
_MAP0 = _read_mapping()
CWD = _MAP0.get("cwd") or _RT.get("cwd") or str(HOME)
PROJECT = _derive_project(CWD)


def _run_meeting(*extra, timeout=20):
    env = os.environ.copy()
    if sys.platform.startswith("win"):
        cli = DATA / "bin" / "meeting.cmd"
        cmd = [str(cli)] + list(extra)
    else:
        cmd = [str(MEETING_CLI)] + list(extra)
    # CRITICAL: `meeting read/send/online` derive THIS session's project from the
    # process cwd. The bridge is launched from an arbitrary directory, so we must
    # run every meeting call from the session's registered cwd (the mapping's cwd)
    # or self would resolve under the wrong project (e.g. cx-test@plugins instead
    # of cx-test@ft) and read/send the wrong conversation.
    run_cwd = CWD if os.path.isdir(CWD) else None
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          env=env, cwd=run_cwd)


def _host_args():
    return ["--host", CONTROL_URL] if CONTROL_URL else []


def _register():
    """Ensure the session is registered (idempotent upsert). Heartbeat itself is
    maintained by the WS /subscribe ping-pong, but registration must exist."""
    try:
        _run_meeting("online", SELF, "--cwd", CWD, "--force", *_host_args())
    except Exception as e:
        _log(f"re-register failed ({type(e).__name__}); will retry on reconnect")


# ---------------------------------------------------------------------------
# Inbound: fetch message bodies via `meeting read`
# ---------------------------------------------------------------------------
def _fetch_new_messages(peer: str, cursor: int):
    """Return (rows, new_cursor) for inbound messages from `peer` newer than
    `cursor` and created after bridge start. rows = [(id, body), ...] ascending."""
    try:
        r = _run_meeting("read", SELF, peer, "--since", str(cursor), "--limit", "50", *_host_args())
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
        # inbound only (skip our own replies in the conversation)
        sender_name = sender_id.split("@", 1)[0]
        if sender_name == SELF:
            continue
        # skip history that predates this bridge instance
        if created < BRIDGE_START:
            continue
        out.append((mid, body))
    return out, new_cursor


# ---------------------------------------------------------------------------
# Codex side: inject one message and read its reply back by turn.id (async)
# ---------------------------------------------------------------------------
async def _codex_inject(ws_addr: str, thread_id: str, peer: str, msg_id: int, body: str):
    """Return the agentMessage text for the injected turn, or None on failure."""
    import websockets as _ws
    async with _ws.connect(ws_addr, max_size=None) as ws:
        pend = {}
        nid = 0

        async def recv_loop():
            try:
                async for data in ws:
                    m = json.loads(data)
                    if isinstance(m, dict) and "id" in m and "method" not in m:
                        fut = pend.pop(m["id"], None)
                        if fut and not fut.done():
                            fut.set_result(m)
            except Exception:
                pass

        task = asyncio.create_task(recv_loop())

        async def call(method, params=None, timeout=30):
            nonlocal nid
            nid += 1
            rid = nid
            req = {"jsonrpc": "2.0", "id": rid, "method": method}
            if params is not None:
                req["params"] = params
            fut = asyncio.get_event_loop().create_future()
            pend[rid] = fut
            await ws.send(json.dumps(req))
            return await asyncio.wait_for(fut, timeout)

        def status_is_idle(st):
            if isinstance(st, dict):
                return st.get("type") == "idle"
            return st == "idle"

        def status_is_completed(st):
            if isinstance(st, dict):
                return st.get("type") == "completed"
            return st == "completed"

        try:
            await call("initialize", {"clientInfo": {"name": "codex-bridge", "version": "1"}})
            await call("thread/resume", {"threadId": thread_id})

            # constraint 3: idle double-read (only inject when the thread is idle)
            deadline = time.monotonic() + _IDLE_MAX_WAIT_S
            while True:
                r1 = await call("thread/read", {"threadId": thread_id, "includeTurns": False})
                st1 = (r1.get("result") or {}).get("thread", {}).get("status")
                if status_is_idle(st1):
                    await asyncio.sleep(_IDLE_GAP_S)
                    r2 = await call("thread/read", {"threadId": thread_id, "includeTurns": False})
                    st2 = (r2.get("result") or {}).get("thread", {}).get("status")
                    if status_is_idle(st2):
                        break
                if time.monotonic() > deadline:
                    _log(f"thread {thread_id} not idle after {_IDLE_MAX_WAIT_S}s; skipping msg {msg_id}")
                    return None
                await asyncio.sleep(_IDLE_GAP_S)

            # constraint 4a: inject, store the exact turn.id
            text = f"[peer={peer} msg_id={msg_id}] {body}"
            r = await call("turn/start", {"threadId": thread_id,
                                          "input": [{"type": "text", "text": text}]}, timeout=60)
            turn_id = (r.get("result") or {}).get("turn", {}).get("id")
            if not turn_id:
                _log(f"turn/start returned no turn.id for msg {msg_id}")
                return None

            # constraint 4b: read the reply back by that EXACT turn.id
            t_deadline = time.monotonic() + _TURN_TIMEOUT_S
            while time.monotonic() < t_deadline:
                rr = await call("thread/read", {"threadId": thread_id, "includeTurns": True})
                turns = (rr.get("result") or {}).get("thread", {}).get("turns") or []
                target = next((t for t in turns if isinstance(t, dict) and t.get("id") == turn_id), None)
                if target and status_is_completed(target.get("status")):
                    texts = [it.get("text") for it in (target.get("items") or [])
                             if isinstance(it, dict) and it.get("type") == "agentMessage" and it.get("text")]
                    return "\n".join(texts) if texts else ""
                await asyncio.sleep(_TURN_POLL_S)

            _log(f"turn {turn_id} (msg {msg_id}) not completed in {_TURN_TIMEOUT_S}s")
            return None
        finally:
            task.cancel()


def _send_reply(peer: str, reply: str) -> None:
    """Relay the codex reply back to the peer (via --body-file for shell safety)."""
    tmp = CODEX_DIR / f".reply-{peer}-{os.getpid()}.txt"
    try:
        tmp.write_text(reply, encoding="utf-8")
        r = _run_meeting("send", SELF, peer, f"--body-file={tmp}", "--kind=回应", *_host_args())
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
# Worker: drain the global serial FIFO, one message at a time
# ---------------------------------------------------------------------------
_Q: "queue.Queue[tuple[str,int,str]]" = queue.Queue()


def _worker():
    while True:
        peer, msg_id, body = _Q.get()
        try:
            mapping = _read_mapping()
            thread_id = mapping.get("session_id")
            ws_addr = mapping.get("ws_addr")
            if not thread_id or not ws_addr:
                _log(f"no mapping (session_id/ws_addr) for msg {msg_id} from {peer}; skipping")
                continue
            _log(f"injecting msg {msg_id} from {peer} -> thread {thread_id}")
            reply = asyncio.run(_codex_inject(ws_addr, thread_id, peer, msg_id, body))
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
# WS /subscribe kernel (forked from monitor.py) — inbound + heartbeat
# ---------------------------------------------------------------------------
def _discover_control_info() -> dict:
    try:
        r = _run_meeting("controls", "--json")
        if r.returncode != 0 or not r.stdout.strip():
            return {}
        controls = json.loads(r.stdout)
        if not controls:
            return {}
        c = next((x for x in controls if x.get("is_current")), controls[0])
        return {"ip_port": f"{c.get('ip','')}:{c.get('port','')}"}
    except Exception:
        return {}


def _read_token():
    try:
        with open(DATA / "config.json") as f:
            return json.load(f).get("auth_token") or None
    except Exception:
        return None


def _resolve_ws_host():
    ip_port = _discover_control_info().get("ip_port", "")
    if not ip_port or ":" not in ip_port:
        return None
    try:
        ip, port_str = ip_port.rsplit(":", 1)
        return ip, int(port_str)
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
    addr = _resolve_ws_host()
    if not addr:
        return None
    ip, port = addr
    try:
        sock = socket.create_connection((ip, port), timeout=10)
    except Exception as e:
        _log(f"ws connect failed ({ip}:{port}): {e}")
        return None
    ws_key, expected_accept = _ws_make_key()
    token = _read_token()
    headers = [
        "GET /subscribe HTTP/1.1",
        f"Host: {ip}:{port}",
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


def _subscribe_loop():
    cursors: dict[str, int] = {}   # per-peer read cursor (max processed msg id)
    backoff = _BACKOFF_BASE
    while True:
        sock = _ws_connect()
        if sock is None:
            delay = min(backoff * random.uniform(1 - _BACKOFF_JITTER, 1 + _BACKOFF_JITTER), _BACKOFF_MAX)
            time.sleep(delay)
            backoff = min(backoff * 2, _BACKOFF_MAX)
            continue
        backoff = _BACKOFF_BASE
        _log("ws /subscribe connected")
        _register()  # re-register on each (re)connect, like monitor

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
                    # frame has no body — fetch it, enqueue new inbound messages
                    rows, cursors[sender] = _fetch_new_messages(sender, cursors.get(sender, 0))
                    for mid, body in rows:
                        _log(f"queue msg {mid} from {sender}")
                        _Q.put((sender, mid, body))
            elif opcode == 0x9:  # ping from daemon -> pong
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
    _log(f"starting (project={PROJECT}, cwd={CWD}, control={CONTROL_URL or 'autodiscover'})")
    _register()
    t = threading.Thread(target=_worker, name="codex-inject-worker", daemon=True)
    t.start()
    try:
        _subscribe_loop()
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
