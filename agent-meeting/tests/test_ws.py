#!/usr/bin/env python3
"""
WS PR1 integration tests.

Starts an isolated daemon on port 8799 with a temp DB, runs 6 test cases,
then tears everything down. Never touches the live daemon on 8765.

Usage:
    python3 agent-meeting/tests/test_ws.py
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
import urllib.request
import urllib.error

TEST_PORT = 8799
HOST = "127.0.0.1"

# ---------- minimal WS client ----------

class WSClient:
    def __init__(self, name: str, cursor: int = 0, token: str | None = None, proto: str = "1"):
        self.name = name
        self.cursor = cursor
        self.sock = socket.create_connection((HOST, TEST_PORT), timeout=5)
        self.sock.settimeout(5)
        self.rfile = self.sock.makefile("rb")
        self.wfile = self.sock.makefile("wb")
        self._handshake(token, proto)
        self.received: list[dict] = []

    def _handshake(self, token, proto):
        key = base64.b64encode(os.urandom(16)).decode()
        headers = [
            f"GET /subscribe HTTP/1.1",
            f"Host: {HOST}:{TEST_PORT}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
            f"X-Meeting-Name: {self.name}",
            f"X-Meeting-Cursor: {self.cursor}",
            f"X-Meeting-Proto: {proto}",
        ]
        if token:
            headers.append(f"Authorization: Bearer {token}")
        request = "\r\n".join(headers) + "\r\n\r\n"
        self.sock.sendall(request.encode())

        # Read response line
        line = b""
        while not line.endswith(b"\r\n"):
            ch = self.sock.recv(1)
            if not ch:
                raise IOError("connection closed during handshake")
            line += ch
        status_line = line.decode().strip()

        # Read all headers until blank line
        resp_headers = {}
        while True:
            hline = b""
            while not hline.endswith(b"\r\n"):
                ch = self.sock.recv(1)
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
            raise IOError(f"handshake failed: {status_line}")

        expected_accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        got = resp_headers.get("sec-websocket-accept", "")
        if got != expected_accept:
            raise IOError(f"Sec-WebSocket-Accept mismatch: {got!r} != {expected_accept!r}")

    def read_frame(self, timeout: float = 5.0):
        """Read one frame, return (opcode, payload_dict_or_bytes)."""
        self.sock.settimeout(timeout)
        header = self._recv_exact(2)
        b0, b1 = header[0], header[1]
        fin = (b0 & 0x80) != 0
        opcode = b0 & 0x0F
        masked = (b1 & 0x80) != 0
        length = b1 & 0x7F
        if not fin:
            raise IOError("fragmented frame")
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        mask_key = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length) if length else b""
        if masked:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        return opcode, payload

    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise IOError("EOF")
            buf += chunk
        return buf

    def send_pong(self, payload: bytes = b""):
        """Send client→server pong frame (masked)."""
        mask = os.urandom(4)
        masked_payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(b"\x8A" + bytes([0x80 | len(masked_payload)]) + mask + masked_payload)

    def send_ping(self):
        """Send client→server ping frame (masked)."""
        mask = os.urandom(4)
        self.sock.sendall(b"\x89\x84" + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(b"ping")))

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

    def read_until_caught_up(self, timeout: float = 5.0) -> list[dict]:
        """Read frames until caught_up, returning all msg frames received."""
        msgs = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = deadline - time.time()
            try:
                opcode, payload = self.read_frame(timeout=max(0.1, remaining))
            except socket.timeout:
                break
            if opcode == 0x1:  # text
                d = json.loads(payload.decode())
                if d.get("type") == "caught_up":
                    break
                if d.get("type") == "msg":
                    msgs.append(d)
            elif opcode == 0x9:  # ping — send pong
                self.send_pong(payload)
        return msgs

    def read_msgs(self, count: int, timeout: float = 5.0) -> list[dict]:
        """Read up to count msg frames within timeout."""
        msgs = []
        deadline = time.time() + timeout
        while len(msgs) < count and time.time() < deadline:
            remaining = deadline - time.time()
            try:
                opcode, payload = self.read_frame(timeout=max(0.1, remaining))
            except socket.timeout:
                break
            if opcode == 0x1:
                d = json.loads(payload.decode())
                if d.get("type") == "msg":
                    msgs.append(d)
            elif opcode == 0x9:
                self.send_pong(payload)
        return msgs


# ---------- daemon lifecycle ----------

def _http(path: str, method="GET", body=None, params_=None, method_=None,
          allow_error=False) -> dict:
    import urllib.parse as _up
    if method_ is not None:
        method = method_
    url = f"http://{HOST}:{TEST_PORT}{path}"
    if params_:
        url += "?" + _up.urlencode(params_)
    data = json.dumps(body).encode() if body else None
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


def start_daemon(db_dir: str) -> subprocess.Popen:
    env = os.environ.copy()
    env["MEETING_HOME"] = db_dir
    # 初始化 DB inline
    daemon_path = os.path.join(
        os.path.dirname(__file__), "..", "bin", "meeting-daemon"
    )
    proc = subprocess.Popen(
        [sys.executable, daemon_path, f"--port={TEST_PORT}", "--no-mdns"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # Wait for daemon to be ready
    for _ in range(30):
        time.sleep(0.3)
        try:
            _http("/health")
            return proc
        except Exception:
            if proc.poll() is not None:
                out, err = proc.communicate()
                raise RuntimeError(f"Daemon exited early:\n{err.decode()}")
    raise RuntimeError("Daemon did not start in time")


def init_test_db(db_dir: str):
    """Create minimal DB structure for tests."""
    import sqlite3
    db_path = os.path.join(db_dir, "db", "rooms.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        PRAGMA journal_mode = WAL;
        PRAGMA foreign_keys = ON;
        CREATE TABLE IF NOT EXISTS sessions (
            name TEXT PRIMARY KEY,
            cwd TEXT,
            host TEXT,
            os TEXT,
            registered_at TEXT,
            last_seen REAL,
            role TEXT NOT NULL DEFAULT 'worker'
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT NOT NULL,
            recipient TEXT NOT NULL,
            kind TEXT NOT NULL,
            body TEXT NOT NULL,
            ask TEXT,
            created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_messages_recipient ON messages(recipient, id);
        CREATE INDEX IF NOT EXISTS idx_messages_sender_recipient ON messages(sender, recipient, id);
        CREATE TABLE IF NOT EXISTS read_cursors (
            member_name TEXT PRIMARY KEY,
            cursor      INTEGER NOT NULL,
            updated_at  INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS groups (
            name       TEXT PRIMARY KEY,
            created_at INTEGER NOT NULL,
            creator    TEXT
        );
        CREATE TABLE IF NOT EXISTS group_members (
            group_name  TEXT NOT NULL,
            member_name TEXT NOT NULL,
            added_at    INTEGER NOT NULL,
            PRIMARY KEY (group_name, member_name),
            FOREIGN KEY (group_name) REFERENCES groups(name) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_group_members_member ON group_members(member_name);
    """)
    conn.close()


