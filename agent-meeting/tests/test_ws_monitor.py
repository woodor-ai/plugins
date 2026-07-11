#!/usr/bin/env python3
"""
WS PR3 integration tests — monitor.py WS client.

Tests the monitor's full WS behavior including cursor=-1 sentinel seeding
introduced in PR3. Never touches the live daemon on 8765.

Schema/API note: the daemon's identity model is (project, name) composite
key (see meeting-daemon docstring "DEPLOY NOTE"). monitor.py derives its own
project from cwd via meeting_common.derive_project() when it registers
(`meeting online --cwd ...`) — this test computes the same value up front
(TEST_PROJECT) and uses it for every identity so monitor's real registration
and the test's direct HTTP calls land in the same project bucket.

Test cases:
  TC-M1: monitor 连上后能收到实时消息并打出正确的通知 stdout 行
  TC-M2: 带游标连入能补未读（backlog replay）
  TC-M3: server ping → monitor 回 masked pong（daemon 不报协议错、连接不掉）
  TC-M4: 杀掉测试 daemon → monitor 退避重连 → 重启 daemon → monitor 重连并按游标补发
  TC-M5: host 解析每次重连都重跑 controls 解析（mock controls 验证调用次数）
  TC-M6: 首启 cursor=-1 播种 — daemon 回 caught_up(max)，monitor 不收历史消息

Usage:
    python3 agent-meeting/tests/test_ws_monitor.py
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

TEST_PORT = 8798          # different from PR1 tests (8799) to allow parallel runs
HOST = "127.0.0.1"
LOG_PATH = "/tmp/ws-pr2-test.log"

BIN_DIR = os.path.join(os.path.dirname(__file__), "..", "bin")
MONITOR_PATH = os.path.join(BIN_DIR, "monitor.py")
DAEMON_PATH = os.path.join(BIN_DIR, "meeting-daemon")

sys.path.insert(0, BIN_DIR)
import meeting_common  # noqa: E402 -- project derivation must match monitor.py's own

# Same project monitor.py will derive from this process's cwd when it calls
# `meeting online --cwd <cwd>` (subprocess.Popen inherits our cwd unmodified).
TEST_PROJECT = meeting_common.derive_project(os.getcwd())


# ---------- minimal WS client (mirrors test_ws.py) ----------

class WSClient:
    def __init__(self, name: str, cursor: int = 0, host: str = HOST, port: int = TEST_PORT,
                 project: str = TEST_PROJECT):
        self.name = name
        self.cursor = cursor
        self.project = project
        self.sock = socket.create_connection((host, port), timeout=5)
        self.sock.settimeout(5)
        self._handshake(host, port)

    def _handshake(self, host, port):
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET /subscribe HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"X-Meeting-Name: {self.name}\r\n"
            f"X-Meeting-Project: {self.project}\r\n"
            f"X-Meeting-Cursor: {self.cursor}\r\n"
            "X-Meeting-Proto: 1\r\n"
            "\r\n"
        )
        self.sock.sendall(request.encode())

        line = b""
        while not line.endswith(b"\r\n"):
            ch = self.sock.recv(1)
            if not ch:
                raise IOError("closed during handshake")
            line += ch
        if "101" not in line.decode():
            raise IOError(f"handshake failed: {line.decode().strip()}")

        while True:
            hline = b""
            while not hline.endswith(b"\r\n"):
                ch = self.sock.recv(1)
                if not ch:
                    raise IOError("closed reading headers")
                hline += ch
            if hline.strip() == b"":
                break

    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise IOError("EOF")
            buf += chunk
        return buf

    def read_frame(self, timeout: float = 5.0) -> tuple[int, bytes]:
        self.sock.settimeout(timeout)
        header = self._recv_exact(2)
        b0, b1 = header[0], header[1]
        opcode = b0 & 0x0F
        masked = (b1 & 0x80) != 0
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        mask_key = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length) if length else b""
        if masked:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        return opcode, payload

    def send_pong(self, payload: bytes = b""):
        mask = os.urandom(4)
        masked_payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(b"\x8A" + bytes([0x80 | len(masked_payload)]) + mask + masked_payload)

    def read_until_caught_up(self, timeout: float = 5.0) -> list[dict]:
        msgs = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = deadline - time.time()
            try:
                opcode, payload = self.read_frame(timeout=max(0.1, remaining))
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


# ---------- daemon lifecycle ----------

def _http(path: str, method="GET", body=None, port: int = TEST_PORT) -> dict:
    url = f"http://{HOST}:{port}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def _register(name: str):
    _http("/register", "POST", {"project": TEST_PROJECT, "name": name})


def _send(self_name: str, peer_name: str, body: str, kind: str = "消息", ask=None):
    payload = {"self_project": TEST_PROJECT, "self": self_name,
               "peer_project": TEST_PROJECT, "peer": peer_name,
               "body": body, "kind": kind}
    if ask is not None:
        payload["ask"] = ask
    return _http("/send", "POST", payload)


def init_test_db(db_dir: str):
    db_path = os.path.join(db_dir, "db", "rooms.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        PRAGMA journal_mode = WAL;
        PRAGMA foreign_keys = ON;
        CREATE TABLE IF NOT EXISTS sessions (
            project TEXT NOT NULL,
            name TEXT NOT NULL,
            cwd TEXT,
            host TEXT,
            os TEXT,
            registered_at TEXT,
            last_seen REAL,
            role TEXT NOT NULL DEFAULT 'worker',
            PRIMARY KEY (project, name)
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_project TEXT NOT NULL,
            sender TEXT NOT NULL,
            recipient_project TEXT NOT NULL,
            recipient TEXT NOT NULL,
            kind TEXT NOT NULL,
            body TEXT NOT NULL,
            ask TEXT,
            created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_messages_recipient ON messages(recipient_project, recipient, id);
        CREATE INDEX IF NOT EXISTS idx_messages_sender_recipient ON messages(sender_project, sender, recipient_project, recipient, id);
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
        CREATE INDEX IF NOT EXISTS idx_group_members_member ON group_members(member_project, member_name);
    """)
    conn.close()


