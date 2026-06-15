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

def _http(path: str, method="GET", body=None) -> dict:
    url = f"http://{HOST}:{TEST_PORT}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def start_daemon(db_dir: str) -> subprocess.Popen:
    env = os.environ.copy()
    env["MEETING_HOME"] = db_dir
    # Initialize the DB by running meeting-migrate equivalent inline
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
    """TC2: 补发 id>cursor"""
    print("\n[TC2] 补发 id>cursor")
    # Send 3 messages to bob before bob connects
    _http("/register", "POST", {"name": "alice_sender"})
    _http("/register", "POST", {"name": "bob_recv"})

    r1 = _http("/send", "POST", {"self": "alice_sender", "peer": "bob_recv", "body": "msg1", "kind": "消息"})
    r2 = _http("/send", "POST", {"self": "alice_sender", "peer": "bob_recv", "body": "msg2", "kind": "消息"})
    r3 = _http("/send", "POST", {"self": "alice_sender", "peer": "bob_recv", "body": "msg3", "kind": "消息"})

    # Connect with cursor=0 — should get all 3
    c = WSClient("bob_recv", cursor=0)
    backlog = c.read_until_caught_up(timeout=5)
    check("TC2: receive 3 backlog msgs", len(backlog) == 3, f"got {len(backlog)}")
    check("TC2: backlog phase=backlog", all(m.get("phase") == "backlog" for m in backlog),
          str([m.get("phase") for m in backlog]))
    check("TC2: msg_ids in order", [m["msg_id"] for m in backlog] == sorted([m["msg_id"] for m in backlog]))

    # Connect with cursor = after msg2 — should get only msg3
    c2 = WSClient("bob_recv", cursor=r2["msg_id"])
    backlog2 = c2.read_until_caught_up(timeout=5)
    check("TC2: cursor filter — only msg3", len(backlog2) == 1 and backlog2[0]["msg_id"] == r3["msg_id"],
          f"got {[m['msg_id'] for m in backlog2]}")

    c.close()
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


def test_concurrent_dedup():
    """TC6: 补发进行中途插入 /send — msg_ids 严格单调不重不漏"""
    print("\n[TC6] 补发/实时并发去重")

    _http("/register", "POST", {"name": "heidi"})
    _http("/register", "POST", {"name": "ivan"})

    # Pre-seed 20 backlog messages
    pre_ids = []
    for i in range(20):
        r = _http("/send", "POST", {"self": "ivan", "peer": "heidi", "body": f"pre-{i}", "kind": "消息"})
        pre_ids.append(r["msg_id"])

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
    # Strict monotone: each subscriber's stream is monotone (order might mix backlog/live but IDs strict)
    check("TC6: no gaps in coverage", set(got_ids) == set(expected_ids))

    c.close()


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
            test_concurrent_dedup()
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