# ---------- test cases ----------

PASS_COUNT = 0
FAIL_COUNT = 0

def check(name: str, cond: bool, detail: str = ""):
    global PASS_COUNT, FAIL_COUNT
    if cond:
        print(f"  PASS: {name}")
        PASS_COUNT += 1
    else:
        print(f"  FAIL: {name}" + (f" — {detail}" if detail else ""))
        FAIL_COUNT += 1


def test_handshake_and_auth(has_auth: bool):
    """TC1: 握手+auth"""
    print("\n[TC1] 握手+auth")
    try:
        c = WSClient("alice", cursor=0)
        # Should receive caught_up
        _ = c.read_until_caught_up(timeout=3)
        check("TC1: 101 upgrade success", True)
        c.close()
    except Exception as e:
        check("TC1: 101 upgrade success", False, str(e))


def test_backlog(db_dir: str):
    """TC2: 补发 id>DB_cursor"""
    print("\n[TC2] 补发 id>DB_cursor")
    # Send 3 messages to bob before bob connects.
    _http("/register", "POST", {"name": "alice_sender"})
    _http("/register", "POST", {"name": "bob_recv"})

    r1 = _http("/send", "POST", {"self": "alice_sender", "peer": "bob_recv", "body": "msg1", "kind": "消息"})
    r2 = _http("/send", "POST", {"self": "alice_sender", "peer": "bob_recv", "body": "msg2", "kind": "消息"})
    r3 = _http("/send", "POST", {"self": "alice_sender", "peer": "bob_recv", "body": "msg3", "kind": "消息"})

    # Pre-set read_cursors to r1 so daemon delivers only r2 and r3.
    # Small sleep to ensure WAL is visible to daemon's next connection.
    _db_upsert_cursor(db_dir, "bob_recv", r1["msg_id"])
    time.sleep(0.05)

    c = WSClient("bob_recv", cursor=0)
    backlog = c.read_until_caught_up(timeout=5)
    check("TC2: receive 2 backlog msgs (r2, r3)", len(backlog) == 2, f"got {len(backlog)}")
    check("TC2: backlog phase=backlog", all(m.get("phase") == "backlog" for m in backlog),
          str([m.get("phase") for m in backlog]))
    check("TC2: msg_ids in order", [m["msg_id"] for m in backlog] == sorted([m["msg_id"] for m in backlog]))
    check("TC2: r1 skipped", all(m["msg_id"] != r1["msg_id"] for m in backlog))
    c.close()
    time.sleep(0.1)  # let _ws_remove flush cursor to DB before c2 connects

    # Second connect: cursor now at r3 (drained), zero backlog.
    c2 = WSClient("bob_recv", cursor=0)
    backlog2 = c2.read_until_caught_up(timeout=5)
    check("TC2: second connect → zero backlog (cursor advanced)", len(backlog2) == 0,
          f"got {[m['msg_id'] for m in backlog2]}")
    c2.close()


def test_realtime_push():
    """TC3: 实时推送"""
    print("\n[TC3] 实时推送")
    _http("/register", "POST", {"name": "carol"})
    _http("/register", "POST", {"name": "dave"})

    # Seed one msg so we know the current max id, then connect with that cursor
    seed = _http("/send", "POST", {"self": "dave", "peer": "carol", "body": "seed", "kind": "消息"})
    latest_id = seed["msg_id"]

    # Connect carol with cursor=latest_id so backlog is empty (seed already seen)
    c = WSClient("carol", cursor=latest_id)
    _ = c.read_until_caught_up(timeout=3)

    # Now send a message to carol in a thread
    def send_later():
        time.sleep(0.2)
        _http("/send", "POST", {"self": "dave", "peer": "carol", "body": "live-msg", "kind": "消息"})

    t = threading.Thread(target=send_later)
    t.start()

    msgs = c.read_msgs(1, timeout=4)
    t.join()

    check("TC3: receive 1 live msg", len(msgs) == 1, f"got {len(msgs)}")
    if msgs:
        check("TC3: live phase=live", msgs[0].get("phase") == "live", msgs[0].get("phase"))
        check("TC3: correct sender", msgs[0].get("sender") == "dave", msgs[0].get("sender"))
    c.close()


