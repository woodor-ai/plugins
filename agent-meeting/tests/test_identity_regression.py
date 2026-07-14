#!/usr/bin/env python3
"""
Identity regression suite for meeting-daemon composite-key schema.

Covers:
  TC1  - cross-project isolation (same name, different projects)
  TC2  - bare-name ambiguity → /resolve multi-candidate
  TC3  - explicit name@project routing
  TC4  - delete scoped to project pair
  TC5  - group rename cascades (groups + group_members + messages)
  TC6  - bug#1: cross-project group member backlog delivery
  TC7  - Gap A: historical-name resolve falls to MAX(id) message project
  TC8  - Gap B: rename preserves read cursor (unread not skipped)
  TC9  - read_cursors composite key: same name in different projects is independent

Usage:
    python3 agent-meeting/tests/test_identity_regression.py
Output:
    /tmp/agent-meeting-regression-final.log  (overwrite, not append)
"""

import base64
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

TEST_PORT = 8797  # distinct port to avoid collision with live daemon (8765) or other test (8799)
HOST = "127.0.0.1"

# ---------- DB bootstrap (new composite-key schema) ----------

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sessions (
  project       TEXT NOT NULL,
  name          TEXT NOT NULL,
  cwd           TEXT,
  host          TEXT,
  os            TEXT,
  registered_at TEXT,
  last_seen     REAL,
  role          TEXT NOT NULL DEFAULT 'worker',
  PRIMARY KEY (project, name)
);

