#!/usr/bin/env python3
"""
am-msgd — central session and message hub for LAN-wide agent-meeting.

Single-host model: this process owns the SQLite DB. Other machines on the
LAN discover us via mDNS (_agent-meeting._tcp.local.) and hit our HTTP API
to do all operations (register / unregister / send / read / show /
turn / list / delete) — no direct DB access from clients.

Localhost clients also go through HTTP (uniform code path; localhost RTT
is <1ms so the cost is negligible).

Auth model: open by default (trusted-network assumption). Set `auth_token`
in config.json to enable Bearer-token enforcement on all routes except
/health. Token is re-read on every request — no central am-msgd restart required
after `meeting token` writes it.

Identity: sessions are identified by (project, name) composite key.
project is derived from git rev-parse --show-toplevel basename (or cwd basename
for non-git dirs) at registration time. Addressing: bare <name> resolves if
globally unique; ambiguous across projects requires <name>@<project>.

DEPLOY NOTE: this schema is incompatible with the old single-key schema.
Wipe ~/.agent-meeting/db/rooms.db before deploying.

Usage:
  am-msgd [--port 8765] [--bind 0.0.0.0]

Run forever; stop with SIGINT/SIGTERM (mDNS unregister + clean shutdown).
"""

import argparse
import base64
import hashlib
import json
import os
import signal
import socket
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from agent_meeting.message_hub.mdns_hub_advertiser import publish_message_hub
from agent_meeting.message_hub.sqlite_conversation_repository import (
    SQLiteConversationRepository,
)
from agent_meeting.message_hub.sqlite_group_repository import (
    SQLiteGroupRepository,
)
from agent_meeting.message_hub.sqlite_message_database import (
    is_group as _is_group,
    open_message_database,
    prepare_message_database,
)
from agent_meeting.message_hub.sqlite_session_repository import (
    SQLiteSessionRepository,
)
from agent_meeting.message_hub.websocket_subscriptions import (
    Subscriber,
    parse_mentions as _parse_mentions,
    read_frame as _ws_read_frame,
    send_close as _ws_send_close,
    send_ping as _ws_send_ping,
    send_text as _ws_send_text,
)

if sys.platform.startswith("win"):
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

MEETING_HOME = os.environ.get("MEETING_HOME") or os.path.expanduser("~/.agent-meeting")
DB_PATH = os.path.join(MEETING_HOME, "db", "rooms.db")
_CONFIG_PATH = os.path.join(MEETING_HOME, "config.json")
PROCESS_INSTANCE_ID = uuid.uuid4().hex

try:
    with open(_CONFIG_PATH) as _f:
        _plugin_version: str = json.load(_f).get("plugin_version") or "unknown"
except Exception:
    _plugin_version = "unknown"

# A session is considered online if last_seen is within this many seconds.
ONLINE_THRESHOLD = 12

# (project, name) -> list[Subscriber]
_subscribers: dict[tuple, list["Subscriber"]] = {}
_sub_lock = threading.Lock()


def _ws_remove(sub: "Subscriber"):
    """Remove a Subscriber from the global table. Safe to call multiple times."""
    with _sub_lock:
        lst = _subscribers.get(sub.key)
        if lst:
            try:
                lst.remove(sub)
            except ValueError:
                pass
            if not lst:
                del _subscribers[sub.key]
    # Legacy delivery subscribers retain their existing transport-cursor
    # behavior. Notify-only subscribers never acknowledge message delivery.
    if sub.mode == "delivery" and sub.high_water_mark > 0:
        try:
            with db() as conn:
                conn.execute(
                    "INSERT INTO read_cursors (project, member_name, cursor, updated_at) VALUES (?, ?, ?, ?)"
                    " ON CONFLICT(project, member_name) DO UPDATE SET"
                    "  cursor=MAX(excluded.cursor, read_cursors.cursor),"
                    "  updated_at=excluded.updated_at",
                    (sub.project, sub.name, sub.high_water_mark, int(time.time())),
                )
        except Exception:
            pass
    try:
        sub.sock.close()
    except Exception:
        pass


