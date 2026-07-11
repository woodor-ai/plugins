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
  * CODEX SIDE — wake-only, strictly poll:
      1. global serial FIFO (one worker thread; one message at a time)
      2. per-message ws connect to the codex app-server, closed after use
      3. idle double-read before injecting (only inject when the thread is idle)
      4. turn/start injects the message as a turn — and that's it. The bridge does
         NOT read the turn back or relay a reply: the live codex session sees the
         message and replies on its own via the `meeting-say` CLI. Outbound is
         entirely codex's job.

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
import json
import os
import queue
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

# Shared WS/discovery/project-derivation kernel: copied into DATA/bin alongside
# monitor.py by session-bootstrap.py's ensure_bin_wrappers (every .py file in
# the plugin's bin/ is copied there), so it is importable once the runtime has
# been bootstrapped at least once.
sys.path.insert(0, str(DATA / "bin"))
try:
    import meeting_common
except ImportError:
    sys.stderr.write(f"codex-bridge: missing shared module meeting_common at {DATA / 'bin'} "
                      "-- run session-bootstrap.py (or install.py) first.\n")
    sys.exit(3)

BRIDGE_START = int(time.time())  # created_at floor for FIRST contact with a peer

# idle-check knobs
_IDLE_GAP_S = 2.0
_IDLE_MAX_WAIT_S = 90.0
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


_derive_project = meeting_common.derive_project


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
    run_cwd = CWD if os.path.isdir(CWD) else None
    return meeting_common.run_meeting_cli(
        MEETING_CLI, *extra, python=sys.executable,
        host=(CTRL_BASE if use_host and CTRL_BASE else None),
        cwd=run_cwd, timeout=timeout)


# ---------------------------------------------------------------------------
# P0-1: resolve the ONE control endpoint at startup. WS + all meeting calls
# share it. CONTROL_URL wins; else discover via `meeting controls`.
# ---------------------------------------------------------------------------
CTRL_HOST = ""
CTRL_PORT = 0
CTRL_BASE = ""   # e.g. http://10.0.0.114:8765 — passed as --host to every meeting call


def _discover_control_endpoint():
    """Return (host, port, base_url) via mDNS/LAN discovery, or None."""
    # controls discovery does not take --host
    info = meeting_common.discover_control(lambda *a: _run_meeting(*a, use_host=False))
    ip, port = info.get("ip", ""), info.get("port", "")
    if not ip or not port:
        return None
    return ip, int(port), info.get("base_url", "")


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
def _fetch_new_messages(room: str, cursor: int, known: bool):
    """Return (rows, new_cursor). rows = [(id, created_at, sender_name, kind, body), ...]
    ascending, inbound only.

    known=True (room has a persisted cursor): process everything id>cursor, even
    if created before this process started — that IS the disconnect catch-up.
    known=False (first contact): additionally require created_at >= BRIDGE_START
    so we never replay a long history on first sight of a room.

    TSV columns from `meeting read`: id TAB created_at TAB sender_id TAB kind TAB ask TAB body
    split(..., 5) keeps body intact even when it contains tab characters.
    """
    try:
        r = _run_meeting("read", SELF, room, "--since", str(cursor), "--limit", "50")
    except Exception as e:
        _log(f"meeting read {room} failed: {e}")
        return [], cursor
    if r.returncode != 0:
        _log(f"meeting read {room} rc={r.returncode}: {(r.stderr or '').strip()[:160]}")
        return [], cursor
    out = []
    new_cursor = cursor
    for line in r.stdout.splitlines():
        parts = line.split("\t", 5)
        if len(parts) < 6:
            continue
        try:
            mid = int(parts[0]); created = int(parts[1])
        except ValueError:
            continue
        sender_id = parts[2]
        kind = parts[3]
        body = parts[5].replace("\\n", "\n")
        new_cursor = max(new_cursor, mid)
        if mid <= cursor:
            continue
        if sender_id.split("@", 1)[0] == SELF:   # skip our own replies
            continue
        if not known and created < BRIDGE_START:  # first contact: no history replay
            continue
        out.append((mid, created, sender_id.split("@", 1)[0], kind, body))
    return out, new_cursor


# ---------------------------------------------------------------------------
# Codex side: inject one message so the live codex session SEES it. The bridge
# only wakes codex — codex replies on its own via meeting-say; there is no
# read-back / relay. Raises _NotInjected if the failure happened BEFORE the turn
# started (safe to retry). Returns True once the turn is started, False if the
# thread never went idle or the turn did not start.
# ---------------------------------------------------------------------------
class _NotInjected(ConnectionError):
    pass