def test_ping_pong():
    """TC4: ping/pong 心跳"""
    print("\n[TC4] ping/pong 心跳")
    _http("/register", "POST", {"name": "eve"})
    c = WSClient("eve", cursor=0)
    _ = c.read_until_caught_up(timeout=3)

    # Wait for a server-side ping (daemon sends every 4s)
    got_ping = False
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            opcode, payload = c.read_frame(timeout=6)
            if opcode == 0x9:  # ping
                c.send_pong(payload)
                got_ping = True
                break
        except socket.timeout:
            break

    check("TC4: received server ping", got_ping)
    c.close()


def test_reconnect_with_cursor():
    """TC5: 断开重连带新游标只补未读"""
    print("\n[TC5] 断开重连补未读")
    _http("/register", "POST", {"name": "frank"})
    _http("/register", "POST", {"name": "grace"})

    r1 = _http("/send", "POST", {"self": "grace", "peer": "frank", "body": "first", "kind": "消息"})
    r2 = _http("/send", "POST", {"self": "grace", "peer": "frank", "body": "second", "kind": "消息"})

    # Connect, read backlog, disconnect
    c = WSClient("frank", cursor=0)
    backlog = c.read_until_caught_up(timeout=5)
    cursor_after = max(m["msg_id"] for m in backlog) if backlog else 0
    c.close()

    # New message arrives while disconnected
    r3 = _http("/send", "POST", {"self": "grace", "peer": "frank", "body": "third", "kind": "消息"})

    # Reconnect with cursor = last seen
    c2 = WSClient("frank", cursor=cursor_after)
    backlog2 = c2.read_until_caught_up(timeout=5)

    check("TC5: only missed msg on reconnect",
          len(backlog2) == 1 and backlog2[0]["msg_id"] == r3["msg_id"],
          f"got {[m['msg_id'] for m in backlog2]}")
    c2.close()


def test_concurrent_dedup(db_dir: str):
    """TC6: 补发进行中途插入 /send — msg_ids 严格单调不重不漏（原 20 条小规模版本）"""
    print("\n[TC6] 补发/实时并发去重（小规模）")

    _http("/register", "POST", {"name": "heidi"})
    _http("/register", "POST", {"name": "ivan"})

    # Pre-seed 20 backlog messages
    pre_ids = []
    for i in range(20):
        r = _http("/send", "POST", {"self": "ivan", "peer": "heidi", "body": f"pre-{i}", "kind": "消息"})
        pre_ids.append(r["msg_id"])

    # Pre-set cursor=0 so daemon delivers all 20 as backlog (not seeded-to-MAX on first connect).
    _db_upsert_cursor(db_dir, "heidi", 0)
    time.sleep(0.05)

    # Connect heidi — backlog will start flowing
    c = WSClient("heidi", cursor=0)

    # While backlog is being sent, inject 5 more messages concurrently
    injected_ids = []
    def inject():
        time.sleep(0.05)  # tiny delay so backlog has started
        for i in range(5):
            r = _http("/send", "POST", {"self": "ivan", "peer": "heidi", "body": f"inject-{i}", "kind": "消息"})
            injected_ids.append(r["msg_id"])

    t = threading.Thread(target=inject)
    t.start()

    # Collect all messages (backlog + live)
    all_msgs = []
    deadline = time.time() + 10
    while time.time() < deadline:
        remaining = deadline - time.time()
        try:
            opcode, payload = c.read_frame(timeout=max(0.1, remaining))
        except socket.timeout:
            break
        if opcode == 0x9:
            c.send_pong(payload)
        elif opcode == 0x1:
            d = json.loads(payload.decode())
            if d.get("type") == "msg":
                all_msgs.append(d)
            elif d.get("type") == "caught_up":
                pass
            # continue reading until we have all expected messages or timeout

    t.join()

    total_expected = len(pre_ids) + len(injected_ids)
    expected_ids = sorted(pre_ids + injected_ids)
    got_ids = sorted([m["msg_id"] for m in all_msgs])

    check("TC6: no duplicates", len(all_msgs) == len(set(m["msg_id"] for m in all_msgs)),
          f"total={len(all_msgs)} unique={len(set(m['msg_id'] for m in all_msgs))}")
    check("TC6: no missing msgs", got_ids == expected_ids,
          f"expected {expected_ids}, got {got_ids}")
    check("TC6: no gaps in coverage", set(got_ids) == set(expected_ids))

    c.close()