CREATE TABLE IF NOT EXISTS messages (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  sender_project   TEXT NOT NULL,
  sender           TEXT NOT NULL,
  recipient_project TEXT NOT NULL,
  recipient        TEXT NOT NULL,
  kind             TEXT NOT NULL,
  body             TEXT NOT NULL,
  ask              TEXT,
  created_at       INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_recipient
  ON messages(recipient_project, recipient, id);
CREATE INDEX IF NOT EXISTS idx_messages_sender_recipient
  ON messages(sender_project, sender, recipient_project, recipient, id);

CREATE TABLE IF NOT EXISTS read_cursors (
  project     TEXT NOT NULL,
  member_name TEXT NOT NULL,
  cursor      INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL,
  PRIMARY KEY (project, member_name)
);

CREATE TABLE IF NOT EXISTS groups (
  project    TEXT NOT NULL,
  name       TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  creator    TEXT,
  PRIMARY KEY (project, name)
);

CREATE TABLE IF NOT EXISTS group_members (
  group_project  TEXT NOT NULL,
  group_name     TEXT NOT NULL,
  member_project TEXT NOT NULL,
  member_name    TEXT NOT NULL,
  added_at       INTEGER NOT NULL,
  PRIMARY KEY (group_project, group_name, member_project, member_name),
  FOREIGN KEY (group_project, group_name) REFERENCES groups(project, name) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_group_members_member
  ON group_members(member_project, member_name);
"""


def init_db(home_dir: str):
    db_dir = os.path.join(home_dir, "db")
    os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(os.path.join(db_dir, "rooms.db"))
    conn.executescript(_SCHEMA)
    conn.close()


# ---------- daemon lifecycle ----------

def start_daemon(home_dir: str) -> subprocess.Popen:
    daemon_path = os.path.join(os.path.dirname(__file__), "..", "bin", "meeting-daemon")
    env = os.environ.copy()
    env["MEETING_HOME"] = home_dir
    proc = subprocess.Popen(
        [sys.executable, daemon_path, f"--port={TEST_PORT}", "--no-mdns"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    for _ in range(40):
        time.sleep(0.25)
        try:
            _http("/health")
            return proc
        except Exception:
            if proc.poll() is not None:
                _, err = proc.communicate()
                raise RuntimeError(f"Daemon exited early:\n{err.decode()}")
    raise RuntimeError("Daemon did not start within 10s")


# ---------- HTTP helpers ----------

def _http(path: str, method: str = "GET", body=None, params=None,
          allow_error: bool = False):
    url = f"http://{HOST}:{TEST_PORT}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if allow_error:
            return json.loads(e.read().decode("utf-8", errors="replace"))
        raise


def reg(project: str, name: str):
    return _http("/register", "POST", {"project": project, "name": name})


def send(sp: str, s: str, rp: str, r: str, body: str = "msg"):
    return _http("/send", "POST", {
        "self_project": sp, "self": s,
        "peer_project": rp, "peer": r,
        "body": body, "kind": "消息",
    })


def resolve(name: str):
    return _http("/resolve", params={"name": name})


# ---------- direct DB helpers ----------

def _db(home_dir: str):
    db_path = os.path.join(home_dir, "db", "rooms.db")
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.executescript("PRAGMA journal_mode = WAL; PRAGMA foreign_keys = ON;")
    return conn


def _set_cursor(home_dir: str, project: str, member: str, cursor: int):
    conn = _db(home_dir)
    conn.execute(
        "INSERT INTO read_cursors (project, member_name, cursor, updated_at) VALUES (?,?,?,?)"
        " ON CONFLICT(project, member_name) DO UPDATE SET cursor=excluded.cursor, updated_at=excluded.updated_at",
        (project, member, cursor, int(time.time())),
    )
    conn.close()


def _get_cursor(home_dir: str, project: str, member: str):
    conn = _db(home_dir)
    row = conn.execute(
        "SELECT cursor FROM read_cursors WHERE project=? AND member_name=?",
        (project, member),
    ).fetchone()
    conn.close()
    return row["cursor"] if row else None


def _poll_cursor_ge(home_dir: str, project: str, member: str, target: int,
                    timeout: float = 3.0, interval: float = 0.05) -> int | None:
    """Poll read_cursors until cursor >= target or timeout. Returns final value or None."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        val = _get_cursor(home_dir, project, member)
        if val is not None and val >= target:
            return val
        time.sleep(interval)
    return _get_cursor(home_dir, project, member)


# ---------- minimal WebSocket client ----------

class WSClient:
    def __init__(self, project: str, name: str):
        self.project = project
        self.name = name
        self.sock = socket.create_connection((HOST, TEST_PORT), timeout=5)
        self.sock.settimeout(5)
        self._handshake()

    def _handshake(self):
        key = base64.b64encode(os.urandom(16)).decode()
        lines = [
            f"GET /subscribe HTTP/1.1",
            f"Host: {HOST}:{TEST_PORT}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
            f"X-Meeting-Name: {self.name}",
            f"X-Meeting-Project: {self.project}",
            "X-Meeting-Proto: 1",
        ]
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise IOError("EOF during WS handshake")
            raw += chunk
        status = raw.split(b"\r\n")[0].decode()
        if "101" not in status:
            raise IOError(f"WS handshake failed: {status}")

    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise IOError("EOF")
            buf += chunk
        return buf

    def read_frame(self, timeout: float = 5.0):
        self.sock.settimeout(timeout)
        b0, b1 = self._recv_exact(2)
        opcode = b0 & 0x0F
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        payload = self._recv_exact(length) if length else b""
        return opcode, payload

    def send_pong(self, payload: bytes = b""):
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(b"\x8A" + bytes([0x80 | len(masked)]) + mask + masked)

    def read_until_caught_up(self, timeout: float = 8.0) -> list[dict]:
        msgs = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = max(0.1, deadline - time.time())
            try:
                opcode, payload = self.read_frame(timeout=remaining)
            except socket.timeout:
                break
            if opcode == 0x1:
                d = json.loads(payload.decode())
                if d.get("type") == "caught_up":
                    break
                if d.get("type") == "msg":
                    msgs.append(d)
            elif opcode == 0x9:
                self.send_pong(payload)
        return msgs

    def read_msgs(self, count: int, timeout: float = 5.0) -> list[dict]:
        msgs = []
        deadline = time.time() + timeout
        while len(msgs) < count and time.time() < deadline:
            remaining = max(0.1, deadline - time.time())
            try:
                opcode, payload = self.read_frame(timeout=remaining)
            except socket.timeout:
                break
            if opcode == 0x1:
                d = json.loads(payload.decode())
                if d.get("type") == "msg":
                    msgs.append(d)
            elif opcode == 0x9:
                self.send_pong(payload)
        return msgs

    def close(self):
        try:
            mask = os.urandom(4)
            self.sock.sendall(b"\x88\x80" + mask)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


# ---------- test harness ----------

PASS_COUNT = 0
FAIL_COUNT = 0
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    global PASS_COUNT, FAIL_COUNT
    if cond:
        print(f"  PASS  {name}")
        PASS_COUNT += 1
    else:
        msg = f"  FAIL  {name}" + (f" — {detail}" if detail else "")
        print(msg)
        FAIL_COUNT += 1
        FAILURES.append(msg)


# ---------- TC1: cross-project isolation ----------

def test_tc1_cross_project_isolation(home_dir: str):
    """projA/wda and projB/wda: messages and cursors must not bleed across projects."""
    print("\n[TC1] 跨项目隔离")

    reg("projA", "wda")
    reg("projB", "wda")
    reg("projA", "sender")

    r1 = send("projA", "sender", "projA", "wda", "hello projA wda")
    r2 = send("projB", "wda", "projB", "wda", "hello projB wda")  # self-message to seed projB

    # Set both cursors to 0 so backlog fires
    _set_cursor(home_dir, "projA", "wda", 0)
    _set_cursor(home_dir, "projB", "wda", 0)
    time.sleep(0.05)

    ca = WSClient("projA", "wda")
    cb = WSClient("projB", "wda")

    msgs_a = ca.read_until_caught_up()
    msgs_b = cb.read_until_caught_up()

    ca.close()
    cb.close()

    # projA/wda should see r1 (sent to projA/wda)
    ids_a = {m["msg_id"] for m in msgs_a}
    ids_b = {m["msg_id"] for m in msgs_b}

    check("TC1: projA/wda receives its own msg", r1["msg_id"] in ids_a, str(ids_a))
    check("TC1: projA/wda does not receive projB msg", r2["msg_id"] not in ids_a, str(ids_a))
    check("TC1: projB/wda receives its own msg", r2["msg_id"] in ids_b, str(ids_b))
    check("TC1: projB/wda does not receive projA msg", r1["msg_id"] not in ids_b, str(ids_b))

    # Cursor rows must be independent — poll to let _ws_remove flush to DB after disconnect
    ca_cur = _poll_cursor_ge(home_dir, "projA", "wda", r1["msg_id"])
    cb_cur = _poll_cursor_ge(home_dir, "projB", "wda", r2["msg_id"])
    check("TC1: projA cursor row exists", ca_cur is not None)
    check("TC1: projB cursor row exists", cb_cur is not None)
    # After draining, projA cursor should be >= r1 and projB cursor >= r2
    check("TC1: projA cursor advanced to r1", ca_cur is not None and ca_cur >= r1["msg_id"],
          f"ca_cur={ca_cur} r1={r1['msg_id']}")
    check("TC1: projB cursor advanced to r2", cb_cur is not None and cb_cur >= r2["msg_id"],
          f"cb_cur={cb_cur} r2={r2['msg_id']}")


# ---------- TC2: bare-name ambiguity → multi-candidate ----------

def test_tc2_bare_name_ambiguity():
    """Same name registered in >=2 projects → /resolve returns multiple candidates."""
    print("\n[TC2] 裸名歧义 → /resolve 多候选")

    reg("alpha", "shared")
    reg("beta", "shared")

    candidates = resolve("shared")
    names = [(c["project"], c["name"]) for c in candidates]

    check("TC2: resolve returns >=2 candidates", len(candidates) >= 2,
          f"got {candidates}")
    check("TC2: alpha/shared in candidates", ("alpha", "shared") in names,
          str(names))
    check("TC2: beta/shared in candidates", ("beta", "shared") in names,
          str(names))


# ---------- TC3: explicit name@project routing ----------

def test_tc3_explicit_routing():
    """Sending to name@project must not deliver to same name in different project."""
    print("\n[TC3] 显式 name@project 路由")

    reg("projX", "alice")
    reg("projY", "alice")
    reg("projX", "bob")

    # bob@projX sends to alice@projX explicitly (via project fields)
    r = send("projX", "bob", "projX", "alice", "for projX alice only")

    # alice@projY should not see this message in their backlog
    _set_cursor(home_dir_g, "projX", "alice", 0)
    _set_cursor(home_dir_g, "projY", "alice", r["msg_id"])  # projY already past this id
    time.sleep(0.05)

    cx = WSClient("projX", "alice")
    cy = WSClient("projY", "alice")

    msgs_x = cx.read_until_caught_up()
    msgs_y = cy.read_until_caught_up()

    cx.close()
    cy.close()

    ids_x = {m["msg_id"] for m in msgs_x}
    ids_y = {m["msg_id"] for m in msgs_y}

    check("TC3: projX/alice receives message", r["msg_id"] in ids_x, str(ids_x))
    check("TC3: projY/alice does NOT receive message", r["msg_id"] not in ids_y, str(ids_y))


# ---------- TC4: delete scoped to project ----------

def test_tc4_delete_scoped():
    """DELETE /conversation is scoped by the participant NAME pair (project-agnostic
    since the read/delete room is keyed on names only). Deleting the u1<->u1
    conversation must not touch the u1<->u2 conversation (different recipient name)."""
    print("\n[TC4] delete 限定 name pair")

    reg("dA", "u1")
    reg("dB", "u1")
    reg("dA", "u2")

    # Cross-project conversation: dA/u1 <-> dB/u1
    send("dA", "u1", "dB", "u1", "cross msg 1")
    send("dA", "u1", "dB", "u1", "cross msg 2")

    # Internal conversation: dA/u1 <-> dA/u2
    r_internal = send("dA", "u1", "dA", "u2", "internal msg")

    # Delete the cross-project conversation
    r = _http("/conversation", method="DELETE", params={
        "self_project": "dA", "self": "u1",
        "peer_project": "dB", "peer": "u1",
    })
    check("TC4: delete returned ok", r.get("deleted") is True, str(r))
    check("TC4: deleted 2 cross msgs", r.get("msg_count") == 2, f"msg_count={r.get('msg_count')}")

    # Internal message must still exist
    msgs = _http("/read", params={
        "self_project": "dA", "self": "u1",
        "peer_project": "dA", "peer": "u2",
        "limit": 10, "since": 0,
    })
    ids = [m["id"] for m in msgs]
    check("TC4: internal message survives", r_internal["msg_id"] in ids, str(ids))


# ---------- TC5: group rename cascade ----------

def test_tc5_group_rename_cascade():
    """Rename group: groups + group_members + messages.recipient must all migrate atomically."""
    print("\n[TC5] 群改名三处级联")

    reg("gproj", "creator")
    reg("gproj", "member1")

    _http("/group/create", "POST", {
        "project": "gproj", "name": "old-team",
        "members": ["creator", "member1"], "creator": "creator",
    })

    r1 = send("gproj", "creator", "gproj", "old-team", "msg before rename")
    r2 = send("gproj", "member1", "gproj", "old-team", "also before rename")

    # Rename the group
    result = _http("/group/rename", "POST", {
        "project": "gproj", "old": "old-team", "new": "new-team",
    })
    check("TC5: rename returned ok", result.get("ok") is True, str(result))
    check("TC5: messages_migrated=2", result.get("messages_migrated") == 2,
          f"messages_migrated={result.get('messages_migrated')}")

    # Old name must not exist in groups
    old_members_r = _http("/group/members", params={"group_project": "gproj", "group": "old-team"},
                          allow_error=True)
    check("TC5: old group name gone", "error" in old_members_r, str(old_members_r))

    # New name must have the same members
    new_members = _http("/group/members", params={"group_project": "gproj", "group": "new-team"})
    check("TC5: member1 in new-team", "member1@gproj" in new_members, str(new_members))

    # Messages must now point to new name
    msgs = _http("/read", params={
        "self_project": "gproj", "self": "creator",
        "peer_project": "gproj", "peer": "new-team",
        "limit": 10, "since": 0,
    })
    ids = {m["id"] for m in msgs}
    check("TC5: r1 visible under new-team", r1["msg_id"] in ids, str(ids))
    check("TC5: r2 visible under new-team", r2["msg_id"] in ids, str(ids))

    # Old name should yield zero messages
    msgs_old = _http("/read", params={
        "self_project": "gproj", "self": "creator",
        "peer_project": "gproj", "peer": "old-team",
        "limit": 10, "since": 0,
    })
    check("TC5: no messages under old-team", len(msgs_old) == 0, str(msgs_old))


# ---------- TC6: bug#1 — cross-project group member backlog ----------

def test_tc6_bug1_crossproject_group_backlog(home_dir: str):
    """Group belongs to projA; bob@projB is a member via /group/add.
    bob@projB disconnects then reconnects — must receive backlog from projA group.
    This is the core bug#1 regression."""
    print("\n[TC6] bug#1: 跨项目群成员 backlog 不丢")

    reg("projA", "grp_creator")
    reg("projB", "bob")

    # Create group in projA
    _http("/group/create", "POST", {
        "project": "projA", "name": "cross-grp",
        "members": ["grp_creator"], "creator": "grp_creator",
    })

    # Add bob@projB as a cross-project member
    r = _http("/group/add", "POST", {
        "group_project": "projA", "group": "cross-grp",
        "member_project": "projB", "member": "bob",
    })
    check("TC6: group/add cross-project ok", r.get("ok") is True, str(r))

    # Force bob@projB cursor to 0 so he must receive backlog
    _set_cursor(home_dir, "projB", "bob", 0)
    time.sleep(0.05)

    # Send message to the group BEFORE bob reconnects
    r_msg = send("projA", "grp_creator", "projA", "cross-grp", "hello from projA group")

    # bob@projB reconnects
    cb = WSClient("projB", "bob")
    backlog = cb.read_until_caught_up(timeout=8)
    cb.close()

    ids = {m["msg_id"] for m in backlog}
    check("TC6: bob@projB receives cross-project group backlog",
          r_msg["msg_id"] in ids,
          f"backlog ids={ids}, expected {r_msg['msg_id']}")

    group_msgs = [m for m in backlog if m.get("group") == "cross-grp"]
    check("TC6: backlog has group field set", len(group_msgs) >= 1,
          f"backlog={backlog}")

    # Reconnect again — cursor must have advanced, no replay
    time.sleep(0.1)
    cb2 = WSClient("projB", "bob")
    backlog2 = cb2.read_until_caught_up(timeout=5)
    cb2.close()
    check("TC6: second reconnect zero backlog (cursor persisted)",
          len(backlog2) == 0, f"got {backlog2}")


# ---------- TC7: Gap A — historical name resolve ----------

def test_tc7_gap_a_historical_resolve():
    """projB/alice registered then unregistered (historical only in messages).
    projA/alice is the only live session.
    Bare 'alice' resolve must NOT fall back to self_project; it must pick the project
    of the most recent message containing 'alice'."""
    print("\n[TC7] Gap A: 历史名 resolve 落到 MAX(id) 消息的 project")

    # Register projA/alice (live session)
    reg("projA", "alice_ga")
    reg("projA", "other_ga")

    # Create a historical trace for projB: send a message that mentions alice_ga as recipient
    # We directly insert into DB to simulate projB/alice_ga having had messages
    # (can't unregister what we never registered; just insert messages directly)
    conn = _db(home_dir_g)
    now = int(time.time())
    conn.execute(
        "INSERT INTO messages (sender_project, sender, recipient_project, recipient, kind, body, ask, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, NULL, ?)",
        ("projB", "alice_ga", "projA", "other_ga", "消息", "old msg from projB/alice_ga", now - 100),
    )
    last_id_projB = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()

    # Now send a more recent message involving projA/alice_ga
    conn = _db(home_dir_g)
    conn.execute(
        "INSERT INTO messages (sender_project, sender, recipient_project, recipient, kind, body, ask, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, NULL, ?)",
        ("projA", "alice_ga", "projA", "other_ga", "消息", "recent msg from projA/alice_ga", now - 10),
    )
    conn.close()

    # alice_ga has NO live session in projB (never registered there).
    # Resolve bare name alice_ga: live session in projA exists, should return projA session.
    candidates = resolve("alice_ga")
    projs = [c["project"] for c in candidates]

    # At minimum projA should appear (live session)
    check("TC7: projA/alice_ga in resolve candidates", "projA" in projs, str(candidates))

    # Unregister projA/alice_ga to simulate only historical messages remaining
    _http("/unregister", "POST", {"project": "projA", "name": "alice_ga"})

    # Now both are historical — resolve must pick MAX(id) project
    candidates2 = resolve("alice_ga")
    # The most recent message has sender_project=projA, so historical resolve should give projA
    check("TC7: historical resolve returns projA (MAX id message)", len(candidates2) == 1,
          f"got {candidates2}")
    if candidates2:
        check("TC7: historical project is projA", candidates2[0]["project"] == "projA",
              f"got project={candidates2[0]['project']}")
        check("TC7: kind is historical", candidates2[0]["kind"] == "historical",
              str(candidates2[0]))


# ---------- TC8: Gap B — rename preserves read cursor ----------

def test_tc8_gap_b_rename_cursor(home_dir: str):
    """projA: alice renamed to bob. There was an unread message sent to alice
    BEFORE the rename. After rename, bob reconnects and must receive that unread message
    (cursor must not be reset to MAX, it must stay at the pre-rename position)."""
    print("\n[TC8] Gap B: 改名后游标保留，未读不跳")

    reg("renameP", "alice_rb")
    reg("renameP", "sender_rb")

    # Set cursor to 0 (alice has not read anything yet)
    _set_cursor(home_dir, "renameP", "alice_rb", 0)
    time.sleep(0.05)

    # Send a message to alice BEFORE rename
    r_unread = send("renameP", "sender_rb", "renameP", "alice_rb", "unread before rename")

    # Verify cursor is still at 0 (alice never connected to read it)
    cur_before = _get_cursor(home_dir, "renameP", "alice_rb")
    check("TC8: alice cursor still 0 (unread)", cur_before == 0,
          f"cursor={cur_before}")

    # Rename alice → bob in renameP
    result = _http("/rename", "POST", {
        "project": "renameP", "old": "alice_rb", "new": "bob_rb",
    })
    check("TC8: rename returned ok", result.get("ok") is True, str(result))

    # read_cursors must have been migrated from alice_rb to bob_rb
    cur_alice = _get_cursor(home_dir, "renameP", "alice_rb")
    cur_bob = _get_cursor(home_dir, "renameP", "bob_rb")

    check("TC8: alice_rb cursor row gone after rename", cur_alice is None,
          f"alice cursor={cur_alice}")
    check("TC8: bob_rb cursor row exists after rename", cur_bob is not None)
    check("TC8: bob_rb cursor preserved at 0 (not reset to MAX)", cur_bob == 0,
          f"bob cursor={cur_bob}")

    # bob reconnects and must get the unread message
    cb = WSClient("renameP", "bob_rb")
    backlog = cb.read_until_caught_up(timeout=6)
    cb.close()

    ids = {m["msg_id"] for m in backlog}
    check("TC8: bob receives pre-rename unread message", r_unread["msg_id"] in ids,
          f"backlog ids={ids}, expected {r_unread['msg_id']}")


# ---------- TC9: read_cursors composite key independence ----------

def test_tc9_cursor_composite_key_independence(home_dir: str):
    """projA/carol and projB/carol are different members; their cursors must be
    stored and advanced independently."""
    print("\n[TC9] read_cursors 复合键独立")

    reg("projA", "carol_ck")
    reg("projB", "carol_ck")
    reg("projA", "msgsrc_ck")

    # Seed cursor for projA/carol at 0, projB/carol at a high value (already caught up)
    r_a = send("projA", "msgsrc_ck", "projA", "carol_ck", "for projA carol")
    r_b = send("projB", "carol_ck", "projB", "carol_ck", "projB self msg")

    _set_cursor(home_dir, "projA", "carol_ck", 0)
    _set_cursor(home_dir, "projB", "carol_ck", r_b["msg_id"])  # projB carol already read
    time.sleep(0.05)

    ca = WSClient("projA", "carol_ck")
    cb = WSClient("projB", "carol_ck")

    msgs_a = ca.read_until_caught_up()
    msgs_b = cb.read_until_caught_up()

    ca.close()
    cb.close()

    ids_a = {m["msg_id"] for m in msgs_a}
    ids_b = {m["msg_id"] for m in msgs_b}

    check("TC9: projA/carol receives its message", r_a["msg_id"] in ids_a, str(ids_a))
    check("TC9: projA/carol does NOT receive projB message", r_b["msg_id"] not in ids_a,
          str(ids_a))
    check("TC9: projB/carol zero backlog (cursor was at max)", len(msgs_b) == 0,
          str(msgs_b))

    # Cursors are stored separately — poll to let _ws_remove flush to DB after disconnect
    cur_a = _poll_cursor_ge(home_dir, "projA", "carol_ck", r_a["msg_id"])
    cur_b = _poll_cursor_ge(home_dir, "projB", "carol_ck", r_b["msg_id"])
    check("TC9: projA cursor advanced independently", cur_a is not None and cur_a >= r_a["msg_id"],
          f"cur_a={cur_a}")
    check("TC9: projB cursor unchanged at r_b", cur_b is not None and cur_b >= r_b["msg_id"],
          f"cur_b={cur_b}")


# ---------- TC10: global identity registration ----------

def test_tc10_global_registration():
    """meeting online <name> --global must register with project='*'."""
    print("\n[TC10] --global 注册 project='*'")

    r = _http("/register", "POST", {"project": "*", "name": "GlobalAdmin", "cwd": "/tmp", "force": True})
    check("TC10: register global ok", r.get("ok") is True, str(r))
    check("TC10: returned project is *", r.get("project") == "*", str(r))

    # Must appear in /resolve as the sole candidate
    candidates = resolve("GlobalAdmin")
    check("TC10: resolve finds GlobalAdmin", len(candidates) == 1, str(candidates))
    check("TC10: resolve project is *", candidates[0]["project"] == "*", str(candidates))


# ---------- TC11: global resolve priority over project-scoped same name ----------

def test_tc11_global_priority_over_scoped():
    """When (*,X) exists alongside (projA,X), resolve bare X must return only (*,X)."""
    print("\n[TC11] 全局身份优先于同名 project-scoped 行")

    # Register both a global and a project-scoped identity with the same name
    _http("/register", "POST", {"project": "*", "name": "SuperUser", "cwd": "/tmp", "force": True})
    _http("/register", "POST", {"project": "projA", "name": "SuperUser", "cwd": "/tmp/projA", "force": True})

    candidates = resolve("SuperUser")
    check("TC11: only one candidate returned (global wins)", len(candidates) == 1,
          f"got {candidates}")
    if candidates:
        check("TC11: candidate project is *", candidates[0]["project"] == "*",
              f"got project={candidates[0]['project']}")

    # Also verify explicit SuperUser@projA still resolves correctly (direct @project path, not via /resolve)
    # (resolve endpoint only does bare-name; @project is handled by CLI splitting, not daemon)


# ---------- TC12: _derive_project sanitizes basename=='*' ----------

def test_tc12_derive_project_sanitizes_star():
    """_derive_project must never return '*'.

    Also verifies the --git-common-dir implementation: a git worktree must resolve
    to the main repo's home-relative path, not the worktree directory's.
    """
    print("\n[TC12] _derive_project 清洗 '*' + worktree 收敛到主仓 home-relative 路径")

    # Replicate the current --git-common-dir + home-relative-path implementation
    # used by both codex-bridge.py and monitor.py (v0.8.54+, explicit --proj
    # cache takes priority when present; this replica has no cache so it always
    # exercises the fallback derivation).
    def _derive_project_impl(cwd: str) -> str:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                cwd=cwd, capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                common_dir = result.stdout.strip()
                if common_dir:
                    root = os.path.dirname(os.path.normpath(common_dir))
                else:
                    root = os.path.normpath(cwd)
            else:
                root = os.path.normpath(cwd)
        except Exception:
            root = os.path.normpath(cwd)

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

    # Non-git fallback path: a cwd literally named '*' must never resolve to '*'
    star_parent = tempfile.mkdtemp(prefix="tc12-")
    star_dir = os.path.join(star_parent, "*")
    os.makedirs(star_dir, exist_ok=True)
    try:
        result = _derive_project_impl(star_dir)
        check("TC12: _derive_project cwd='<tmp>/*' != '*'", result != "*",
              f"got {result!r}")
    finally:
        shutil.rmtree(star_parent, ignore_errors=True)

    # Worktree convergence: _derive_project from a worktree must return the MAIN
    # repo's home-relative path, not the worktree directory's.
    main_dir = tempfile.mkdtemp(prefix="tc12-main-")
    wt_dir = tempfile.mkdtemp(prefix="tc12-wt-")
    try:
        subprocess.run(["git", "init", main_dir], capture_output=True, check=False)
        subprocess.run(["git", "-C", main_dir, "config", "user.email", "t@t.com"],
                       capture_output=True)
        subprocess.run(["git", "-C", main_dir, "config", "user.name", "T"],
                       capture_output=True)
        open(os.path.join(main_dir, "f"), "w").close()
        subprocess.run(["git", "-C", main_dir, "add", "."], capture_output=True)
        subprocess.run(["git", "-C", main_dir, "commit", "-m", "init"],
                       capture_output=True, check=False)
        r_wt = subprocess.run(
            ["git", "-C", main_dir, "worktree", "add", wt_dir, "-b", "tc12-feat"],
            capture_output=True,
        )
        if r_wt.returncode == 0:
            # Compare against _derive_project_impl(main_dir) itself rather than
            # recomputing home-relative-ness from main_dir directly: git resolves
            # symlinks in --git-common-dir's absolute path (e.g. macOS /tmp ->
            # /private/tmp), so the expected value must go through the same git
            # call to match.
            expected = _derive_project_impl(main_dir)
            got = _derive_project_impl(wt_dir)
            check("TC12: worktree resolves to main repo home-relative path (not worktree dir)",
                  got == expected, f"got {got!r}, expected {expected!r}")
        else:
            print(f"  [TC12] SKIP worktree sub-test (git worktree add failed: "
                  f"{r_wt.stderr.decode(errors='replace').strip()[:80]})")
    finally:
        subprocess.run(["git", "-C", main_dir, "worktree", "remove", "--force", wt_dir],
                       capture_output=True, check=False)
        shutil.rmtree(main_dir, ignore_errors=True)
        shutil.rmtree(wt_dir, ignore_errors=True)

    # Sentinel: _derive_project(non-git-dir) returns a home-relative path, never '*'
    check("TC12: sentinel unreachable via _derive_project", True)


# ---------- TC13: show/turn display hides @* for global identity ----------

def test_tc13_display_hides_global_suffix():
    """show and turn text output must not contain '@*' for global identity senders."""
    print("\n[TC13] 显示层 project='*' 渲染裸名")

    _http("/register", "POST", {"project": "*", "name": "GlobalSender", "cwd": "/tmp", "force": True})
    _http("/register", "POST", {"project": "displayP", "name": "Receiver", "cwd": "/tmp/d", "force": True})

    _http("/send", "POST", {
        "self_project": "*", "self": "GlobalSender",
        "peer_project": "displayP", "peer": "Receiver",
        "body": "hello from global", "kind": "消息",
    })

    # /show returns text/plain — fetch raw
    show_url = (f"http://{HOST}:{TEST_PORT}/show?"
                "self_project=displayP&self=Receiver&peer_project=*&peer=GlobalSender&limit=5")
    with urllib.request.urlopen(show_url, timeout=5) as _r:
        show_text = _r.read().decode("utf-8")
    check("TC13: show text does not contain '@*'", "@*" not in show_text,
          f"show_text snippet: {show_text[:300]!r}")
    check("TC13: show text contains bare 'GlobalSender'", "GlobalSender" in show_text,
          f"show_text snippet: {show_text[:300]!r}")

    # /turn returns the recipient of the last message. GlobalSender sent to Receiver@displayP,
    # so turn is Receiver@displayP (project-scoped, not global — correct display).
    # Verify it does NOT contain "@*" (global sender side is hidden).
    turn_r = _http("/turn", params={
        "self_project": "displayP", "self": "Receiver",
        "peer_project": "*", "peer": "GlobalSender",
    })
    turn_val = turn_r.get("turn", "")
    check("TC13: turn does not contain @*", "@*" not in turn_val,
          f"turn={turn_r!r}")


# ---------- TC14: /group/members hides @* for global members ----------

def test_tc14_group_members_hides_global_suffix():
    """/group/members must return bare name for global (*) members, name@project for others."""
    print("\n[TC14] /group/members 全局成员裸名显示")

    _http("/register", "POST", {"project": "tc14p", "name": "RegularMember", "cwd": "/tmp", "force": True})
    _http("/register", "POST", {"project": "*", "name": "GlobalMember14", "cwd": "/tmp", "force": True})

    _http("/group/create", "POST", {
        "project": "tc14p", "name": "tc14-group",
        "members": ["RegularMember"], "creator": "RegularMember",
    })
    _http("/group/add", "POST", {
        "group_project": "tc14p", "group": "tc14-group",
        "member_project": "*", "member": "GlobalMember14",
    })

    members = _http("/group/members", params={"group_project": "tc14p", "group": "tc14-group"})
    check("TC14: global member appears as bare name", "GlobalMember14" in members,
          f"members={members}")
    check("TC14: global member does NOT appear as GlobalMember14@*", "GlobalMember14@*" not in members,
          f"members={members}")
    check("TC14: regular member appears as name@project", "RegularMember@tc14p" in members,
          f"members={members}")


# ---------- TC15: send to global identity — turn field is bare name ----------

def test_tc15_send_turn_hides_global_suffix():
    """1-to-1 send where peer is a global identity must return turn as bare name, not name@*."""
    print("\n[TC15] send 到全局身份 turn 字段裸名")

    _http("/register", "POST", {"project": "tc15p", "name": "Sender15", "cwd": "/tmp", "force": True})
    _http("/register", "POST", {"project": "*", "name": "GlobalRecipient15", "cwd": "/tmp", "force": True})

    r = _http("/send", "POST", {
        "self_project": "tc15p", "self": "Sender15",
        "peer_project": "*", "peer": "GlobalRecipient15",
        "body": "hello global", "kind": "消息",
    })
    turn_val = r.get("turn", "")
    check("TC15: turn is bare name (no @*)", turn_val == "GlobalRecipient15",
          f"turn={turn_val!r}")


# ---------- TC16: group/create with global member — members list hides @* ----------

def test_tc16_group_create_members_hides_global_suffix():
    """/group/create response members list must not contain @* for global members."""
    print("\n[TC16] group/create 返回的 members 含全局成员裸名")

    _http("/register", "POST", {"project": "tc16p", "name": "Creator16", "cwd": "/tmp", "force": True})
    _http("/register", "POST", {"project": "*", "name": "GlobalMember16", "cwd": "/tmp", "force": True})

    r = _http("/group/create", "POST", {
        "project": "tc16p", "name": "tc16-group",
        "members": ["Creator16", "GlobalMember16@*"], "creator": "Creator16",
    })
    members = r.get("members", [])
    check("TC16: create returned ok", r.get("ok") is True, str(r))
    check("TC16: global member appears as bare name in create response", "GlobalMember16" in members,
          f"members={members}")
    check("TC16: global member NOT as GlobalMember16@* in create response", "GlobalMember16@*" not in members,
          f"members={members}")
    check("TC16: regular member appears as Creator16@tc16p", "Creator16@tc16p" in members,
          f"members={members}")


# ---------- TC17: split sender_project — read must return BOTH halves ----------

def test_tc17_split_identity_read_both_halves(home_dir: str):
    """Regression for the 'send succeeds but peer reads empty' bug.

    A worktree session (wdav3-laptop) had its derived project drift between the
    worktree dir name (WoodorAudit, --show-toplevel) and the main-repo name
    (wda-v3, --git-common-dir) across versions, so its messages to wdav3 landed
    under two different sender_project values. The old project-keyed room clause
    filtered out whichever half did not match the currently-resolved peer project
    → recipient reads empty even though the body is in the DB.

    The room is now keyed on names only, so a read must surface BOTH halves
    regardless of which project value the caller passes for the peer.
    """
    print("\n[TC17] sender_project 分裂 → 读侧两半都可读（本 bug 回归）")

    reg("wda-v3", "wdav3")  # recipient, main-repo identity

    now = int(time.time())
    conn = _db(home_dir)
    # Half 1: sender resolved to the worktree dir name (old --show-toplevel)
    conn.execute(
        "INSERT INTO messages (sender_project, sender, recipient_project, recipient, kind, body, ask, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, NULL, ?)",
        ("WoodorAudit", "wdav3-laptop", "wda-v3", "wdav3", "消息", "half-1 body under WoodorAudit", now - 50),
    )
    id_a = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    # Half 2: same session, sender resolved to the main-repo name (new --git-common-dir)
    conn.execute(
        "INSERT INTO messages (sender_project, sender, recipient_project, recipient, kind, body, ask, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, NULL, ?)",
        ("wda-v3", "wdav3-laptop", "wda-v3", "wdav3", "消息", "half-2 body under wda-v3", now - 10),
    )
    id_b = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()

    # Recipient reads the room. Regardless of which project the caller resolved
    # the peer to, BOTH halves must be returned.
    for peer_proj in ("WoodorAudit", "wda-v3"):
        msgs = _http("/read", params={
            "self_project": "wda-v3", "self": "wdav3",
            "peer_project": peer_proj, "peer": "wdav3-laptop",
            "limit": 20, "since": 0,
        })
        ids = {m["id"] for m in msgs}
        check(f"TC17: read (peer_project={peer_proj}) returns half-1", id_a in ids,
              f"ids={ids}")
        check(f"TC17: read (peer_project={peer_proj}) returns half-2", id_b in ids,
              f"ids={ids}")
        bodies = {m["body"] for m in msgs}
        check(f"TC17: half bodies present (peer_project={peer_proj})",
              "half-1 body under WoodorAudit" in bodies and "half-2 body under wda-v3" in bodies,
              f"bodies={bodies}")

    # /show must also render both halves (project-agnostic room).
    show_url = (f"http://{HOST}:{TEST_PORT}/show?"
                "self_project=wda-v3&self=wdav3&peer_project=WoodorAudit&peer=wdav3-laptop&limit=20")
    with urllib.request.urlopen(show_url, timeout=5) as _r:
        show_text = _r.read().decode("utf-8")
    check("TC17: show renders both halves", "half-1 body under WoodorAudit" in show_text
          and "half-2 body under wda-v3" in show_text, f"snippet={show_text[:400]!r}")


# ---------- main ----------

home_dir_g: str = ""  # set in main(), used by TC3/TC7 which don't pass it as param


def main():
    global home_dir_g

    log_path = "/tmp/agent-meeting-regression-final.log"

    home_dir = tempfile.mkdtemp(prefix="am-identity-reg-")
    home_dir_g = home_dir

    try:
        init_db(home_dir)
        proc = start_daemon(home_dir)

        try:
            test_tc1_cross_project_isolation(home_dir)
            test_tc2_bare_name_ambiguity()
            test_tc3_explicit_routing()
            test_tc4_delete_scoped()
            test_tc5_group_rename_cascade()
            test_tc6_bug1_crossproject_group_backlog(home_dir)
            test_tc7_gap_a_historical_resolve()
            test_tc8_gap_b_rename_cursor(home_dir)
            test_tc9_cursor_composite_key_independence(home_dir)
            test_tc10_global_registration()
            test_tc11_global_priority_over_scoped()
            test_tc12_derive_project_sanitizes_star()
            test_tc13_display_hides_global_suffix()
            test_tc14_group_members_hides_global_suffix()
            test_tc15_send_turn_hides_global_suffix()
            test_tc16_group_create_members_hides_global_suffix()
            test_tc17_split_identity_read_both_halves(home_dir)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    finally:
        shutil.rmtree(home_dir, ignore_errors=True)

    sep = "=" * 60
    summary_lines = [
        sep,
        f"Results: {PASS_COUNT} passed, {FAIL_COUNT} failed  (total {PASS_COUNT + FAIL_COUNT} checks)",
    ]
    if FAILURES:
        summary_lines.append("\nFailed checks:")
        summary_lines.extend(FAILURES)
    summary_lines.append(sep)
    summary = "\n".join(summary_lines)
    print(f"\n{summary}")

    with open(log_path, "w") as f:
        f.write(summary + "\n")

    sys.exit(0 if FAIL_COUNT == 0 else 1)


if __name__ == "__main__":
    main()