async def _codex_inject(ws_addr: str, thread_id: str, msg_id: int, text: str):
    import websockets as _ws
    try:
        ws = await _ws.connect(ws_addr, max_size=None, open_timeout=10)
    except Exception as e:
        raise _NotInjected(f"connect {ws_addr} failed: {e}")

    pend = {}
    nid = 0

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
                    return False  # not retryable (thread genuinely busy)
                await asyncio.sleep(_IDLE_GAP_S)
        except (ConnectionError, asyncio.TimeoutError) as e:
            raise _NotInjected(str(e))

        # --- inject only ---
        # codex sees the message as a turn and handles its OWN reply via meeting-say.
        # The bridge deliberately does NOT read the turn back or relay a reply —
        # outbound is entirely codex's job now.
        r = await call("turn/start", {"threadId": thread_id,
                                      "input": [{"type": "text", "text": text}]}, timeout=60)
        if not (r.get("result") or {}).get("turn", {}).get("id"):
            _log(f"turn/start returned no turn for msg {msg_id}")
            return False
        return True
    finally:
        task.cancel()
        try:
            await ws.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# control:* instructions (kind column) — never inferred from body text.
# Fresh control:restart / control:clear are injected as a turn with an
# explicit prefix; codex executes the actual action per AGENTS.md. Stale or
# unknown control kinds are logged only (never injected — keeps the live
# session free of noise for garbage that shouldn't wake it at all).
# ---------------------------------------------------------------------------
_CONTROL_STALE_S = 600

_CONTROL_DIRECTIVES = {
    "restart": "Write a handoff card summarizing in-flight state now, then stop "
               "accepting new tasks and wait for this session to end.",
    "clear": "Abort whatever task is in flight, clear your working context, and "
             "report back that you have been cleared.",
}


def _handle_control(room: str, mid: int, created: int, sender: str, kind: str):
    """Return injection text for a fresh, known control:<action>; None (log-only) otherwise."""
    action = kind.split(":", 1)[1] if ":" in kind else ""
    age_s = int(time.time()) - created
    if age_s > _CONTROL_STALE_S or created < BRIDGE_START:
        mins = max(0, age_s) // 60
        _log(f"ignoring stale control:{action} msg {mid} from {sender} in {room} ({mins} min ago)")
        return None
    directive = _CONTROL_DIRECTIVES.get(action)
    if directive is None:
        _log(f"unknown control instruction 'control:{action}' msg {mid} from {sender} in {room}; ignoring")
        return None
    return f"[control:{action} from peer={sender}] {directive}"


# ---------------------------------------------------------------------------
# Group charter — injected ahead of the body for group messages only (never
# for 1:1). Cached per group name for a few minutes so a burst of group
# messages doesn't re-run `meeting group charter` on every single one.
# ---------------------------------------------------------------------------
_GROUP_CHARTER_TTL_S = 180
_group_charter_cache: dict = {}  # group_name -> (fetched_monotonic, charter_text_or_empty)


def _get_group_charter(group_name: str) -> str:
    now = time.monotonic()
    cached = _group_charter_cache.get(group_name)
    if cached and now - cached[0] < _GROUP_CHARTER_TTL_S:
        return cached[1]
    charter = ""
    try:
        r = _run_meeting("group", "charter", group_name)
        if r.returncode == 0:
            out = (r.stdout or "").strip()
            if out and not out.startswith("(no charter set"):
                charter = out
        else:
            _log(f"group charter lookup rc={r.returncode} for {group_name}: "
                 f"{(r.stderr or '').strip()[:160]}")
    except Exception as e:
        _log(f"group charter lookup failed for {group_name}: {e}")
    _group_charter_cache[group_name] = (now, charter)
    return charter


# ---------------------------------------------------------------------------
# Worker: drain the global serial FIFO, one message at a time.
# Queue item: (room, msg_id, sender_name, text) — text is the fully composed
# injection text (control prefix / group envelope+charter / peer envelope
# already applied by _process_room).
# ---------------------------------------------------------------------------
_Q: "queue.Queue[tuple]" = queue.Queue()