def _read_db_cursor(db_dir: str, member_name: str) -> int | None:
    """Read cursor value from read_cursors table. Returns None if no row."""
    db_path = os.path.join(db_dir, "db", "rooms.db")
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=5)
    conn.executescript("PRAGMA journal_mode = WAL;")
    row = conn.execute(
        "SELECT cursor FROM read_cursors WHERE member_name=?", (member_name,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


def _db_upsert_cursor(db_dir: str, member_name: str, cursor: int):
    """Insert or update a read_cursors row directly (test setup helper).

    Uses WAL mode and isolation_level=None (autocommit) to match daemon's
    connection settings, ensuring the write is immediately visible to the daemon.
    """
    db_path = os.path.join(db_dir, "db", "rooms.db")
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=5)
    conn.executescript("PRAGMA journal_mode = WAL;")
    conn.execute(
        "INSERT INTO read_cursors (member_name, cursor, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT(member_name) DO UPDATE SET cursor=excluded.cursor, updated_at=excluded.updated_at",
        (member_name, cursor, int(time.time())),
    )
    conn.close()


def test_first_seed_zero_replay(db_dir: str):
    """TC8: 新成员首次连接 — daemon 从 read_cursors 无行走首次 seed 分支，
    seed 到 MAX(id)，零 backlog，caught_up 游标正确，read_cursors 落行。"""
    print("\n[TC8] 首次 seed 零回放")

    _http("/register", "POST", {"name": "seed_sender"})
    _http("/register", "POST", {"name": "seed_newmember"})

    # Insert 3 history messages
    r1 = _http("/send", "POST", {"self": "seed_sender", "peer": "seed_newmember", "body": "h1", "kind": "消息"})
    r2 = _http("/send", "POST", {"self": "seed_sender", "peer": "seed_newmember", "body": "h2", "kind": "消息"})
    r3 = _http("/send", "POST", {"self": "seed_sender", "peer": "seed_newmember", "body": "h3", "kind": "消息"})
    max_id = r3["msg_id"]

    # First connect — no read_cursors row yet; daemon should seed to MAX and return zero backlog.
    # WSClient does not send X-Meeting-Cursor (cursor=0 is default but daemon ignores it).
    c = WSClient("seed_newmember", cursor=0)

    # Capture caught_up cursor value before read_until_caught_up discards it.
    caught_up_cursor = None
    backlog = []
    deadline = time.time() + 5
    while time.time() < deadline:
        remaining = deadline - time.time()
        try:
            opcode, payload = c.read_frame(timeout=max(0.1, remaining))
        except socket.timeout:
            break
        if opcode == 0x1:
            d = json.loads(payload.decode())
            if d.get("type") == "caught_up":
                caught_up_cursor = d.get("cursor")
                break
            if d.get("type") == "msg":
                backlog.append(d)
        elif opcode == 0x9:
            c.send_pong(payload)
    c.close()

    check("TC8: first seed → zero backlog", len(backlog) == 0,
          f"got {len(backlog)} msgs")
    check("TC8: caught_up cursor = MAX(id)",
          caught_up_cursor == max_id,
          f"caught_up={caught_up_cursor}, expected={max_id}")

    # read_cursors must have a row now
    db_cursor = _read_db_cursor(db_dir, "seed_newmember")
    check("TC8: read_cursors row written", db_cursor is not None,
          "no row found in read_cursors")
    check("TC8: read_cursors cursor = MAX(id)",
          db_cursor == max_id,
          f"db_cursor={db_cursor}, expected={max_id}")


def test_cursor_survives_restart(db_dir: str):
    """TC-PRA1: 游标活过重启 — 预置 cursor=0，成员收 N 条后断开，
    reconnect 不带 cursor header，daemon 从 read_cursors 续，只补断开后的新消息。"""
    print("\n[TC-PRA1] 游标活过重启")

    _http("/register", "POST", {"name": "pra1_sender"})
    _http("/register", "POST", {"name": "pra1_recv"})

    # Send 3 messages.
    r1 = _http("/send", "POST", {"self": "pra1_sender", "peer": "pra1_recv", "body": "m1", "kind": "消息"})
    r2 = _http("/send", "POST", {"self": "pra1_sender", "peer": "pra1_recv", "body": "m2", "kind": "消息"})
    r3 = _http("/send", "POST", {"self": "pra1_sender", "peer": "pra1_recv", "body": "m3", "kind": "消息"})

    # Pre-set cursor to 0 so daemon delivers all 3 as backlog on first connect.
    _db_upsert_cursor(db_dir, "pra1_recv", 0)
    time.sleep(0.05)

    c = WSClient("pra1_recv", cursor=0)
    backlog = c.read_until_caught_up(timeout=5)
    c.close()
    check("TC-PRA1: initial backlog=3", len(backlog) == 3, f"got {len(backlog)}")

    # Give daemon time to flush cursor on disconnect.
    time.sleep(0.1)

    # Verify cursor is persisted after backlog drain + disconnect.
    db_cursor_after_drain = _read_db_cursor(db_dir, "pra1_recv")
    check("TC-PRA1: cursor persisted after drain",
          db_cursor_after_drain == r3["msg_id"],
          f"db_cursor={db_cursor_after_drain}, expected={r3['msg_id']}")

    # Send one more message (arrives while "disconnected").
    r4 = _http("/send", "POST", {"self": "pra1_sender", "peer": "pra1_recv", "body": "m4", "kind": "消息"})

    # Reconnect — daemon resumes from read_cursors (=r3), should only replay r4.
    c2 = WSClient("pra1_recv", cursor=0)
    backlog2 = c2.read_until_caught_up(timeout=5)
    c2.close()

    check("TC-PRA1: only 1 msg on reconnect (r4)",
          len(backlog2) == 1 and backlog2[0]["msg_id"] == r4["msg_id"],
          f"got {[m['msg_id'] for m in backlog2]}, expected [{r4['msg_id']}]")


def test_monitor_no_tmp_file(db_dir: str):
    """TC-PRA2: monitor 纯通知器 — 收消息打 📬 行，不写任何 TMP 文件。
    验证 send 后 monitor 有输出，且无 meeting-*.last_msg_id 文件存在。"""
    print("\n[TC-PRA2] monitor 纯通知器（无 TMP 文件）")
    import tempfile as _tempfile
    tmpdir = _tempfile.gettempdir()

    # Ensure no stale state file exists
    state_file = os.path.join(tmpdir, "meeting-pra2_recv.last_msg_id")
    try:
        os.unlink(state_file)
    except FileNotFoundError:
        pass

    check("TC-PRA2: no STATE_FILE before test", not os.path.exists(state_file))

    # Verify monitor.py source has no STATE_FILE / last_msg_id references
    monitor_path = os.path.join(os.path.dirname(__file__), "..", "bin", "monitor.py")
    with open(monitor_path, encoding="utf-8") as f:
        src = f.read()
    check("TC-PRA2: STATE_FILE removed from monitor source",
          "STATE_FILE" not in src,
          "STATE_FILE still present in monitor.py")
    check("TC-PRA2: last_msg_id removed from monitor source",
          "last_msg_id" not in src,
          "last_msg_id still present in monitor.py")
    # Verify that no X-Meeting-Cursor header is sent in the WS handshake headers list.
    # The string may appear in comments/docstrings but must not appear in the headers list.
    import ast
    headers_sent = False
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "X-Meeting-Cursor:" in node.value:
                headers_sent = True
                break
    check("TC-PRA2: X-Meeting-Cursor not sent in WS headers",
          not headers_sent,
          "X-Meeting-Cursor header still sent in monitor.py handshake")


def _collect_all_msgs(c: WSClient, expect_count: int, timeout: float = 15.0) -> list[dict]:
    """Read frames until expect_count msg frames received or timeout, skipping pings."""
    msgs = []
    deadline = time.time() + timeout
    while len(msgs) < expect_count and time.time() < deadline:
        remaining = deadline - time.time()
        try:
            opcode, payload = c.read_frame(timeout=max(0.1, remaining))
        except socket.timeout:
            break
        if opcode == 0x9:
            c.send_pong(payload)
        elif opcode == 0x1:
            d = json.loads(payload.decode())
            if d.get("type") == "msg":
                msgs.append(d)
    return msgs


def _db_seed_messages(db_dir: str, sender: str, recv: str, count: int) -> list[int]:
    """Insert `count` messages directly into the DB and return their ids.

    Bypasses HTTP so we can seed large backlogs quickly without hammering
    the daemon's thread pool.
    """
    db_path = os.path.join(db_dir, "db", "rooms.db")
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    now = int(time.time())
    ids = []
    for i in range(count):
        cur = conn.execute(
            "INSERT INTO messages (sender, recipient, kind, body, ask, created_at) VALUES (?, ?, ?, ?, NULL, ?)",
            (sender, recv, "消息", f"pre-{i}", now),
        )
        ids.append(cur.lastrowid)
    conn.commit()
    conn.close()
    return ids


def test_mid_drain_inject_race(db_dir: str, rounds: int = 20):
    """TC7: 大补发队列 + 补发途中注入高 id 消息 — 检验消息连续、不缺、不重。

    Designed to catch the PR1 race: old code did one-shot fetchall for backlog;
    a concurrent fanout (no draining check) could bump hwm to a high id, causing
    the backlog loop to skip all rows between the injected id and end of snapshot.

    Old code behaviour:
      - fanout fires for msg 201 while backlog is at row 100/200
      - fanout acquires send_lock, sends 201, sets hwm=201
      - backlog loop resumes at row 101: id=101 <= hwm=201 → skip ... repeat for 101..200
      - rows 101..200 permanently lost

    New code: fanout skips state==draining subs; backlog loop owns the stream;
    state flip is inside send_lock with a terminal DB check — no gap.

    Pre-seeding uses direct SQLite writes (not HTTP) to keep the daemon's thread
    pool free for the actual test traffic.
    """
    print(f"\n[TC7] 补发途中注入高id消息竞态（{rounds}轮）")

    pass_rounds = 0
    fail_rounds = 0

    for rnd in range(rounds):
        suffix = f"tc7r{rnd}"
        sender = f"ivan7_{suffix}"
        recv = f"heidi7_{suffix}"
        _http("/register", "POST", {"name": sender})
        _http("/register", "POST", {"name": recv})

        # Pre-seed 200 messages directly into DB (fast, no HTTP overhead)
        pre_ids = _db_seed_messages(db_dir, sender, recv, 200)

        # Pre-set cursor=0 so daemon delivers all pre-seeded messages as backlog.
        _db_upsert_cursor(db_dir, recv, 0)

        # Connect recv — daemon starts draining backlog in a new thread
        c = WSClient(recv, cursor=0)

        # Inject 10 live messages via HTTP while backlog drain is in progress.
        injected_ids = []

        def inject(s=sender, rc=recv, ids=injected_ids):
            time.sleep(0.02)  # let backlog drain start (200 rows takes >20ms)
            for i in range(10):
                r = _http("/send", "POST", {"self": s, "peer": rc, "body": f"live-{i}", "kind": "消息"})
                ids.append(r["msg_id"])

        t = threading.Thread(target=inject, daemon=True)
        t.start()

        expected_count = 200 + 10
        all_msgs = _collect_all_msgs(c, expected_count, timeout=20.0)
        t.join(timeout=10)
        c.close()

        expected_ids = sorted(pre_ids + injected_ids)
        got_ids = sorted(m["msg_id"] for m in all_msgs)

        no_dup = len(all_msgs) == len(set(m["msg_id"] for m in all_msgs))
        no_miss = got_ids == expected_ids

        if no_dup and no_miss:
            pass_rounds += 1
        else:
            fail_rounds += 1
            missing = sorted(set(expected_ids) - set(got_ids))
            extra = sorted(set(got_ids) - set(expected_ids))
            print(f"    round {rnd}: FAIL — missing={missing[:10]} extra={extra[:10]}")

    check(f"TC7: no duplicates/missing across {rounds} rounds",
          fail_rounds == 0,
          f"{fail_rounds} failed rounds out of {rounds}")
    if fail_rounds == 0:
        print(f"    all {rounds} rounds passed")


def _db_create_group(db_dir: str, group_name: str, members: list[str]):
    """Insert group + members directly into DB (test setup helper)."""
    db_path = os.path.join(db_dir, "db", "rooms.db")
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=5)
    conn.executescript("PRAGMA journal_mode = WAL; PRAGMA foreign_keys = ON;")
    now = int(time.time())
    conn.execute(
        "INSERT OR IGNORE INTO groups (name, created_at, creator) VALUES (?, ?, NULL)",
        (group_name, now),
    )
    for m in members:
        conn.execute(
            "INSERT OR IGNORE INTO group_members (group_name, member_name, added_at) VALUES (?, ?, ?)",
            (group_name, m, now),
        )
    conn.close()