def _ws_fanout(msg_id: int, recipient_project: str, recipient_name: str,
               sender_project: str, sender_name: str, ask, body: str = ""):
    """Push a message to all live/draining subscribers for recipient (or group members)."""
    with db() as conn:
        is_grp = _is_group(conn, recipient_project, recipient_name)
        if is_grp:
            rows = conn.execute(
                "SELECT member_project, member_name FROM group_members"
                " WHERE group_project=? AND group_name=?",
                (recipient_project, recipient_name),
            ).fetchall()
            target_keys = [(r["member_project"], r["member_name"]) for r in rows]
        else:
            target_keys = [(recipient_project, recipient_name)]

    group_field = recipient_name if is_grp else None

    mention_targets: set = set()
    directed = False
    if is_grp and body:
        member_name_set = {k[1] for k in target_keys}
        mention_targets = _parse_mentions(body, member_name_set)
        directed = bool(mention_targets)

    base_payload = {
        "type": "msg",
        "msg_id": msg_id,
        "sender": sender_name,
        "sender_project": sender_project,
        "ask": ask or "",
        "group": group_field,
        "phase": "live",
    }

    dead = []
    now = int(time.time())
    for (tgt_project, tgt_name) in target_keys:
        if directed:
            payload = {**base_payload, "mention": tgt_name in mention_targets}
        else:
            payload = base_payload
        frame = json.dumps(payload, ensure_ascii=False)

        key = (tgt_project, tgt_name)
        with _sub_lock:
            subs = list(_subscribers.get(key, []))
        for sub in subs:
            with sub.send_lock:
                if sub.state != "live":
                    continue
                if sub.mode == "notify":
                    notify_frame = json.dumps(
                        {"type": "notify", "msg_id": msg_id},
                        ensure_ascii=False,
                    )
                    if not _ws_send_text(sub, notify_frame):
                        dead.append(sub)
                    continue
                if msg_id <= sub.high_water_mark:
                    continue
                ok = _ws_send_text(sub, frame)
                if ok:
                    sub.high_water_mark = msg_id
                    try:
                        with db() as conn2:
                            conn2.execute(
                                "INSERT INTO read_cursors (project, member_name, cursor, updated_at) VALUES (?, ?, ?, ?)"
                                " ON CONFLICT(project, member_name) DO UPDATE SET"
                                "  cursor=MAX(excluded.cursor, read_cursors.cursor),"
                                "  updated_at=excluded.updated_at",
                                (tgt_project, tgt_name, msg_id, now),
                            )
                    except Exception:
                        pass
                else:
                    dead.append(sub)

    for sub in dead:
        _ws_remove(sub)


# ---------- heartbeat + pong-timeout sweep ----------

_WS_PING_INTERVAL = 4
_WS_PONG_TIMEOUT = 15


def _ws_heartbeat_loop():
    last_sweep = time.time()
    while True:
        time.sleep(_WS_PING_INTERVAL)
        now = time.time()

        with _sub_lock:
            all_subs = [s for lst in _subscribers.values() for s in lst]

        dead = []
        for sub in all_subs:
            if not _ws_send_ping(sub):
                dead.append(sub)

        if now - last_sweep >= 5:
            last_sweep = now
            for sub in all_subs:
                if now - sub.last_pong > _WS_PONG_TIMEOUT and sub not in dead:
                    dead.append(sub)

        for sub in dead:
            _ws_remove(sub)


# ---------- DB helpers ----------

def db():
    return open_message_database(DB_PATH)