def _inject_with_retry(ws_addr, thread_id, msg_id, text):
    """Return True if the message was delivered into the codex session, else False.
    Retries ONLY pre-injection connect failures."""
    attempt = 0
    while True:
        try:
            return bool(asyncio.run(_codex_inject(ws_addr, thread_id, msg_id, text)))
        except _NotInjected as e:
            attempt += 1
            if attempt > _CONNECT_RETRIES:
                _log(f"msg {msg_id}: give up after {attempt} connect attempts: {e}")
                return False
            _log(f"msg {msg_id}: pre-injection failure ({e}); retry {attempt}/{_CONNECT_RETRIES}")
            time.sleep(_CONNECT_RETRY_GAP_S)
        except Exception as e:
            _log(f"msg {msg_id}: unexpected inject error: {type(e).__name__}: {e}")
            return False


def _worker():
    while True:
        room, msg_id, sender, text = _Q.get()
        try:
            with _room_lock:
                blocked = room in _peer_blocked
            if blocked:
                # Room is frozen after an earlier injection failure: drop this
                # message rather than inject it out of order. _retry_worker
                # (or a reconnect) will re-fetch it from the committed cursor
                # once the room is unblocked, so nothing is lost -- just not
                # injected via this stale queue entry.
                _log(f"msg {msg_id} from {sender}: {room} is blocked; dropping "
                     f"(will be re-fetched once unblocked)")
                continue
            mapping = _read_json(MAPPING_FILE)
            thread_id = mapping.get("session_id")
            ws_addr = mapping.get("ws_addr")
            if not thread_id or not ws_addr:
                _log(f"no mapping for msg {msg_id} from {sender}; blocking {room} until reconnect "
                     f"(codex session has not started a turn yet, so no session_id)")
                with _room_lock:
                    _peer_blocked.add(room)
                continue
            _log(f"waking codex with msg {msg_id} from {sender} -> thread {thread_id}")
            delivered = _inject_with_retry(ws_addr, thread_id, msg_id, text)
            if delivered:
                with _room_lock:
                    _cursors[room] = max(_cursors.get(room, 0), msg_id)
                    _save_cursors(_cursors)
                    _retry_counts.pop((room, msg_id), None)
                _log(f"msg {msg_id} from {sender}: delivered into codex session")
            else:
                with _room_lock:
                    _peer_blocked.add(room)
                    _retry_counts[(room, msg_id)] = _retry_counts.get((room, msg_id), 0) + 1
                _log(f"msg {msg_id} from {sender}: NOT delivered; blocking {room}")
        except Exception as e:
            _log(f"worker error on msg {msg_id} from {sender}: {type(e).__name__}: {e}")
        finally:
            _Q.task_done()


# ---------------------------------------------------------------------------
# Retry: periodically un-freeze blocked rooms. _peer_blocked/_session_cursors
# are also touched by _on_connect (main WS thread) and _process_room, so every
# access is guarded by _room_lock -- this is the only extra concurrency this
# fix introduces (injection itself stays serialized through the single
# _worker thread / _Q, so no concurrent turn/start calls are added).
# ---------------------------------------------------------------------------
_RETRY_INTERVAL_S = 30.0


def _retry_worker():
    while True:
        time.sleep(_RETRY_INTERVAL_S)
        with _room_lock:
            retry_rooms = [(r, _room_groups.get(r)) for r in _peer_blocked]
            _peer_blocked.difference_update(r for r, _ in retry_rooms)
            for r, _ in retry_rooms:
                _session_cursors.pop(r, None)
        for room, group in retry_rooms:
            _log(f"retrying blocked room {room}")
            _process_room(room, group=group)


# ---------------------------------------------------------------------------
# WS /subscribe kernel: meeting_common.WSSubscribeClient (shared with
# monitor.py). Uses the single resolved control endpoint (CTRL_HOST/CTRL_PORT)
# -- resolve_addr below is a fixed closure, never re-discovered per reconnect
# (that's the P0-1 single-control-endpoint invariant).
# ---------------------------------------------------------------------------

# _room_lock guards every shared mutable structure below (_cursors,
# _session_cursors, _known_peers, _peer_blocked, _room_groups, _retry_counts).
# Two threads touch them concurrently now: the main WS thread (_on_text /
# _on_connect) and _retry_worker. _process_room holds the lock for its whole
# body (including the `meeting read` subprocess call) so two threads can never
# both fetch+enqueue the same room at once -- the only alternative would be a
# race that double-enqueues (and so double-injects) the same message.
_room_lock = threading.Lock()

_cursors: dict = {}          # committed cursors (persisted to disk only on success)
_session_cursors: dict = {}  # in-session fetch horizon (not persisted; dedup within session)
_known_peers: set = set()
_peer_blocked: set = set()   # rooms blocked after an injection failure; cursor frozen
_room_groups: dict = {}      # room name -> group name, or None for DMs
_retry_counts: dict = {}     # (room, msg_id) -> consecutive failed injection rounds