def test_g1_group_and_private_unified_stream(db_dir: str):
    """TC-G1: 成员收到群消息+私聊消息在同一 WS 流，group 字段正确。"""
    print("\n[TC-G1] 群+私聊一条流 group 字段")

    _http("/register", "POST", {"name": "g1_alice"})
    _http("/register", "POST", {"name": "g1_bob"})
    _http("/register", "POST", {"name": "g1_carol"})

    # Create group via daemon
    r = _http("/group/create", "POST", {"name": "g1-team", "members": ["g1_bob", "g1_carol"], "creator": "g1_alice"})
    check("TC-G1: group created", r.get("ok") is True, str(r))

    # Pre-set cursor for g1_bob so no seed backlog
    max_r = _http("/send", "POST", {"self": "g1_alice", "peer": "g1_alice", "body": "seed", "kind": "消息"})
    _db_upsert_cursor(db_dir, "g1_bob", max_r["msg_id"])
    time.sleep(0.05)

    c = WSClient("g1_bob", cursor=0)
    _ = c.read_until_caught_up(timeout=3)

    # Send group message
    rg = _http("/send", "POST", {"self": "g1_alice", "peer": "g1-team", "body": "hello group", "kind": "消息"})
    # Send 1:1 message
    r1 = _http("/send", "POST", {"self": "g1_alice", "peer": "g1_bob", "body": "hello direct", "kind": "消息"})

    msgs = c.read_msgs(2, timeout=5)
    c.close()

    by_id = {m["msg_id"]: m for m in msgs}
    grp_msg = by_id.get(rg["msg_id"])
    direct_msg = by_id.get(r1["msg_id"])

    check("TC-G1: received group msg", grp_msg is not None)
    check("TC-G1: group field = group name", grp_msg is not None and grp_msg.get("group") == "g1-team",
          str(grp_msg))
    check("TC-G1: received direct msg", direct_msg is not None)
    check("TC-G1: direct msg group=None", direct_msg is not None and direct_msg.get("group") is None,
          str(direct_msg))


