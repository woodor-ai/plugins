"""
End-to-end test for the codex bridge (agent-meeting v0.8.41).

Drives a REAL codex-bridge.py subprocess against:
  - a fake agent-meeting control: a hand-rolled WS server standing in for the
    daemon's /subscribe endpoint (handshake + push a `type:msg` frame + answer
    ping with pong).
  - a fake codex app-server: a hand-rolled WS JSON-RPC server answering
    initialize / thread/resume / thread/read / turn/start (the methods
    codex-bridge.py's injection path actually calls).
  - a mock `meeting` CLI (a small python script) standing in for `meeting read`
    / `meeting online` / `meeting offline`, so no real daemon is needed.

Everything lives under a throwaway MEETING_HOME (tmp_path) -- never the dev
repo. No real daemon, no real codex binary, no real websockets server library
is required on the test's own interpreter (frames are hand-framed over raw
sockets); the bridge subprocess itself is run under the real agent-meeting
venv python since codex-bridge.py imports `websockets` for the app-server leg
-- the test is skipped if that venv isn't present.

Also covers meeting-say.py (codex outbound): a direct subprocess invocation
against the same mock `meeting` CLI, asserting the exact `send` invocation and
body-file contents.
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
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "agent-meeting"
BRIDGE_PY = PLUGIN / "codex" / "codex-bridge.py"
MEETING_SAY_PY = PLUGIN / "codex" / "meeting-say.py"
MEETING_COMMON_PY = PLUGIN / "bin" / "meeting_common.py"

if sys.platform.startswith("win"):
    VENV_PY = Path.home() / ".agent-meeting" / "venv" / "Scripts" / "python.exe"
else:
    VENV_PY = Path.home() / ".agent-meeting" / "venv" / "bin" / "python"


def _venv_has_websockets() -> bool:
    if not VENV_PY.exists():
        return False
    r = subprocess.run([str(VENV_PY), "-c", "import websockets"], capture_output=True)
    return r.returncode == 0


pytestmark = pytest.mark.skipif(
    not _venv_has_websockets(),
    reason="agent-meeting venv (with `websockets` installed) not bootstrapped on this machine",
)


# ---------------------------------------------------------------------------
# Minimal server-side WS framing (RFC 6455). meeting_common.ws_read_frame
# already handles reading both masked (client) and unmasked (server) frames,
# so it is reused here for reading; sending needs its own unmasked helper
# since meeting_common's sender always masks (correct only for the client
# role monitor.py/codex-bridge.py play).
# ---------------------------------------------------------------------------

sys.path.insert(0, str(PLUGIN / "bin"))
import meeting_common  # noqa: E402


def _ws_accept_key(client_key: str) -> str:
    return base64.b64encode(
        hashlib.sha1((client_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
    ).decode()


def _ws_send_unmasked(sock: socket.socket, opcode: int, payload: bytes) -> None:
    n = len(payload)
    if n < 126:
        header = struct.pack("!BB", 0x80 | opcode, n)
    elif n < 65536:
        header = struct.pack("!BBH", 0x80 | opcode, 126, n)
    else:
        header = struct.pack("!BBQ", 0x80 | opcode, 127, n)
    sock.sendall(header + payload)


def _read_handshake_headers(conn: socket.socket) -> dict:
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = conn.recv(4096)
        if not chunk:
            return {}
        buf += chunk
    head = buf.split(b"\r\n\r\n", 1)[0]
    headers = {}
    for line in head.split(b"\r\n")[1:]:
        if b":" in line:
            k, _, v = line.partition(b":")
            headers[k.strip().lower().decode()] = v.strip().decode()
    return headers


def _do_ws_handshake(conn: socket.socket) -> None:
    headers = _read_handshake_headers(conn)
    accept = _ws_accept_key(headers.get("sec-websocket-key", ""))
    resp = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
    )
    conn.sendall(resp.encode())


# ---------------------------------------------------------------------------
# Fake agent-meeting control: /subscribe endpoint the bridge connects to.
# ---------------------------------------------------------------------------

class FakeControlServer:
    def __init__(self):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self.host, self.port = self._srv.getsockname()
        self._conn = None
        self._conn_ready = threading.Event()
        self._stop = False
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self):
        self._srv.settimeout(1.0)
        while not self._stop:
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            _do_ws_handshake(conn)
            self._conn = conn
            self._conn_ready.set()
            self._serve(conn)
            return  # one connection is enough for this test

    def _serve(self, conn):
        conn.settimeout(1.0)
        while not self._stop:
            try:
                opcode, payload = meeting_common.ws_read_frame(conn)
            except socket.timeout:
                continue
            except (IOError, OSError):
                return
            if opcode == 0x9:  # ping -> pong
                _ws_send_unmasked(conn, 0xA, payload)
            elif opcode == 0x8:
                return

    def wait_connected(self, timeout=10):
        if not self._conn_ready.wait(timeout):
            raise TimeoutError("bridge never connected to the fake control")

    def push_msg(self, **fields):
        payload = json.dumps({"type": "msg", **fields}).encode()
        _ws_send_unmasked(self._conn, 0x1, payload)

    def close(self):
        self._stop = True
        for s in (self._conn, self._srv):
            try:
                s.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Fake codex app-server: JSON-RPC over WS, answering the methods
# codex-bridge.py's injection path calls.
# ---------------------------------------------------------------------------

class FakeAppServer:
    def __init__(self):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self.host, self.port = self._srv.getsockname()
        self.received: list = []
        self._stop = False
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    @property
    def ws_addr(self) -> str:
        return f"ws://{self.host}:{self.port}"

    def _accept_loop(self):
        self._srv.settimeout(1.0)
        while not self._stop:
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle_conn, args=(conn,), daemon=True).start()

    def _handle_conn(self, conn):
        _do_ws_handshake(conn)
        conn.settimeout(10.0)
        while not self._stop:
            try:
                opcode, payload = meeting_common.ws_read_frame(conn)
            except (socket.timeout, IOError, OSError):
                return
            if opcode == 0x8:
                return
            if opcode != 0x1:
                continue
            try:
                req = json.loads(payload.decode("utf-8"))
            except Exception:
                continue
            self.received.append(req)
            result = self._respond_for(req.get("method"), req.get("params") or {})
            resp = json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result}).encode()
            try:
                _ws_send_unmasked(conn, 0x1, resp)
            except OSError:
                return

    @staticmethod
    def _respond_for(method, params):
        if method == "thread/resume":
            return {"thread": {"id": params.get("threadId")}}
        if method == "thread/read":
            return {"thread": {"status": "idle"}}
        if method == "turn/start":
            return {"turn": {"id": "fake-turn-1"}}
        return {}  # initialize, thread/start, etc.

    def close(self):
        self._stop = True
        try:
            self._srv.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Mock `meeting` CLI: a plain python script driven entirely by env vars, so
# the test controls what `meeting read` returns without a real daemon.
# ---------------------------------------------------------------------------

_MOCK_MEETING_CLI = '''#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
log_path = os.environ.get("MOCK_MEETING_CALLS_LOG")
if log_path:
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(args) + "\\n")

if not args:
    sys.exit(1)

cmd = args[0]
if cmd in ("online", "offline"):
    sys.exit(0)
elif cmd == "read":
    # args: read <self> <peer/room> --since N --limit M
    peer = args[2] if len(args) > 2 else ""
    since = 0
    for i, a in enumerate(args):
        if a == "--since":
            since = int(args[i + 1])
    fixture_path = os.environ.get("MOCK_MEETING_READ_FIXTURE")
    # Fixture is a dict keyed by the exact peer/room arg the bridge passed --
    # this is what exposes a bare-name room key colliding with (or missing)
    # the project-qualified fixture key a real daemon would have kept apart.
    fixtures = json.loads(open(fixture_path, encoding="utf-8").read()) if fixture_path else {}
    rows = fixtures.get(peer, [])
    lines = []
    for row in rows:
        if row["id"] <= since:
            continue
        lines.append("\\t".join([
            str(row["id"]), str(row["created_at"]), row["sender_id"],
            row["kind"], row.get("ask", ""), row["body"],
        ]))
    print("\\n".join(lines))
    sys.exit(0)
elif cmd == "send":
    body_file = None
    for a in args:
        if a.startswith("--body-file="):
            body_file = a.split("=", 1)[1]
    out_path = os.environ.get("MOCK_MEETING_SEND_OUT")
    if out_path:
        body = open(body_file, encoding="utf-8").read() if body_file else ""
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"args": args, "body": body}, f)
    sys.exit(0)
else:
    sys.exit(0)
'''


def _write_mock_meeting_cli(bin_dir: Path) -> Path:
    p = bin_dir / "meeting"
    p.write_text(_MOCK_MEETING_CLI, encoding="utf-8")
    p.chmod(0o755)
    return p


def _install_meeting_common(bin_dir: Path) -> None:
    shutil.copyfile(str(MEETING_COMMON_PY), str(bin_dir / "meeting_common.py"))


# ---------------------------------------------------------------------------
# Test: bridge inbound -> `meeting read` -> inject into fake app-server ->
# cursor persisted.
# ---------------------------------------------------------------------------

def _setup_bridge_home(tmp_path: Path, session_name: str, fake_control: "FakeControlServer",
                       fake_app: "FakeAppServer") -> Path:
    """Lay out a throwaway MEETING_HOME with the mock `meeting` CLI + session
    mapping/runtime.json a real codex-bridge.py subprocess needs to start."""
    meeting_home = tmp_path / "meeting_home"
    bin_dir = meeting_home / "bin"
    codex_dir = meeting_home / "codex"
    for d in (bin_dir, codex_dir / "sessions", codex_dir / "cursors", codex_dir / "logs"):
        d.mkdir(parents=True)

    _write_mock_meeting_cli(bin_dir)
    _install_meeting_common(bin_dir)

    session_cwd = str(tmp_path / "project")
    Path(session_cwd).mkdir()

    (codex_dir / "runtime.json").write_text(json.dumps({
        "name": session_name,
        "ws_addr": fake_app.ws_addr,
        "control_url": f"http://{fake_control.host}:{fake_control.port}",
        "cwd": session_cwd,
    }), encoding="utf-8")
    (codex_dir / "sessions" / f"{session_name}.json").write_text(json.dumps({
        "name": session_name,
        "session_id": "fake-thread-1",
        "ws_addr": fake_app.ws_addr,
        "cwd": session_cwd,
    }), encoding="utf-8")
    return meeting_home


def test_bridge_inbound_to_injection_to_cursor_advance(tmp_path):
    fake_control = FakeControlServer()
    fake_app = FakeAppServer()
    session_name = "cx-e2e"
    meeting_home = _setup_bridge_home(tmp_path, session_name, fake_control, fake_app)
    codex_dir = meeting_home / "codex"

    calls_log = tmp_path / "meeting_calls.log"
    read_fixture = tmp_path / "read_fixture.json"
    now = int(time.time())
    read_fixture.write_text(json.dumps({
        "alice@someproj": [
            {"id": 1, "created_at": now, "sender_id": "alice", "kind": "消息",
             "ask": "", "body": "hello from alice"},
        ],
    }), encoding="utf-8")

    env = os.environ.copy()
    env["MEETING_HOME"] = str(meeting_home)
    env["MOCK_MEETING_CALLS_LOG"] = str(calls_log)
    env["MOCK_MEETING_READ_FIXTURE"] = str(read_fixture)

    proc = subprocess.Popen(
        [str(VENV_PY), str(BRIDGE_PY), session_name],
        env=env, cwd=str(tmp_path),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        fake_control.wait_connected(timeout=15)

        fake_control.push_msg(sender="alice", sender_project="someproj")

        cursor_file = codex_dir / "cursors" / f"{session_name}.json"
        deadline = time.time() + 30
        cursors = {}
        while time.time() < deadline:
            if cursor_file.exists():
                try:
                    cursors = json.loads(cursor_file.read_text(encoding="utf-8"))
                except Exception:
                    cursors = {}
                if cursors.get("alice@someproj") == 1:
                    break
            time.sleep(0.3)

        assert cursors.get("alice@someproj") == 1, (
            f"cursor did not advance to msg 1; got {cursors!r}. "
            f"bridge output:\n{_drain(proc)}"
        )

        turn_calls = [r for r in fake_app.received if r.get("method") == "turn/start"]
        assert len(turn_calls) == 1, f"expected 1 turn/start, got {turn_calls}"
        injected_text = turn_calls[0]["params"]["input"][0]["text"]
        assert injected_text == "[peer=alice msg_id=1] hello from alice", injected_text

        # `meeting read` was invoked with the project-qualified room, not a bare name.
        logged = [json.loads(line) for line in calls_log.read_text(encoding="utf-8").splitlines()]
        read_calls = [c for c in logged if c and c[0] == "read"]
        assert any("alice@someproj" in c for c in read_calls), read_calls
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        fake_control.close()
        fake_app.close()


# ---------------------------------------------------------------------------
# v0.10.0 phase 2 target#2: two live agents sharing a name across different
# projects must not collide into one room, and neither message may be lost.
# ---------------------------------------------------------------------------

def test_bridge_routes_same_name_different_project_senders_without_loss(tmp_path):
    fake_control = FakeControlServer()
    fake_app = FakeAppServer()
    session_name = "cx-e2e-dup"
    meeting_home = _setup_bridge_home(tmp_path, session_name, fake_control, fake_app)
    codex_dir = meeting_home / "codex"

    calls_log = tmp_path / "meeting_calls.log"
    read_fixture = tmp_path / "read_fixture.json"
    now = int(time.time())
    # Keyed by the exact room the bridge must pass to `meeting read` -- a bare
    # "alice" room key (the pre-fix behavior) matches NEITHER key below, which
    # is exactly how this test proves messages would otherwise be dropped.
    read_fixture.write_text(json.dumps({
        "alice@projA": [
            {"id": 1, "created_at": now, "sender_id": "alice@projA", "kind": "消息",
             "ask": "", "body": "hello from A"},
        ],
        "alice@projB": [
            {"id": 1, "created_at": now, "sender_id": "alice@projB", "kind": "消息",
             "ask": "", "body": "hello from B"},
        ],
    }), encoding="utf-8")

    env = os.environ.copy()
    env["MEETING_HOME"] = str(meeting_home)
    env["MOCK_MEETING_CALLS_LOG"] = str(calls_log)
    env["MOCK_MEETING_READ_FIXTURE"] = str(read_fixture)

    proc = subprocess.Popen(
        [str(VENV_PY), str(BRIDGE_PY), session_name],
        env=env, cwd=str(tmp_path),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        fake_control.wait_connected(timeout=15)

        fake_control.push_msg(sender="alice", sender_project="projA")
        fake_control.push_msg(sender="alice", sender_project="projB")

        cursor_file = codex_dir / "cursors" / f"{session_name}.json"
        deadline = time.time() + 30
        cursors = {}
        while time.time() < deadline:
            if cursor_file.exists():
                try:
                    cursors = json.loads(cursor_file.read_text(encoding="utf-8"))
                except Exception:
                    cursors = {}
                if cursors.get("alice@projA") == 1 and cursors.get("alice@projB") == 1:
                    break
            time.sleep(0.3)

        assert cursors.get("alice@projA") == 1 and cursors.get("alice@projB") == 1, (
            f"both same-name senders' messages must be delivered, got {cursors!r}. "
            f"bridge output:\n{_drain(proc)}"
        )

        turn_calls = [r for r in fake_app.received if r.get("method") == "turn/start"]
        injected = sorted(c["params"]["input"][0]["text"] for c in turn_calls)
        assert injected == ["[peer=alice msg_id=1] hello from A", "[peer=alice msg_id=1] hello from B"], (
            f"expected both peers' distinct bodies to be injected, got {injected}"
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        fake_control.close()
        fake_app.close()


def test_bridge_exits_loudly_on_frame_missing_sender_project(tmp_path):
    """A malformed inbound frame (no sender_project) must crash the bridge
    loudly instead of silently dropping the message and continuing."""
    fake_control = FakeControlServer()
    fake_app = FakeAppServer()
    session_name = "cx-e2e-bad-frame"
    meeting_home = _setup_bridge_home(tmp_path, session_name, fake_control, fake_app)

    env = os.environ.copy()
    env["MEETING_HOME"] = str(meeting_home)

    proc = subprocess.Popen(
        [str(VENV_PY), str(BRIDGE_PY), session_name],
        env=env, cwd=str(tmp_path),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        fake_control.wait_connected(timeout=15)

        # Push a frame with sender_project omitted entirely.
        fake_control.push_msg(sender="alice")

        try:
            rc = proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            out = _drain(proc)
            raise AssertionError(
                f"bridge did not exit after a malformed frame; it must fail loudly, "
                f"not keep running silently.\noutput:\n{out}"
            )
        assert rc != 0, f"bridge must exit non-zero on a malformed frame, got {rc}"
        out = (proc.stdout.read() or b"").decode(errors="replace")
        assert "missing sender/sender_project" in out, out
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            pass
        fake_control.close()
        fake_app.close()


def _drain(proc: subprocess.Popen) -> str:
    try:
        proc.terminate()
        out, _ = proc.communicate(timeout=3)
        return (out or b"").decode(errors="replace")
    except Exception:
        return "(could not drain bridge output)"


# ---------------------------------------------------------------------------
# Test: meeting-say.py outbound -- mocked `meeting send` invocation.
# ---------------------------------------------------------------------------

def test_meeting_say_outbound_invokes_send_with_body(tmp_path):
    meeting_home = tmp_path / "meeting_home"
    bin_dir = meeting_home / "bin"
    codex_dir = meeting_home / "codex"
    bin_dir.mkdir(parents=True)
    codex_dir.mkdir(parents=True)

    _write_mock_meeting_cli(bin_dir)

    (codex_dir / "runtime.json").write_text(json.dumps({
        "name": "cx-e2e-say",
        "control_url": "http://127.0.0.1:19999",
    }), encoding="utf-8")

    send_out = tmp_path / "send_out.json"
    env = os.environ.copy()
    env["MEETING_HOME"] = str(meeting_home)
    env["MOCK_MEETING_SEND_OUT"] = str(send_out)

    r = subprocess.run(
        [sys.executable, str(MEETING_SAY_PY), "bob", "hi", "there"],
        env=env, capture_output=True, text=True, timeout=15,
    )
    assert r.returncode == 0, f"meeting-say failed: {r.stdout}\n{r.stderr}"

    assert send_out.exists(), "mock `meeting send` was never invoked"
    payload = json.loads(send_out.read_text(encoding="utf-8"))
    assert payload["body"] == "hi there"
    assert payload["args"][0] == "send"
    assert payload["args"][1] == "cx-e2e-say"
    assert payload["args"][2] == "bob"
    assert "--host" in payload["args"] and "http://127.0.0.1:19999" in payload["args"]