# ---------- HTTP handlers ----------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 30

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{self.address_string()} {fmt % args}\n")

    def _json(self, status: int, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, status: int, body: str):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _err(self, status, msg):
        self._json(status, {"error": msg})

    def _check_auth(self, path: str) -> bool:
        if path == "/health":
            return True
        expected = None
        try:
            with open(_CONFIG_PATH) as _f:
                expected = json.load(_f).get("auth_token") or None
        except Exception:
            pass
        if not expected:
            return True
        auth_header = self.headers.get("Authorization", "")
        if auth_header == f"Bearer {expected}":
            return True
        self._json(401, {"error": "unauthorized"})
        return False

    def _query(self):
        return parse_qs(urlparse(self.path).query)

    def _read_json_body(self):
        n = int(self.headers.get("Content-Length", "0"))
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def do_GET(self):
        path = urlparse(self.path).path
        if not self._check_auth(path):
            return

        if (path == "/subscribe" and
                self.headers.get("Upgrade", "").lower() == "websocket"):
            self._ws_upgrade()
            return

        if path == "/health":
            return self._json(200, {
                "ok": True,
                "host": socket.gethostname(),
                "version": _plugin_version,
                "instance_id": PROCESS_INSTANCE_ID,
            })
        q = self._query()
        try:
            if path == "/list":
                return self._json(200, self._list())
            if path == "/turn":
                return self._json(200, {"turn": self._turn(
                    q["self_project"][0], q["self"][0],
                    q["peer_project"][0], q["peer"][0],
                )})
            if path == "/show":
                return self._text(200, self._show(
                    q["self_project"][0], q["self"][0],
                    q["peer_project"][0], q["peer"][0],
                    int(q.get("limit", ["20"])[0]),
                ))
            if path == "/read":
                return self._json(200, self._read(
                    q["self_project"][0], q["self"][0],
                    q["peer_project"][0], q["peer"][0],
                    int(q.get("limit", ["30"])[0]),
                    int(q.get("since", ["0"])[0]),
                ))
            if path == "/inbox":
                return self._json(200, self._inbox(
                    q["project"][0], q["name"][0],
                    q["instance"][0],
                    int(q.get("limit", ["500"])[0]),
                ))
            if path == "/message":
                return self._json(200, self._message(
                    q["project"][0], q["name"][0], int(q["id"][0]),
                ))
            if path == "/group/list":
                return self._json(200, self._group_list(
                    q.get("member_project", [None])[0],
                    q.get("member", [None])[0],
                ))
            if path == "/group/members":
                return self._json(200, self._group_members(
                    q["group_project"][0], q["group"][0],
                ))
            if path == "/group/charter":
                return self._json(200, self._group_charter_get(
                    q["project"][0], q["name"][0],
                ))
            if path == "/resolve":
                return self._json(200, self._resolve_candidates(q["name"][0]))
            return self._err(404, f"no such GET route: {path}")
        except KeyError as e:
            return self._err(400, f"missing param: {e}")
        except Exception as e:
            sys.stderr.write(traceback.format_exc())
            return self._err(500, f"internal error: {e}")

    def _ws_upgrade(self):
        """Handle WebSocket upgrade on /subscribe.

        Required headers: X-Meeting-Name, X-Meeting-Project, X-Meeting-Proto.
        """
        proto = self.headers.get("X-Meeting-Proto", "")
        if proto != "1":
            self._text(426, "Upgrade Required: X-Meeting-Proto must be 1")
            return

        expected_token = None
        try:
            with open(_CONFIG_PATH) as _f:
                expected_token = json.load(_f).get("auth_token") or None
        except Exception:
            pass
        if expected_token:
            auth_header = self.headers.get("Authorization", "")
            if auth_header != f"Bearer {expected_token}":
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

        name = self.headers.get("X-Meeting-Name", "").strip()
        if not name:
            self._err(400, "X-Meeting-Name required")
            return

        project = self.headers.get("X-Meeting-Project", "").strip()
        if not project:
            self._err(400, "X-Meeting-Project required")
            return
        mode = self.headers.get("X-Meeting-Mode", "delivery").strip().lower()
        if mode not in ("delivery", "notify"):
            self._err(400, "X-Meeting-Mode must be delivery or notify")
            return
        instance = self.headers.get("X-Meeting-Instance", "").strip()
        with db() as conn:
            current = conn.execute(
                "SELECT instance FROM sessions WHERE project=? AND name=?",
                (project, name),
            ).fetchone()
        current_instance = (
            (current["instance"] or "") if current is not None else ""
        )
        if mode == "notify" or current_instance:
            if not instance:
                self._err(
                    400,
                    "X-Meeting-Instance required for registered identity",
                )
                return
            if current is None or current_instance != instance:
                self._err(409, "registration instance is no longer current")
                return

        ws_key = self.headers.get("Sec-WebSocket-Key", "")
        if not ws_key:
            self._err(400, "Sec-WebSocket-Key required")
            return

        accept = base64.b64encode(
            hashlib.sha1((ws_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()

        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()

        self.connection.settimeout(None)

        now = int(time.time())
        with db() as conn:
            row = conn.execute(
                "SELECT cursor FROM read_cursors WHERE project=? AND member_name=?",
                (project, name),
            ).fetchone()
            if row is None and mode == "delivery":
                max_row = conn.execute(
                    "SELECT COALESCE(MAX(id), 0) AS m FROM messages"
                ).fetchone()
                cursor = max_row["m"]
                conn.execute(
                    "INSERT INTO read_cursors (project, member_name, cursor, updated_at) VALUES (?, ?, ?, ?)",
                    (project, name, cursor, now),
                )
            elif row is not None:
                cursor = row["cursor"]
            else:
                cursor = 0

            if mode == "delivery":
                # Legacy delivery subscribers may exist without an explicit
                # registration, so preserve their historical session behavior.
                conn.execute(
                    "INSERT INTO sessions (project, name, cwd, host, os, registered_at, last_seen, role)"
                    " VALUES (?, ?, NULL, NULL, NULL, ?, ?, 'worker')"
                    " ON CONFLICT(project, name) DO UPDATE SET"
                    "  last_seen=excluded.last_seen",
                    (project, name, str(int(now)), float(now)),
                )

        sub = Subscriber(
            project, name, self.connection, self.wfile, cursor, mode=mode
        )

        with _sub_lock:
            _subscribers.setdefault(sub.key, []).append(sub)

        try:
            if mode == "delivery":
                self._ws_send_backlog(sub, cursor)
            else:
                # The client may complete its first inbox fetch after the 101
                # response but before this subscriber is added to _subscribers.
                # An unconditional sync notification closes that handshake
                # window; later messages are covered by normal fanout.
                with sub.send_lock:
                    if not _ws_send_text(
                        sub,
                        json.dumps(
                            {"type": "notify", "reason": "subscribed"},
                            ensure_ascii=False,
                        ),
                    ):
                        return
            sys.stderr.write(
                f"[ws] {project}/{name} connected, mode={mode}, cursor={cursor}\n"
            )

            while True:
                try:
                    opcode, payload = _ws_read_frame(self.rfile)
                except Exception:
                    break

                if opcode == 0xA:  # pong
                    sub.last_pong = time.time()
                    try:
                        with db() as conn:
                            if mode == "notify":
                                conn.execute(
                                    "UPDATE sessions SET last_seen=?"
                                    " WHERE project=? AND name=? AND instance=?",
                                    (time.time(), project, name, instance),
                                )
                            else:
                                conn.execute(
                                    "UPDATE sessions SET last_seen=?"
                                    " WHERE project=? AND name=?",
                                    (time.time(), project, name),
                                )
                    except Exception:
                        pass
                elif opcode == 0x9:  # ping from client
                    try:
                        self.wfile.write(b"\x8A" + bytes([len(payload)]) + payload)
                        self.wfile.flush()
                    except Exception:
                        break
                elif opcode == 0x8:  # close
                    _ws_send_close(sub)
                    break
                else:
                    _ws_send_close(sub)
                    break
        finally:
            _ws_remove(sub)
            self.close_connection = True
            sys.stderr.write(f"[ws] {project}/{name} disconnected\n")

    def _ws_send_backlog(self, sub: "Subscriber", cursor: int):
        """Drain all messages with id > cursor to sub, then atomically flip state to live."""
        while True:
            with sub.send_lock:
                hwm = sub.high_water_mark

            with db() as conn:
                rows = conn.execute(
                    "SELECT id, sender_project, sender, recipient_project, recipient, ask FROM messages"
                    " WHERE id>?"
                    "  AND ("
                    "   (recipient_project=? AND recipient=?)"
                    "   OR EXISTS ("
                    "     SELECT 1 FROM group_members gm"
                    "     WHERE gm.member_project=? AND gm.member_name=?"
                    "       AND gm.group_project=messages.recipient_project"
                    "       AND gm.group_name=messages.recipient"
                    "       AND messages.id>gm.joined_after_message_id"
                    "   )"
                    "  )"
                    " ORDER BY id ASC LIMIT 500",
                    (hwm, sub.project, sub.name, sub.project, sub.name),
                ).fetchall()
                group_names: set[str] = set()
                if rows:
                    recipients = set((r["recipient_project"], r["recipient"]) for r in rows)
                    for (rproj, rcp) in recipients:
                        if conn.execute(
                            "SELECT 1 FROM groups WHERE project=? AND name=?",
                            (rproj, rcp),
                        ).fetchone():
                            group_names.add(rcp)

            if not rows:
                with sub.send_lock:
                    with db() as conn:
                        more = conn.execute(
                            "SELECT 1 FROM messages"
                            " WHERE id>?"
                            "  AND ("
                            "   (recipient_project=? AND recipient=?)"
                            "   OR EXISTS ("
                            "     SELECT 1 FROM group_members gm"
                            "     WHERE gm.member_project=? AND gm.member_name=?"
                            "       AND gm.group_project=messages.recipient_project"
                            "       AND gm.group_name=messages.recipient"
                            "       AND messages.id>gm.joined_after_message_id"
                            "   )"
                            "  )"
                            " LIMIT 1",
                            (sub.high_water_mark, sub.project, sub.name,
                             sub.project, sub.name),
                        ).fetchone()
                    if not more:
                        sub.state = "live"
                        hwm = sub.high_water_mark
                        caught_up_frame = json.dumps(
                            {"type": "caught_up", "cursor": hwm},
                            ensure_ascii=False,
                        )
                        try:
                            with db() as conn:
                                conn.execute(
                                    "INSERT INTO read_cursors (project, member_name, cursor, updated_at) VALUES (?, ?, ?, ?)"
                                    " ON CONFLICT(project, member_name) DO UPDATE SET"
                                    "  cursor=MAX(excluded.cursor, read_cursors.cursor),"
                                    "  updated_at=excluded.updated_at",
                                    (sub.project, sub.name, hwm, int(time.time())),
                                )
                        except Exception:
                            pass
                        _ws_send_text(sub, caught_up_frame)
                        return
                continue

            for row in rows:
                group_field = row["recipient"] if row["recipient"] in group_names else None
                frame = json.dumps({
                    "type": "msg",
                    "msg_id": row["id"],
                    "sender": row["sender"],
                    "sender_project": row["sender_project"],
                    "ask": row["ask"] or "",
                    "group": group_field,
                    "phase": "backlog",
                }, ensure_ascii=False)
                with sub.send_lock:
                    if row["id"] <= sub.high_water_mark:
                        continue
                    ok = _ws_send_text(sub, frame)
                    if ok:
                        sub.high_water_mark = row["id"]
                    else:
                        _ws_remove(sub)
                        return

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._check_auth(path):
            return
        try:
            body = self._read_json_body()
            if path == "/send":
                result = self._send(
                    body["self_project"], body["self"],
                    body["peer_project"], body["peer"],
                    body["body"],
                    body.get("kind", "回应"),
                    body.get("ask"),
                )
                return self._json(200, result)
            if path == "/register":
                return self._json(200, self._register(
                    body["project"], body["name"],
                    body.get("cwd"), body.get("force", False), body.get("role"),
                    body.get("host"), body.get("os"), body.get("instance"),
                    body.get("client_version"), body.get("legacy_cursor"),
                ))
            if path == "/ack":
                result = self._ack(
                    body["project"], body["name"], body["instance"],
                    int(body["expected_cursor"]), int(body["through"]),
                )
                return self._json(409 if result.get("error") else 200, result)
            if path == "/unregister":
                return self._json(200, self._unregister(
                    body["project"], body["name"], body.get("instance"),
                ))
            if path == "/prune":
                return self._json(200, self._prune(
                    float(body.get("older_than_days", 7)),
                    bool(body.get("include_referenced", False)),
                    bool(body.get("apply", False)),
                ))
            if path == "/rename":
                result = self._rename(body["project"], body["old"], body["new"])
                status = 400 if result.get("error") else 200
                return self._json(status, result)
            if path == "/group/create":
                result = self._group_create(
                    body["project"], body["name"],
                    body.get("members", []), body.get("creator"),
                )
                status = 400 if result.get("error") else 200
                return self._json(status, result)
            if path == "/group/add":
                result = self._group_add(
                    body["group_project"], body["group"],
                    body["member_project"], body["member"],
                )
                status = 400 if result.get("error") else 200
                return self._json(status, result)
            if path == "/group/remove":
                result = self._group_remove(
                    body["group_project"], body["group"],
                    body["member_project"], body["member"],
                )
                status = 400 if result.get("error") else 200
                return self._json(status, result)
            if path == "/group/rename":
                result = self._group_rename(body["project"], body["old"], body["new"])
                status = 400 if result.get("error") else 200
                return self._json(status, result)
            if path == "/group/charter":
                result = self._group_charter_set(
                    body["project"], body["name"], body.get("charter"),
                )
                status = 400 if result.get("error") else 200
                return self._json(status, result)
            return self._err(404, f"no such POST route: {path}")
        except KeyError as e:
            return self._err(400, f"missing param: {e}")
        except Exception as e:
            sys.stderr.write(traceback.format_exc())
            return self._err(500, f"internal error: {e}")

    def do_DELETE(self):
        path = urlparse(self.path).path
        if not self._check_auth(path):
            return
        q = self._query()
        try:
            if path == "/conversation":
                return self._json(200, self._delete(
                    q["self_project"][0], q["self"][0],
                    q["peer_project"][0], q["peer"][0],
                ))
            if path == "/group":
                result = self._group_purge(q["project"][0], q["name"][0])
                status = 400 if result.get("error") else 200
                return self._json(status, result)
            return self._err(404, f"no such DELETE route: {path}")
        except KeyError as e:
            return self._err(400, f"missing param: {e}")
        except Exception as e:
            sys.stderr.write(traceback.format_exc())
            return self._err(500, f"internal error: {e}")

    # --- business logic ---

    @staticmethod
    def _fmt_id(name: str, project: str) -> str:
        """Render the canonical identity; global identities retain the @* suffix."""
        return f"{name}@{project}"

    def _group_repository(self) -> SQLiteGroupRepository:
        return SQLiteGroupRepository(
            connect=db,
            format_identity=self._fmt_id,
        )

    def _session_repository(self) -> SQLiteSessionRepository:
        return SQLiteSessionRepository(
            connect=db,
            online_threshold=ONLINE_THRESHOLD,
        )

    def _conversation_repository(
        self,
    ) -> SQLiteConversationRepository:
        return SQLiteConversationRepository(
            connect=db,
            format_identity=self._fmt_id,
            fanout=_ws_fanout,
        )

    def _resolve_candidates(self, raw_name: str):
        return self._session_repository().resolve_candidates(raw_name)

    def _list(self):
        return self._session_repository().list_sessions()

    def _register(self, project: str, name: str, cwd, force: bool,
                  role=None, host=None, os_label=None, instance=None,
                  client_version=None, legacy_cursor=None):
        return self._session_repository().register(
            project,
            name,
            cwd,
            force,
            role=role,
            host=host,
            os_label=os_label,
            instance=instance,
            client_version=client_version,
            legacy_cursor=legacy_cursor,
        )

    def _ack(self, project: str, name: str, instance: str,
             expected_cursor: int, through: int):
        return self._session_repository().acknowledge(
            project,
            name,
            instance,
            expected_cursor,
            through,
        )

    def _prune(self, older_than_days: float, include_referenced: bool, apply: bool):
        return self._session_repository().prune(
            older_than_days,
            include_referenced,
            apply,
        )

    def _unregister(self, project: str, name: str, instance=None):
        return self._session_repository().unregister(
            project,
            name,
            instance,
        )

    def _turn(self, self_project: str, self_name: str, peer_project: str, peer_name: str):
        return self._conversation_repository().current_turn(
            self_project,
            self_name,
            peer_project,
            peer_name,
        )

    def _show(self, self_project: str, self_name: str, peer_project: str, peer_name: str, limit: int):
        return self._conversation_repository().render_conversation(
            self_project,
            self_name,
            peer_project,
            peer_name,
            limit,
        )

    def _read(self, self_project: str, self_name: str, peer_project: str, peer_name: str,
              limit: int, since: int):
        return self._conversation_repository().read_conversation(
            self_project,
            self_name,
            peer_project,
            peer_name,
            limit,
            since,
        )

    def _inbox(self, project: str, name: str, instance: str, limit: int):
        return self._conversation_repository().read_inbox(
            project,
            name,
            instance,
            limit,
        )

    def _message(self, project: str, name: str, message_id: int):
        return self._conversation_repository().read_message(
            project,
            name,
            message_id,
        )

    def _send(self, self_project: str, self_name: str, peer_project: str, peer_name: str,
              body: str, kind: str, ask):
        return self._conversation_repository().send_message(
            self_project,
            self_name,
            peer_project,
            peer_name,
            body,
            kind,
            ask,
        )

    def _group_create(self, project: str, name: str, members: list, creator):
        return self._group_repository().create(
            project,
            name,
            members,
            creator,
        )

    def _group_add(self, group_project: str, group: str, member_project: str, member: str):
        return self._group_repository().add(
            group_project,
            group,
            member_project,
            member,
        )

    def _group_remove(self, group_project: str, group: str, member_project: str, member: str):
        return self._group_repository().remove(
            group_project,
            group,
            member_project,
            member,
        )

    def _group_rename(self, project: str, old: str, new: str):
        return self._group_repository().rename(project, old, new)

    def _group_list(self, member_project, member):
        return self._group_repository().list_groups(member_project, member)

    def _group_members(self, group_project: str, group: str):
        return self._group_repository().members(group_project, group)

    def _group_charter_get(self, project: str, name: str):
        return self._group_repository().get_charter(project, name)

    def _group_charter_set(self, project: str, name: str, charter):
        return self._group_repository().set_charter(
            project,
            name,
            charter,
        )

    def _group_purge(self, project: str, name: str):
        return self._group_repository().purge(project, name)

    def _rename(self, project: str, old: str, new: str):
        return self._session_repository().rename(project, old, new)

    def _delete(self, self_project: str, self_name: str, peer_project: str, peer_name: str):
        return self._conversation_repository().delete_conversation(
            self_project,
            self_name,
            peer_project,
            peer_name,
        )


# ---------- main ----------

def main():
    p = argparse.ArgumentParser(prog="am-msgd")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--bind", default="0.0.0.0")
    p.add_argument("--no-mdns", action="store_true", help="skip mDNS publish (for testing)")
    args = p.parse_args()

    if not os.path.exists(DB_PATH):
        print(f"DB not found at {DB_PATH}. Run `meeting init` first.", file=sys.stderr)
        sys.exit(1)

    prepare_message_database(DB_PATH)

    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((args.bind, args.port), Handler)

    zc = info = None
    if not args.no_mdns:
        zc, info = publish_message_hub(args.port, _plugin_version)

    def shutdown(sig, _frame):
        print(f"\n[shutdown] received signal {sig}, unpublishing mDNS + stopping HTTP", flush=True)
        def _do():
            try:
                if zc and info:
                    zc.unregister_service(info)
                    zc.close()
            except Exception:
                pass
            server.shutdown()
            sys.exit(0)
        threading.Thread(target=_do, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    def _watchdog(port: int):
        failures = 0
        time.sleep(15)
        while True:
            try:
                s = socket.create_connection(("127.0.0.1", port), timeout=5)
                s.sendall(b"GET /health HTTP/1.0\r\nHost: localhost\r\n\r\n")
                s.recv(256)
                s.close()
                failures = 0
            except Exception:
                failures += 1
                print(f"[watchdog] health check failed ({failures}/3)", flush=True)
                if failures >= 3:
                    print("[watchdog] central am-msgd unresponsive, forcing exit", flush=True)
                    os._exit(1)
            time.sleep(10)

    threading.Thread(target=_watchdog, args=(args.port,), daemon=True).start()
    threading.Thread(target=_ws_heartbeat_loop, daemon=True).start()

    print(f"[http] listening on {args.bind}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