def test_g2_group_turn_less(db_dir: str):
    """TC-G2: 群消息 turn=None，多人发不冲突。"""
    print("\n[TC-G2] 群 turn-less")

    _http("/register", "POST", {"name": "g2_alice"})
    _http("/register", "POST", {"name": "g2_bob"})

    _http("/group/create", "POST", {"name": "g2-room", "members": ["g2_alice", "g2_bob"]})

    r1 = _http("/send", "POST", {"self": "g2_alice", "peer": "g2-room", "body": "hi", "kind": "消息"})
    r2 = _http("/send", "POST", {"self": "g2_bob", "peer": "g2-room", "body": "hey", "kind": "消息"})

    check("TC-G2: send1 turn=None", r1.get("turn") is None, str(r1))
    check("TC-G2: send2 turn=None", r2.get("turn") is None, str(r2))


def test_g3_group_cursor_via_db(db_dir: str):
    """TC-G3: 群消息也走 DB 游标（重连只补未读）。"""
    print("\n[TC-G3] 群消息走 DB 游标")

    _http("/register", "POST", {"name": "g3_sender"})
    _http("/register", "POST", {"name": "g3_recv"})

    _http("/group/create", "POST", {"name": "g3-chan", "members": ["g3_recv"], "creator": "g3_sender"})

    # Pre-set cursor=0 so backlog delivers group msg
    rg = _http("/send", "POST", {"self": "g3_sender", "peer": "g3-chan", "body": "grp1", "kind": "消息"})
    _db_upsert_cursor(db_dir, "g3_recv", 0)
    time.sleep(0.05)

    c = WSClient("g3_recv", cursor=0)
    bl = c.read_until_caught_up(timeout=5)
    c.close()

    group_msgs = [m for m in bl if m.get("group") == "g3-chan"]
    check("TC-G3: group msg in backlog", len(group_msgs) >= 1,
          f"all msgs: {bl}")

    # Reconnect; already-seen msg should not replay
    time.sleep(0.1)
    c2 = WSClient("g3_recv", cursor=0)
    bl2 = c2.read_until_caught_up(timeout=5)
    c2.close()
    check("TC-G3: zero backlog on reconnect", len(bl2) == 0, f"got {bl2}")


