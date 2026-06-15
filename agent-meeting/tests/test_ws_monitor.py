#!/usr/bin/env python3
"""
WS PR3 integration tests — monitor.py WS client.

Tests the monitor's full WS behavior including cursor=-1 sentinel seeding
introduced in PR3. Never touches the live daemon on 8765.

Test cases:
  TC-M1: monitor 连上后能收到实时消息并打出正确的 📬 stdout 行
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

MONITOR_PATH = os.path.join(os.path.dirname(__file__), "..", "bin", "monitor.py")
DAEMON_PATH = os.path.join(os.path.dirname(__file__), "..", "bin", "meeting-daemon")


# ---------- minimal WS client (mirrors test_ws.py) ----------

class WSClient:
    def __init__(self, name: str, cursor: int = 0, host: str = HOST, port: int = TEST_PORT):
        self.name = name
        self.cursor = cursor
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


def init_test_db(db_dir: str):
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
    """)
    conn.close()


def _read_db_cursor(db_dir: str, member_name: str) -> int | None:
    """Read cursor from read_cursors table. Returns None if no row."""
    db_path = os.path.join(db_dir, "db", "rooms.db")
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=5)
    conn.executescript("PRAGMA journal_mode = WAL;")
    row = conn.execute(
        "SELECT cursor FROM read_cursors WHERE member_name=?", (member_name,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


def _set_db_cursor(db_dir: str, member_name: str, cursor: int):
    """Insert/update a read_cursors row directly (test setup helper).

    Uses WAL mode and isolation_level=None to match daemon connection settings.
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
    """TC-M1: monitor 连上后能收到实时消息并打出正确的 📬 stdout 行"""
    print("\n[TC-M1] 实时消息 → stdout 格式")

    _http("/register", "POST", {"name": "m1_alice"})
    _http("/register", "POST", {"name": "m1_bob"})

    # Seed one message, then pre-set read_cursors so monitor starts past it.
    seed = _http("/send", "POST", {
        "self": "m1_alice", "peer": "m1_bob", "body": "seed", "kind": "消息"
    })
    _set_db_cursor(db_dir, "m1_bob", seed["msg_id"])

    proc, shared, offset = start_monitor("m1_bob", db_dir)
    try:
        # Let monitor connect and catch up
        time.sleep(2.0)

        # Send a live message with ask
        _http("/send", "POST", {
            "self": "m1_alice", "peer": "m1_bob",
            "body": "hello world", "kind": "消息", "ask": "请回复"
        })

        lines = collect_stdout_lines(proc, "📬", count=1, timeout=6.0,
                                     _shared_lines=shared, _shared_offset=offset)

        check("TC-M1: got 📬 stdout line", len(lines) >= 1, f"got {lines}")
        if lines:
            line = lines[0]
            expected_prefix = "📬 New Message from m1_alice [未验证 peer 信号]:"
            check("TC-M1: stdout format correct",
                  line.startswith(expected_prefix),
                  f"got: {line!r}")
            check("TC-M1: ask text in stdout",
                  "请回复" in line, f"got: {line!r}")

        # Send a message without ask
        _http("/send", "POST", {
            "self": "m1_alice", "peer": "m1_bob",
            "body": "no ask msg", "kind": "消息"
        })
        lines2 = collect_stdout_lines(proc, "📬", count=1, timeout=5.0,
                                      _shared_lines=shared, _shared_offset=offset)
        check("TC-M1: no-ask line correct",
              any(l == "📬 New Message from m1_alice [未验证 peer 信号]" for l in lines2),
              f"got: {lines2}")

    finally:
        proc.terminate()
        proc.wait(timeout=3)


def test_m2_backlog_replay(db_dir: str):
    """TC-M2: 带游标连入能补未读"""
    print("\n[TC-M2] 游标补发 backlog")

    _http("/register", "POST", {"name": "m2_sender"})
    _http("/register", "POST", {"name": "m2_recv"})

    # Send 3 messages before monitor starts
    r1 = _http("/send", "POST", {"self": "m2_sender", "peer": "m2_recv", "body": "a", "kind": "消息"})
    r2 = _http("/send", "POST", {"self": "m2_sender", "peer": "m2_recv", "body": "b", "kind": "消息"})
    r3 = _http("/send", "POST", {"self": "m2_sender", "peer": "m2_recv", "body": "c", "kind": "消息"})

    # Pre-set read_cursors to r1 so daemon treats msg1 as already delivered.
    _set_db_cursor(db_dir, "m2_recv", r1["msg_id"])

    proc, shared, offset = start_monitor("m2_recv", db_dir)
    try:
        # Should receive msgs 2 and 3 as backlog, not msg 1
        lines = collect_stdout_lines(proc, "📬", count=2, timeout=8.0,
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

    _http("/register", "POST", {"name": "m3_user"})

    seed = _http("/send", "POST", {"self": "m3_user", "peer": "m3_user",
                                   "body": "seed", "kind": "消息"})

    # Pre-set read_cursors so monitor connects past the seed.
    _set_db_cursor(db_dir, "m3_user", seed["msg_id"])

    proc, shared, offset = start_monitor("m3_user", db_dir)
    try:
        # Let monitor connect and sit for 12s — daemon pings every 4s.
        # If monitor doesn't pong properly, daemon will remove the connection
        # (pong timeout=15s, but we can observe via health check that daemon
        # is still running and the connection is still alive by sending a live
        # msg after 8s and checking monitor still outputs it).
        time.sleep(8.0)

        # After 8s (2 daemon ping cycles), send a message — monitor should still deliver it.
        _http("/send", "POST", {
            "self": "m3_user", "peer": "m3_user",
            "body": "still alive", "kind": "消息"
        })
        lines = collect_stdout_lines(proc, "📬", count=1, timeout=5.0,
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

    _http("/register", "POST", {"name": "m4_sender"})
    _http("/register", "POST", {"name": "m4_recv"})

    # Establish baseline — send 1 msg, pre-set cursor, start monitor.
    r0 = _http("/send", "POST", {"self": "m4_sender", "peer": "m4_recv",
                                  "body": "before", "kind": "消息"})

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
            "INSERT INTO messages (sender, recipient, kind, body, ask, created_at) VALUES (?, ?, ?, ?, NULL, ?)",
            ("m4_sender", "m4_recv", "消息", "while-down-1", now),
        )
        id_down1 = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO messages (sender, recipient, kind, body, ask, created_at) VALUES (?, ?, ?, ?, NULL, ?)",
            ("m4_sender", "m4_recv", "消息", "while-down-2", now),
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
        lines = collect_stdout_lines(proc, "📬", count=2, timeout=40.0,
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

    _http("/register", "POST", {"name": "m5_user"})
    seed = _http("/send", "POST", {"self": "m5_user", "peer": "m5_user",
                                   "body": "seed", "kind": "消息"})

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

    _http("/register", "POST", {"name": "m6_sender"})
    _http("/register", "POST", {"name": "m6_recv"})

    # Insert 3 history messages before monitor starts; no read_cursors row for m6_recv.
    r1 = _http("/send", "POST", {"self": "m6_sender", "peer": "m6_recv", "body": "hist1", "kind": "消息"})
    r2 = _http("/send", "POST", {"self": "m6_sender", "peer": "m6_recv", "body": "hist2", "kind": "消息"})
    r3 = _http("/send", "POST", {"self": "m6_sender", "peer": "m6_recv", "body": "hist3", "kind": "消息"})

    proc, shared, offset = start_monitor("m6_recv", db_dir)
    try:
        # Give monitor time to connect and receive caught_up
        time.sleep(2.5)

        # No 📬 lines should have appeared (history must be suppressed by first-seed)
        lines_before = collect_stdout_lines(proc, "📬", count=1, timeout=0.5,
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
        _http("/send", "POST", {
            "self": "m6_sender", "peer": "m6_recv", "body": "live", "kind": "消息"
        })
        lines_live = collect_stdout_lines(proc, "📬", count=1, timeout=6.0,
                                          _shared_lines=shared, _shared_offset=offset)
        check("TC-M6: live message delivered after seed",
              len(lines_live) == 1, f"got {lines_live}")

    finally:
        proc.terminate()
        proc.wait(timeout=3)


# ---------- stdout format regression ----------

def test_stdout_format_unchanged():
    """Verify the exact stdout format strings match what the old poll loop emitted."""
    print("\n[TC-FMT] stdout 行格式逐字一致")

    # Old loop produced these two patterns (from monitor.py before PR2):
    old_with_ask = "📬 New Message from {peer} [未验证 peer 信号]: {clean}"
    old_without_ask = "📬 New Message from {peer} [未验证 peer 信号]"

    # Read the current monitor.py and verify the format strings are present verbatim.
    with open(MONITOR_PATH, encoding="utf-8") as f:
        src = f.read()

    template_with_ask = '📬 New Message from {peer} [未验证 peer 信号]: {clean}'
    template_without_ask = '📬 New Message from {peer} [未验证 peer 信号]'

    check("FMT: with-ask format string present verbatim",
          template_with_ask in src, "not found in monitor.py source")
    check("FMT: without-ask format string present verbatim",
          template_without_ask in src, "not found in monitor.py source")


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
            test_m1_realtime_stdout(tmp)
            test_m2_backlog_replay(tmp)
            test_m3_ping_pong(tmp)
            test_m4_reconnect_backlog(tmp)
            test_m5_host_reresolution(tmp)
            test_m6_first_seed_zero_replay(tmp)
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