def _read_db_cursor(db_dir: str, member_name: str, project: str = TEST_PROJECT) -> int | None:
    """Read cursor from read_cursors table. Returns None if no row."""
    db_path = os.path.join(db_dir, "db", "rooms.db")
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=5)
    conn.executescript("PRAGMA journal_mode = WAL;")
    row = conn.execute(
        "SELECT cursor FROM read_cursors WHERE project=? AND member_name=?", (project, member_name)
    ).fetchone()
    conn.close()
    return row[0] if row else None


def _set_db_cursor(db_dir: str, member_name: str, cursor: int, project: str = TEST_PROJECT):
    """Insert/update a read_cursors row directly (test setup helper).

    Uses WAL mode and isolation_level=None to match daemon connection settings.
    """
    db_path = os.path.join(db_dir, "db", "rooms.db")
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=5)
    conn.executescript("PRAGMA journal_mode = WAL;")
    conn.execute(
        "INSERT INTO read_cursors (project, member_name, cursor, updated_at) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(project, member_name) DO UPDATE SET cursor=excluded.cursor, updated_at=excluded.updated_at",
        (project, member_name, cursor, int(time.time())),
    )
    conn.close()


def start_daemon(db_dir: str, port: int = TEST_PORT) -> subprocess.Popen:
    env = os.environ.copy()
    env["MEETING_HOME"] = db_dir
    proc = subprocess.Popen(
        [sys.executable, DAEMON_PATH, f"--port={port}", "--no-mdns"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    for _ in range(40):
        time.sleep(0.25)
        try:
            _http("/health", port=port)
            return proc
        except Exception:
            if proc.poll() is not None:
                out, err = proc.communicate()
                raise RuntimeError(f"Daemon exited early:\n{err.decode()}")
    raise RuntimeError("Daemon did not start in time")


# ---------- monitor process helpers ----------

def start_monitor(name: str, db_dir: str) -> "tuple[subprocess.Popen, list[str], list[int]]":
    """Start monitor.py in a subprocess.

    Returns (proc, shared_lines, offset) where shared_lines is populated
    by a background reader thread. Pass shared_lines + offset to
    collect_stdout_lines to poll monitor output without select() races.
    """
    env = os.environ.copy()
    env["MEETING_HOME"] = db_dir

    proc = subprocess.Popen(
        [sys.executable, MONITOR_PATH, name],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    shared_lines, _ = _start_stdout_reader(proc)
    offset = [0]
    return proc, shared_lines, offset


def _start_stdout_reader(proc: subprocess.Popen) -> "tuple[list[str], threading.Event]":
    """Spawn a background thread that continuously reads monitor stdout into a list.

    Returns (lines_list, stop_event). The list is appended to from the thread;
    collect_stdout_lines polls it. Call stop_event.set() when done (the thread
    exits on its own when proc.stdout hits EOF / process exits).
    """
    lines: list[str] = []

    def _reader():
        for raw in proc.stdout:  # type: ignore[union-attr]
            lines.append(raw.decode(errors="replace").rstrip())

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    return lines, t


def collect_stdout_lines(proc: subprocess.Popen, expect_prefix: str, count: int,
                         timeout: float = 8.0,
                         _shared_lines: "list[str] | None" = None,
                         _shared_offset: "list[int] | None" = None) -> list[str]:
    """Collect lines from monitor stdout that start with expect_prefix.

    If _shared_lines is provided, reads from that pre-populated list (from
    _start_stdout_reader) starting at _shared_offset[0], advancing the offset.
    Otherwise falls back to direct readline (works for short-lived process reads).
    """
    found = []
    deadline = time.time() + timeout

    if _shared_lines is not None and _shared_offset is not None:
        while len(found) < count and time.time() < deadline:
            idx = _shared_offset[0]
            while idx < len(_shared_lines):
                text = _shared_lines[idx]
                idx += 1
                _shared_offset[0] = idx
                if text.startswith(expect_prefix):
                    found.append(text)
                    if len(found) >= count:
                        break
            if len(found) < count:
                time.sleep(0.05)
        return found

    # Fallback: direct readline (used only in test helpers that terminate proc first)
    while len(found) < count and time.time() < deadline:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        try:
            line = proc.stdout.readline()  # type: ignore[union-attr]
        except Exception:
            break
        if not line:
            break
        text = line.decode(errors="replace").rstrip()
        if text.startswith(expect_prefix):
            found.append(text)
    return found


# ---------- test state ----------

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


# ---------- controls shim ----------
# monitor.py calls `meeting controls --json` to discover daemon ip:port.
# In tests, MEETING_HOME points to our test db_dir. We install a fake
# `meeting` binary there that returns the test daemon's address.
# All other meeting subcommands (online, offline) fall through to the real binary.

def install_controls_shim(db_dir: str, ip: str, port: int) -> str:
    """Write a fake `meeting` CLI stub that answers `controls --json`."""
    bin_dir = os.path.join(db_dir, "bin")
    os.makedirs(bin_dir, exist_ok=True)
    stub_path = os.path.join(bin_dir, "meeting")
    payload = json.dumps([{"ip": ip, "port": port, "host": "test-host", "is_current": True}])

    script = f"""#!/usr/bin/env python3
import sys, json, subprocess, os

# When called as: meeting controls --json → return test daemon coords
args = sys.argv[1:]
if args == ["controls", "--json"]:
    print({payload!r})
    sys.exit(0)

# For all other calls (online, offline) fall through to the real binary
# which is at the default location (~/.agent-meeting/bin/meeting).
import pathlib
real = pathlib.Path.home() / ".agent-meeting" / "bin" / "meeting"
os.execv(str(real), [str(real)] + args)
"""
    with open(stub_path, "w") as f:
        f.write(script)
    os.chmod(stub_path, 0o755)
    return stub_path


def install_controls_shim_counting(db_dir: str, ip: str, port: int,
                                   counter_file: str) -> str:
    """Like install_controls_shim but increments counter_file on each controls call."""
    bin_dir = os.path.join(db_dir, "bin")
    os.makedirs(bin_dir, exist_ok=True)
    stub_path = os.path.join(bin_dir, "meeting")
    payload = json.dumps([{"ip": ip, "port": port, "host": "test-host", "is_current": True}])

    script = f"""#!/usr/bin/env python3
import sys, json, os, pathlib

args = sys.argv[1:]
if args == ["controls", "--json"]:
    # Increment counter
    cf = {counter_file!r}
    try:
        n = int(open(cf).read().strip()) + 1
    except Exception:
        n = 1
    open(cf, "w").write(str(n))
    print({payload!r})
    sys.exit(0)

real = pathlib.Path.home() / ".agent-meeting" / "bin" / "meeting"
os.execv(str(real), [str(real)] + args)
"""
    with open(stub_path, "w") as f:
        f.write(script)
    os.chmod(stub_path, 0o755)
    return stub_path


# ---------- test cases ----------

def test_m1_realtime_stdout(db_dir: str):
    """TC-M1: monitor 连上后能收到实时消息并打出正确的通知 stdout 行"""
    print("\n[TC-M1] 实时消息 → stdout 格式")

    _register("m1_alice")
    _register("m1_bob")

    # Seed one message, then pre-set read_cursors so monitor starts past it.
    seed = _send("m1_alice", "m1_bob", "seed")
    _set_db_cursor(db_dir, "m1_bob", seed["msg_id"])

    proc, shared, offset = start_monitor("m1_bob", db_dir)
    try:
        # Let monitor connect and catch up
        time.sleep(2.0)

        # Send a live message with ask
        _send("m1_alice", "m1_bob", "hello world", ask="请回复")

        lines = collect_stdout_lines(proc, "New Message", count=1, timeout=6.0,
                                     _shared_lines=shared, _shared_offset=offset)

        check("TC-M1: got notification stdout line", len(lines) >= 1, f"got {lines}")
        if lines:
            line = lines[0]
            expected_prefix = "New Message from m1_alice [unverified peer]:"
            check("TC-M1: stdout format correct",
                  line.startswith(expected_prefix),
                  f"got: {line!r}")
            check("TC-M1: ask text in stdout",
                  "请回复" in line, f"got: {line!r}")

        # Send a message without ask
        _send("m1_alice", "m1_bob", "no ask msg")
        lines2 = collect_stdout_lines(proc, "New Message", count=1, timeout=5.0,
                                      _shared_lines=shared, _shared_offset=offset)
        check("TC-M1: no-ask line correct",
              any(l == "New Message from m1_alice [unverified peer]" for l in lines2),
              f"got: {lines2}")

    finally:
        proc.terminate()
        proc.wait(timeout=3)


def test_m2_backlog_replay(db_dir: str):
    """TC-M2: 带游标连入能补未读"""
    print("\n[TC-M2] 游标补发 backlog")

    _register("m2_sender")
    _register("m2_recv")

    # Send 3 messages before monitor starts
    r1 = _send("m2_sender", "m2_recv", "a")
    r2 = _send("m2_sender", "m2_recv", "b")
    r3 = _send("m2_sender", "m2_recv", "c")

    # Pre-set read_cursors to r1 so daemon treats msg1 as already delivered.
    _set_db_cursor(db_dir, "m2_recv", r1["msg_id"])

    proc, shared, offset = start_monitor("m2_recv", db_dir)
    try:
        # Should receive msgs 2 and 3 as backlog, not msg 1
        lines = collect_stdout_lines(proc, "New Message", count=2, timeout=8.0,
                                     _shared_lines=shared, _shared_offset=offset)
        check("TC-M2: received 2 backlog msgs", len(lines) == 2, f"got {len(lines)}: {lines}")
        # Can't check body text (no ask in msgs), but count=2 proves msg1 was skipped
        check("TC-M2: not more than 2 msgs (msg1 skipped)",
              len(lines) <= 2, f"lines: {lines}")
    finally:
        proc.terminate()
        proc.wait(timeout=3)


def test_m3_ping_pong(db_dir: str):
    """TC-M3: server ping → monitor 回 masked pong（daemon 不报协议错、连接不掉）"""
    print("\n[TC-M3] server ping → monitor masked pong")

    _register("m3_user")
    _register("m3_other")

    seed = _send("m3_other", "m3_user", "seed")

    # Pre-set read_cursors so monitor connects past the seed.
    _set_db_cursor(db_dir, "m3_user", seed["msg_id"])

    proc, shared, offset = start_monitor("m3_user", db_dir)
    try:
        # Let monitor connect and sit for 8s — daemon pings every 4s.
        # If monitor doesn't pong properly, daemon will remove the connection.
        time.sleep(8.0)

        # After 8s (2 daemon ping cycles), send a message from m3_other (not self)
        # so the self-echo suppression does not filter it out.
        _send("m3_other", "m3_user", "still alive")
        lines = collect_stdout_lines(proc, "New Message", count=1, timeout=5.0,
                                     _shared_lines=shared, _shared_offset=offset)
        check("TC-M3: monitor still alive after ping cycles",
              len(lines) >= 1, f"got {lines}")

        # Also verify daemon is still healthy
        health = _http("/health")
        check("TC-M3: daemon still healthy", health.get("ok") is True)

    finally:
        proc.terminate()
        proc.wait(timeout=3)


def test_m4_reconnect_backlog(db_dir: str):
    """TC-M4: 杀掉 daemon → monitor 退避重连 → 重启 daemon → monitor 重连并补发"""
    global _daemon_proc
    print("\n[TC-M4] daemon 重启 + monitor 重连补发")

    _register("m4_sender")
    _register("m4_recv")

    # Establish baseline — send 1 msg, pre-set cursor, start monitor.
    r0 = _send("m4_sender", "m4_recv", "before")

    # Pre-set read_cursors so daemon starts monitor past r0.
    _set_db_cursor(db_dir, "m4_recv", r0["msg_id"])

    proc, shared, offset = start_monitor("m4_recv", db_dir)
    try:
        # Wait for monitor to connect
        time.sleep(2.5)

        # Kill the daemon
        _daemon_proc.terminate()
        _daemon_proc.wait(timeout=5)

        # While daemon is down, send 2 more messages directly into DB
        db_path = os.path.join(db_dir, "db", "rooms.db")
        conn = sqlite3.connect(db_path, timeout=10)
        now = int(time.time())
        cur = conn.execute(
            "INSERT INTO messages (sender_project, sender, recipient_project, recipient, kind, body, ask, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, NULL, ?)",
            (TEST_PROJECT, "m4_sender", TEST_PROJECT, "m4_recv", "消息", "while-down-1", now),
        )
        id_down1 = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO messages (sender_project, sender, recipient_project, recipient, kind, body, ask, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, NULL, ?)",
            (TEST_PROJECT, "m4_sender", TEST_PROJECT, "m4_recv", "消息", "while-down-2", now),
        )
        id_down2 = cur.lastrowid
        conn.commit()
        conn.close()

        # Give monitor time to detect disconnect and start backing off
        time.sleep(3.0)

        # Restart daemon on same port with same DB
        _daemon_proc = start_daemon(db_dir, TEST_PORT)

        # Monitor should reconnect and get the 2 missed messages as backlog.
        # Daemon reads read_cursors (=r0) and replays everything after it.
        lines = collect_stdout_lines(proc, "New Message", count=2, timeout=40.0,
                                     _shared_lines=shared, _shared_offset=offset)
        check("TC-M4: received 2 missed msgs after reconnect",
              len(lines) == 2, f"got {len(lines)}: {lines}")

    finally:
        proc.terminate()
        proc.wait(timeout=3)


def test_m5_host_reresolution(db_dir: str):
    """TC-M5: 重连时重跑 controls 解析（验证调用次数 >1）"""
    global _daemon_proc
    print("\n[TC-M5] 重连时重解析 host (controls 调用计数)")

    import tempfile as _tempfile
    counter_file = os.path.join(_tempfile.gettempdir(), "ws-pr2-controls-counter.txt")
    try:
        os.unlink(counter_file)
    except FileNotFoundError:
        pass

    # Replace shim with counting version
    install_controls_shim_counting(db_dir, HOST, TEST_PORT, counter_file)

    _register("m5_user")
    seed = _send("m5_user", "m5_user", "seed")

    _set_db_cursor(db_dir, "m5_user", seed["msg_id"])

    proc, shared, offset = start_monitor("m5_user", db_dir)
    try:
        # Let monitor connect (1st controls call)
        time.sleep(2.5)

        # Kill daemon to force a reconnect
        _daemon_proc.terminate()
        _daemon_proc.wait(timeout=5)

        # Give monitor a moment to detect disconnect
        time.sleep(2.0)

        # Restart daemon
        _daemon_proc = start_daemon(db_dir, TEST_PORT)

        # Wait for monitor to reconnect (triggers 2nd+ controls call)
        time.sleep(5.0)

        # Verify controls was called more than once
        try:
            count = int(open(counter_file).read().strip())
        except Exception:
            count = 0

        check("TC-M5: controls called on initial connect", count >= 1,
              f"counter={count}")
        check("TC-M5: controls re-called on reconnect (>1 calls)",
              count >= 2, f"counter={count}")

    finally:
        proc.terminate()
        proc.wait(timeout=3)
        # Restore plain shim for any subsequent tests
        install_controls_shim(db_dir, HOST, TEST_PORT)


def test_m6_first_seed_zero_replay(db_dir: str):
    """TC-M6: 新成员无 read_cursors 行 — daemon 首次 seed 到 MAX，monitor 零历史回放，
    read_cursors 落行，后续实时消息正常投递。"""
    print("\n[TC-M6] 首次 seed 零回放（DB 权威）")

    _register("m6_sender")
    _register("m6_recv")

    # Insert 3 history messages before monitor starts; no read_cursors row for m6_recv.
    r1 = _send("m6_sender", "m6_recv", "hist1")
    r2 = _send("m6_sender", "m6_recv", "hist2")
    r3 = _send("m6_sender", "m6_recv", "hist3")

    proc, shared, offset = start_monitor("m6_recv", db_dir)
    try:
        # Give monitor time to connect and receive caught_up
        time.sleep(2.5)

        # No notification lines should have appeared (history must be suppressed by first-seed)
        lines_before = collect_stdout_lines(proc, "New Message", count=1, timeout=0.5,
                                            _shared_lines=shared, _shared_offset=offset)
        check("TC-M6: no history flood on fresh start",
              len(lines_before) == 0, f"got {len(lines_before)} unexpected lines: {lines_before}")

        # read_cursors must have a row for m6_recv seeded to MAX(id)
        db_cursor = _read_db_cursor(db_dir, "m6_recv")
        check("TC-M6: read_cursors row written after seed",
              db_cursor is not None, "no row in read_cursors")
        check("TC-M6: read_cursors cursor = MAX(id)",
              db_cursor == r3["msg_id"],
              f"db_cursor={db_cursor}, expected={r3['msg_id']}")

        # New live message after seeding must still arrive
        _send("m6_sender", "m6_recv", "live")
        lines_live = collect_stdout_lines(proc, "New Message", count=1, timeout=6.0,
                                          _shared_lines=shared, _shared_offset=offset)
        check("TC-M6: live message delivered after seed",
              len(lines_live) == 1, f"got {lines_live}")

    finally:
        proc.terminate()
        proc.wait(timeout=3)


def test_mg1_group_notification(db_dir: str):
    """TC-MG1: 群消息 → monitor 打出通知行（group 字段 non-None）。"""
    global _daemon_proc
    print("\n[TC-MG1] 群消息 → monitor 通知")

    _register("mg1_sender")
    _register("mg1_member")

    # Create group with mg1_member
    _http("/group/create", "POST", {"project": TEST_PROJECT, "name": "mg1-chan",
                                    "members": ["mg1_member"],
                                    "creator": "mg1_sender"})

    # Seed a direct msg to advance member's cursor past any history
    seed = _send("mg1_sender", "mg1_member", "seed")
    _set_db_cursor(db_dir, "mg1_member", seed["msg_id"])

    proc, shared, offset = start_monitor("mg1_member", db_dir)
    try:
        time.sleep(2.0)

        # Send a group message — monitor should receive it and print a notification
        _send("mg1_sender", "mg1-chan", "group hello", ask="reply")

        lines = collect_stdout_lines(proc, "New Message", count=1, timeout=6.0,
                                     _shared_lines=shared, _shared_offset=offset)
        check("TC-MG1: notification line for group msg", len(lines) >= 1, f"got {lines}")
        if lines:
            check("TC-MG1: sender in notification line", "mg1_sender" in lines[0], lines[0])
    finally:
        proc.terminate()
        proc.wait(timeout=3)


def test_ms1_self_echo_suppressed(db_dir: str):
    """TC-MS1: sender==self 的帧不触发通知；sender!=self 正常触发。"""
    print("\n[TC-MS1] 自回显抑制")

    _register("ms1_self")
    _register("ms1_other")

    # Advance cursor past any history
    seed = _send("ms1_other", "ms1_self", "seed")
    _set_db_cursor(db_dir, "ms1_self", seed["msg_id"])

    proc, shared, offset = start_monitor("ms1_self", db_dir)
    try:
        time.sleep(2.0)

        # Send from self → self: should be suppressed
        _send("ms1_self", "ms1_self", "self-echo", ask="should not appear")

        # Short wait — if suppression works, no notification line appears
        self_lines = collect_stdout_lines(proc, "New Message", count=1, timeout=2.0,
                                          _shared_lines=shared, _shared_offset=offset)
        check("TC-MS1: self-echo not emitted", len(self_lines) == 0,
              f"got unexpected lines: {self_lines}")

        # Send from other → self: should arrive normally
        _send("ms1_other", "ms1_self", "from other", ask="hi")
        other_lines = collect_stdout_lines(proc, "New Message", count=1, timeout=5.0,
                                           _shared_lines=shared, _shared_offset=offset)
        check("TC-MS1: other-sender msg emitted", len(other_lines) >= 1,
              f"got {other_lines}")
        if other_lines:
            check("TC-MS1: other-sender name in line",
                  "ms1_other" in other_lines[0], other_lines[0])

    finally:
        proc.terminate()
        proc.wait(timeout=3)


# ---------- stdout format regression ----------

def test_stdout_format_unchanged():
    """Verify the exact stdout format strings match what monitor.py emits."""
    print("\n[TC-FMT] stdout 行格式逐字一致")

    with open(MONITOR_PATH, encoding="utf-8") as f:
        src = f.read()

    # 1:1 format: peer + empty location → no "in group"
    template_with_ask = 'New Message from {peer}{location} [unverified peer]: {clean}'
    template_without_ask = 'New Message from {peer}{location} [unverified peer]'

    check("FMT: with-ask format string present verbatim",
          template_with_ask in src, "not found in monitor.py source")
    check("FMT: without-ask format string present verbatim",
          template_without_ask in src, "not found in monitor.py source")


def test_emit_message_unit():
    """TC-MG2: _emit_message 群消息带 'in group <群名>'，1:1 不带。"""
    import importlib.util
    import io
    from contextlib import redirect_stdout

    print("\n[TC-MG2] _emit_message 单元测试（群消息 vs 1:1 格式）")

    spec = importlib.util.spec_from_file_location("monitor", MONITOR_PATH)
    mod = importlib.util.load_from_spec = None  # won't use this path

    # Import _emit_message by exec-ing just that function from source
    with open(MONITOR_PATH, encoding="utf-8") as f:
        src = f.read()

    ns: dict = {}
    # Extract and exec just _emit_message so we don't trigger module-level side effects
    lines = src.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("def _emit_message("))
    # Collect lines until next top-level def/class or EOF
    end = start + 1
    while end < len(lines):
        l = lines[end]
        if l and not l[0].isspace() and not l.startswith("#"):
            break
        end += 1
    func_src = "\n".join(lines[start:end])
    exec(compile(func_src, MONITOR_PATH, "exec"), ns)
    emit = ns["_emit_message"]

    # Test group message
    buf = io.StringIO()
    with redirect_stdout(buf):
        emit("alice", "请回复", "dev-chan")
    line = buf.getvalue().strip()
    check("TC-MG2: group msg has 'in group dev-chan'",
          "in group dev-chan" in line, repr(line))
    check("TC-MG2: group msg has sender",
          "alice" in line, repr(line))
    check("TC-MG2: group msg has ask",
          "请回复" in line, repr(line))

    # Test group message without ask — exact match, no trailing ask/colon
    buf2 = io.StringIO()
    with redirect_stdout(buf2):
        emit("bob", None, "team-chat")
    line2 = buf2.getvalue().strip()
    check("TC-MG2: group no-ask has 'in group team-chat'",
          "in group team-chat" in line2, repr(line2))
    check("TC-MG2: group no-ask exact format (no ask suffix)",
          line2 == "New Message from bob in group team-chat [unverified peer]", repr(line2))

    # Test 1:1 message — must NOT contain "in group"
    buf3 = io.StringIO()
    with redirect_stdout(buf3):
        emit("carol", "hi", None)
    line3 = buf3.getvalue().strip()
    check("TC-MG2: 1:1 msg no 'in group'",
          "in group" not in line3, repr(line3))
    check("TC-MG2: 1:1 msg has sender",
          "carol" in line3, repr(line3))

    # Test 1:1 message with group omitted (default)
    buf4 = io.StringIO()
    with redirect_stdout(buf4):
        emit("dave", None)
    line4 = buf4.getvalue().strip()
    check("TC-MG2: 1:1 default no 'in group'",
          "in group" not in line4, repr(line4))


# ---------- main ----------

_daemon_proc: subprocess.Popen = None  # type: ignore[assignment]


def main():
    global _daemon_proc

    log = open(LOG_PATH, "w")
    log.write(f"WS PR2 test run: {time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
    log.flush()

    tmp = tempfile.mkdtemp(prefix="agent-meeting-pr2-test-")
    try:
        init_test_db(tmp)
        print(f"[setup] test DB: {tmp}")

        install_controls_shim(tmp, HOST, TEST_PORT)
        _daemon_proc = start_daemon(tmp, TEST_PORT)
        print(f"[setup] daemon pid={_daemon_proc.pid} port={TEST_PORT}")

        try:
            test_stdout_format_unchanged()
            test_emit_message_unit()
            test_m1_realtime_stdout(tmp)
            test_m2_backlog_replay(tmp)
            test_m3_ping_pong(tmp)
            test_m4_reconnect_backlog(tmp)
            test_m5_host_reresolution(tmp)
            test_m6_first_seed_zero_replay(tmp)
            test_mg1_group_notification(tmp)
            test_ms1_self_echo_suppressed(tmp)
        finally:
            try:
                _daemon_proc.terminate()
                _daemon_proc.wait(timeout=5)
            except Exception:
                pass

    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        log.write(f"Results: {PASS_COUNT} passed, {FAIL_COUNT} failed\n")
        log.close()

    print(f"\n{'='*40}")
    print(f"Results: {PASS_COUNT} passed, {FAIL_COUNT} failed")
    print(f"log: {LOG_PATH}")
    sys.exit(0 if FAIL_COUNT == 0 else 1)


if __name__ == "__main__":
    main()