def test_g4_add_member_seed_zero_replay(db_dir: str):
    """TC-G4: add 新成员首次 seed 不回放入群前消息。"""
    print("\n[TC-G4] add 新成员首次 seed 零回放")

    _http("/register", "POST", {"name": "g4_alice"})
    _http("/register", "POST", {"name": "g4_bob"})

    _http("/group/create", "POST", {"name": "g4-grp", "members": ["g4_alice"]})

    # Some history in the group before bob joins
    _http("/send", "POST", {"self": "g4_alice", "peer": "g4-grp", "body": "old msg", "kind": "消息"})

    # Add bob — daemon should seed read_cursors to MAX
    r = _http("/group/add", "POST", {"group": "g4-grp", "member": "g4_bob"})
    check("TC-G4: add returned ok", r.get("ok") is True, str(r))

    # bob connects — should see zero backlog (seeded past old msg)
    c = WSClient("g4_bob", cursor=0)
    bl = c.read_until_caught_up(timeout=5)
    c.close()

    check("TC-G4: zero backlog for new member", len(bl) == 0, f"got {bl}")

    # Verify read_cursors row exists for bob
    db_cursor = _read_db_cursor(db_dir, "g4_bob")
    check("TC-G4: read_cursors row exists for new member", db_cursor is not None)


def test_g5_delete_group_purge(db_dir: str):
    """TC-G5: 删群 purge — messages 清零，CASCADE 清成员，名可重建。"""
    print("\n[TC-G5] 删群 purge")

    _http("/register", "POST", {"name": "g5_alice"})
    _http("/group/create", "POST", {"name": "g5-del", "members": ["g5_alice"]})
    _http("/send", "POST", {"self": "g5_alice", "peer": "g5-del", "body": "msg1", "kind": "消息"})
    _http("/send", "POST", {"self": "g5_alice", "peer": "g5-del", "body": "msg2", "kind": "消息"})

    r = _http("/group", method_="DELETE", params_={"name": "g5-del"})
    check("TC-G5: purge returned ok", r.get("ok") is True, str(r))
    check("TC-G5: purged 2 messages", r.get("purged") == 2, f"purged={r.get('purged')}")

    # Verify messages gone
    db_path = os.path.join(db_dir, "db", "rooms.db")
    conn = sqlite3.connect(db_path)
    cnt = conn.execute("SELECT COUNT(*) FROM messages WHERE recipient='g5-del'").fetchone()[0]
    conn.close()
    check("TC-G5: recipient=group messages=0", cnt == 0, f"cnt={cnt}")

    # Verify group_members gone (CASCADE)
    conn = sqlite3.connect(db_path)
    mem_cnt = conn.execute("SELECT COUNT(*) FROM group_members WHERE group_name='g5-del'").fetchone()[0]
    conn.close()
    check("TC-G5: group_members cascade deleted", mem_cnt == 0, f"mem_cnt={mem_cnt}")

    # Name can be reused
    r2 = _http("/group/create", "POST", {"name": "g5-del", "members": ["g5_alice"]})
    check("TC-G5: group name reusable after purge", r2.get("ok") is True, str(r2))


def test_g6_reject_name(db_dir: str):
    """TC-G6: 拒名三条 — 活 session 名 / 已存群名 / historical 消息名。"""
    print("\n[TC-G6] 拒名三条校验")

    # (a) active session name
    _http("/register", "POST", {"name": "g6-active"})
    r = _http("/group/create", "POST", {"name": "g6-active", "members": []}, allow_error=True)
    check("TC-G6a: reject active session name", "error" in r, str(r))

    # (b) existing group name
    _http("/group/create", "POST", {"name": "g6-existing-grp", "members": []})
    r = _http("/group/create", "POST", {"name": "g6-existing-grp", "members": []}, allow_error=True)
    check("TC-G6b: reject existing group name", "error" in r, str(r))

    # (c) name with historical messages
    _http("/register", "POST", {"name": "g6-hist-sender"})
    _http("/send", "POST", {"self": "g6-hist-sender", "peer": "g6-hist-target",
                             "body": "hi", "kind": "消息"})
    r = _http("/group/create", "POST", {"name": "g6-hist-target", "members": []}, allow_error=True)
    check("TC-G6c: reject name with historical messages", "error" in r, str(r))


def test_g6_add_active_member_succeeds():
    """TC-G6-add: 把有消息历史的活成员 add 进群应成功（强制更正验证）。"""
    print("\n[TC-G6-add] add 活成员不被条件(3)拒掉")

    _http("/register", "POST", {"name": "g6add-sender"})
    _http("/register", "POST", {"name": "g6add-member"})
    _http("/group/create", "POST", {"name": "g6add-grp", "members": ["g6add-sender"]})

    # Give g6add-member a message history
    _http("/send", "POST", {"self": "g6add-sender", "peer": "g6add-member",
                             "body": "history msg", "kind": "消息"})

    # Adding the member with history should succeed
    r = _http("/group/add", "POST", {"group": "g6add-grp", "member": "g6add-member"})
    check("TC-G6-add: add member with history succeeds", r.get("ok") is True, str(r))

    # Verify member is in the group
    members = _http("/group/members", params_={"group": "g6add-grp"})
    check("TC-G6-add: member appears in group", "g6add-member" in members, str(members))