_MAX_MSG_RETRY_ROUNDS = 5    # after this many failed rounds, stop retrying the
                             # original body (e.g. oversized message that will
                             # never inject) and hand out a stand-in notice instead


def _process_room(room: str, group: str = None):
    """Fetch new inbound from room (peer or group name), enqueue. Does NOT persist cursor.

    group=None: DM room; group=<name>: group room; prefix injected text accordingly.
    Cursor is only advanced (and persisted) in the worker after successful injection.

    control:* messages (kind column) never go through the normal body/charter
    formatting below — they are routed through _handle_control instead, and a
    stale/unknown one is dropped here (log-only, never enqueued).
    """
    with _room_lock:
        _room_groups[room] = group
        known = room in _known_peers
        since = max(_session_cursors.get(room, 0), _cursors.get(room, 0))
        rows, new_horizon = _fetch_new_messages(room, since, known)
        _session_cursors[room] = new_horizon
        _known_peers.add(room)
        for mid, created, sender, kind, body in rows:
            if kind.startswith("control:"):
                text = _handle_control(room, mid, created, sender, kind)
                if text is None:
                    continue
            elif group:
                prefix = f"[group={group} peer={sender} msg_id={mid}]"
                charter = _get_group_charter(group)
                text = f"{prefix} [group charter] {charter}\n{body}" if charter else f"{prefix} {body}"
            else:
                text = f"[peer={sender} msg_id={mid}] {body}"

            retry_count = _retry_counts.get((room, mid), 0)
            if retry_count >= _MAX_MSG_RETRY_ROUNDS:
                _log(f"msg {mid} from {sender} in {room}: giving up on the original body "
                     f"after {retry_count} failed injection rounds (likely too large); "
                     f"injecting a stand-in notice instead so the room can unblock")
                text = (f"[peer={sender} msg_id={mid}] bridge notice: this message failed "
                        f"to inject {retry_count} times in a row and is being skipped -- "
                        f"run `meeting show {SELF} {sender}` to read the original body directly.")

            _log(f"queue msg {mid} from {sender} in {room}")
            _Q.put((room, mid, sender, text))


def _on_text(msg: dict) -> None:
    if msg.get("type") != "msg":
        return
    sender = msg.get("sender", "")
    sender_project = msg.get("sender_project", "")
    group = msg.get("group") or None
    if sender == SELF and sender_project == PROJECT:
        return  # self-sent
    if group:
        # Group message: honour mention filter same as monitor.py. If mention
        # field is present and falsy, the message was directed at someone else
        # (@them, not @us) — skip it.
        if "mention" in msg and not msg["mention"]:
            return
        _process_room(group, group=group)
    else:
        _process_room(sender)


def _on_connect() -> None:
    # CTRL_BASE is fixed (resolved once in main()) -- already logged at startup.
    _register()
    # On reconnect: unblock rooms that had injection failures so their messages
    # can be retried. Clear session cursors for blocked rooms so _process_room
    # re-fetches from the committed cursor (not the in-session fetch horizon).
    with _room_lock:
        for r in _peer_blocked:
            _session_cursors.pop(r, None)
        _peer_blocked.clear()
        known_rooms = [(r, _room_groups.get(r)) for r in _known_peers]
    # P1-4: catch up all known rooms for anything sent while disconnected.
    for room, group in known_rooms:
        _process_room(room, group=group)


def main():
    _resolve_control()
    _cursors.update(_load_cursors())
    _known_peers.update(_cursors.keys())
    _log(f"starting (project={PROJECT}, cwd={CWD}, control={CTRL_BASE}, "
         f"known_peers={sorted(_known_peers)})")
    _register()
    t = threading.Thread(target=_worker, name="codex-inject-worker", daemon=True)
    t.start()
    rt = threading.Thread(target=_retry_worker, name="codex-inject-retry", daemon=True)
    rt.start()
    ws_client = meeting_common.WSSubscribeClient(
        self_name=SELF, project=PROJECT,
        resolve_addr=lambda: (CTRL_HOST, CTRL_PORT),  # fixed -- resolved once above (P0-1)
        read_token=lambda: meeting_common.read_auth_token(DATA),
        on_text=_on_text, on_connect=_on_connect, log=_log,
    )
    try:
        ws_client.run_forever()
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