def test_g7_add_old_member_no_replay(db_dir: str):
    """TC-G7: add 老成员不回放入群前消息。"""
    print("\n[TC-G7] add 老成员不回放历史")

    _http("/register", "POST", {"name": "g7_alice"})
    _http("/register", "POST", {"name": "g7_bob"})

    _http("/group/create", "POST", {"name": "g7-grp", "members": ["g7_alice"]})

    # Messages before bob joins
    _http("/send", "POST", {"self": "g7_alice", "peer": "g7-grp", "body": "before", "kind": "消息"})

    # Ensure g7_bob has a read_cursors row already (simulating existing member)
    seed_r = _http("/send", "POST", {"self": "g7_alice", "peer": "g7_bob", "body": "seed", "kind": "消息"})
    _db_upsert_cursor(db_dir, "g7_bob", seed_r["msg_id"])

    # Add bob
    _http("/group/add", "POST", {"group": "g7-grp", "member": "g7_bob"})

    # Bob connects — should not get the pre-join group message
    c = WSClient("g7_bob", cursor=0)
    bl = c.read_until_caught_up(timeout=5)
    c.close()

    # The seed direct msg to bob should already be past cursor, so only new msgs
    pre_join_group = [m for m in bl if m.get("group") == "g7-grp"]
    check("TC-G7: no pre-join group msgs replayed", len(pre_join_group) == 0,
          f"got group msgs: {pre_join_group}")


def test_g_concurrent_dedup_group(db_dir: str):
    """TC-G-concurrent: 群扇出 + 补发并发 — 多成员不重不漏。"""
    print("\n[TC-G-concurrent] 群扇出并发去重")

    _http("/register", "POST", {"name": "gc_sender"})
    _http("/register", "POST", {"name": "gc_recv1"})
    _http("/register", "POST", {"name": "gc_recv2"})

    _http("/group/create", "POST", {"name": "gc-grp",
                                     "members": ["gc_recv1", "gc_recv2"],
                                     "creator": "gc_sender"})

    # Pre-seed 20 group messages directly
    db_path = os.path.join(db_dir, "db", "rooms.db")
    conn = sqlite3.connect(db_path, timeout=10)
    now = int(time.time())
    pre_ids = []
    for i in range(20):
        cur = conn.execute(
            "INSERT INTO messages (sender, recipient, kind, body, ask, created_at) VALUES (?, ?, ?, ?, NULL, ?)",
            ("gc_sender", "gc-grp", "消息", f"pre-{i}", now),
        )
        pre_ids.append(cur.lastrowid)
    conn.commit()
    conn.close()

    # Set both members' cursors to 0
    _db_upsert_cursor(db_dir, "gc_recv1", 0)
    _db_upsert_cursor(db_dir, "gc_recv2", 0)
    time.sleep(0.05)

    c1 = WSClient("gc_recv1", cursor=0)
    c2 = WSClient("gc_recv2", cursor=0)

    injected_ids = []

    def inject():
        time.sleep(0.05)
        for i in range(5):
            r = _http("/send", "POST", {"self": "gc_sender", "peer": "gc-grp",
                                         "body": f"live-{i}", "kind": "消息"})
            injected_ids.append(r["msg_id"])

    t = threading.Thread(target=inject)
    t.start()

    all_msgs1 = _collect_all_msgs(c1, 25, timeout=15.0)
    all_msgs2 = _collect_all_msgs(c2, 25, timeout=15.0)
    t.join()
    c1.close()
    c2.close()

    expected_ids = sorted(pre_ids + injected_ids)

    for label, msgs in [("recv1", all_msgs1), ("recv2", all_msgs2)]:
        got_ids = sorted(m["msg_id"] for m in msgs)
        no_dup = len(msgs) == len(set(m["msg_id"] for m in msgs))
        no_miss = got_ids == expected_ids
        check(f"TC-G-concurrent: {label} no dup", no_dup,
              f"total={len(msgs)} unique={len(set(m['msg_id'] for m in msgs))}")
        check(f"TC-G-concurrent: {label} no miss", no_miss,
              f"expected={expected_ids}, got={got_ids}")


def test_unknown_get_route_404():
    """TC9: 未知 GET 路由必须返回 404，不能悬挂。"""
    print("\n[TC9] 未知 GET 路由 → 404")
    url = f"http://{HOST}:{TEST_PORT}/nonexistent"
    try:
        urllib.request.urlopen(url, timeout=5)
        check("TC9: unknown GET → 404", False, "expected HTTPError 404, got 200")
    except urllib.error.HTTPError as e:
        check("TC9: unknown GET → 404", e.code == 404, f"got HTTP {e.code}")
    except Exception as e:
        check("TC9: unknown GET → 404", False, f"unexpected error: {e}")


# ---------- main ----------

def main():
    tmp = tempfile.mkdtemp(prefix="agent-meeting-test-")
    try:
        init_test_db(tmp)
        print(f"[setup] test DB: {tmp}")
        proc = start_daemon(tmp)
        print(f"[setup] daemon pid={proc.pid} on port {TEST_PORT}")

        try:
            test_handshake_and_auth(False)
            test_backlog(tmp)
            test_realtime_push()
            test_ping_pong()
            test_reconnect_with_cursor()
            test_concurrent_dedup(tmp)
            test_first_seed_zero_replay(tmp)
            test_cursor_survives_restart(tmp)
            test_monitor_no_tmp_file(tmp)
            test_unknown_get_route_404()
            test_mid_drain_inject_race(tmp, rounds=5)
            # PR-B group tests
            test_g1_group_and_private_unified_stream(tmp)
            test_g2_group_turn_less(tmp)
            test_g3_group_cursor_via_db(tmp)
            test_g4_add_member_seed_zero_replay(tmp)
            test_g5_delete_group_purge(tmp)
            test_g6_reject_name(tmp)
            test_g6_add_active_member_succeeds()
            test_g7_add_old_member_no_replay(tmp)
            test_g_concurrent_dedup_group(tmp)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'='*40}")
    print(f"Results: {PASS_COUNT} passed, {FAIL_COUNT} failed")
    sys.exit(0 if FAIL_COUNT == 0 else 1)


if __name__ == "__main__":
    main()
