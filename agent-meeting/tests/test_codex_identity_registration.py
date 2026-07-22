"""
Regression tests: codex-side registration honors the SAME authoritative
project-identity gate as monitor.py / `meeting online` CLI (docs/contracts/
0.10.0-composite-key-identity.md, codex-side parity pass).

Covers:
  T1 - codex-register.py (SessionStart hook): a repo root with NO cached
       --proj declaration -> `meeting online` inside the hook is refused
       (exit 4 / missing_project_identity). The hook's own additionalContext
       reports "not bridged" (never the success wording), and no session row
       is ever written.
  T2 - codex-meeting.py's --proj entrypoint writes the SAME per-root cache
       `meeting online --proj` writes (drives the real code path; Launcher.
       setup() is stubbed out so no real codex/app-server process is spawned).
  T3 - codex-register.py, same repo root as T2 (cache hit via --cwd only,
       no --proj passed by the hook) -> registration succeeds, sessions.
       project equals the DECLARED value -- never a path-derived string.
  T4 - codex-bridge.py: missing identity (exit 4) is fatal at startup;
       `meeting online` is invoked exactly once (no retry loop), and never
       carries a --proj flag (constraint: no path-derivation smuggled in).
  T5 - codex-bridge.py: a transient registration failure (exit 1, not in
       the no-retry set) is NOT fatal -- the bridge stays alive and retries
       `meeting online` again on every WS reconnect.

Uses a real `meeting-daemon` + the source tree's `bin/meeting` CLI (never the
machine's installed ~/.agent-meeting), all under a throwaway MEETING_HOME.
T4/T5 additionally spawn a real codex-bridge.py subprocess and are skipped if
the local agent-meeting venv (`websockets`) is not bootstrapped -- same skip
convention as test_e2e_bridge.py.
"""

import importlib.util
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
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "agent-meeting"
BIN_DIR = PLUGIN / "bin"
CODEX_DIR_SRC = PLUGIN / "codex"
MEETING_PATH = BIN_DIR / "meeting"
DAEMON_PATH = BIN_DIR / "meeting-daemon"
MEETING_COMMON_PATH = BIN_DIR / "meeting_common.py"
CODEX_REGISTER_PY = CODEX_DIR_SRC / "codex-register.py"
CODEX_MEETING_PY = CODEX_DIR_SRC / "codex-meeting.py"
BRIDGE_PY = CODEX_DIR_SRC / "codex-bridge.py"

if sys.platform.startswith("win"):
    VENV_PY = Path.home() / ".agent-meeting" / "venv" / "Scripts" / "python.exe"
else:
    VENV_PY = Path.home() / ".agent-meeting" / "venv" / "bin" / "python"


def _venv_has_websockets() -> bool:
    if not VENV_PY.exists():
        return False
    r = subprocess.run([str(VENV_PY), "-c", "import websockets"], capture_output=True)
    return r.returncode == 0


requires_venv = pytest.mark.skipif(
    not _venv_has_websockets(),
    reason="agent-meeting venv (with `websockets` installed) not bootstrapped on this machine",
)

TEST_PORT = 8795  # distinct from other tests' ports (8765 live, 8796-8799 test suite)
HOST_URL = f"http://127.0.0.1:{TEST_PORT}"

sys.path.insert(0, str(BIN_DIR))
import meeting_common  # noqa: E402


# ---------------------------------------------------------------------------
# Real daemon + real `meeting` CLI under a throwaway MEETING_HOME.
# ---------------------------------------------------------------------------

def _install_real_meeting_cli(meeting_home: Path) -> None:
    dst = meeting_home / "bin"
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copy2(MEETING_PATH, dst / "meeting")
    shutil.copy2(MEETING_COMMON_PATH, dst / "meeting_common.py")
    os.chmod(dst / "meeting", 0o755)


def _start_daemon(meeting_home: Path) -> subprocess.Popen:
    db_dir = meeting_home / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(str(db_dir / "rooms.db")).close()

    env = os.environ.copy()
    env["MEETING_HOME"] = str(meeting_home)
    proc = subprocess.Popen(
        [sys.executable, str(DAEMON_PATH), f"--port={TEST_PORT}", "--no-mdns"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    for _ in range(40):
        time.sleep(0.25)
        try:
            with urllib.request.urlopen(f"{HOST_URL}/health", timeout=5) as r:
                json.loads(r.read())
            return proc
        except Exception:
            if proc.poll() is not None:
                _, err = proc.communicate()
                raise RuntimeError(f"daemon exited early:\n{err.decode()}")
    raise RuntimeError("daemon did not start within 10s")


def _sessions_row(meeting_home: Path, project: str, name: str):
    conn = sqlite3.connect(str(meeting_home / "db" / "rooms.db"), timeout=5)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM sessions WHERE project=? AND name=?", (project, name)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def _all_session_names(meeting_home: Path) -> set:
    conn = sqlite3.connect(str(meeting_home / "db" / "rooms.db"), timeout=5)
    rows = conn.execute("SELECT project, name FROM sessions").fetchall()
    conn.close()
    return set(rows)


@pytest.fixture(scope="module")
def daemon_env(tmp_path_factory):
    meeting_home = tmp_path_factory.mktemp("codex-identity-home")
    _install_real_meeting_cli(meeting_home)
    proc = _start_daemon(meeting_home)
    try:
        yield meeting_home
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ---------------------------------------------------------------------------
# T1 - codex-register.py refuses without an authoritative identity.
# ---------------------------------------------------------------------------

def _run_register_hook(meeting_home: Path, name: str, cwd: str, instance=None):
    runtime = {
        "name": name,
        "ws_addr": "ws://127.0.0.1:1",  # unused by the hook itself
        "control_url": HOST_URL,
    }
    if instance:
        runtime["instance"] = instance
    codex_dir = meeting_home / "codex"
    (codex_dir / "sessions").mkdir(parents=True, exist_ok=True)
    (codex_dir / "runtime.json").write_text(json.dumps(runtime), encoding="utf-8")

    payload = {"session_id": f"sess-{name}", "cwd": cwd, "source": "startup"}
    env = os.environ.copy()
    env["MEETING_HOME"] = str(meeting_home)
    return subprocess.run(
        [sys.executable, str(CODEX_REGISTER_PY)],
        input=json.dumps(payload), env=env, capture_output=True, text=True, timeout=15,
    )


def test_register_hook_uses_meeting_home_env(daemon_env, tmp_path):
    """codex-register.py must read/write under MEETING_HOME, not the real
    ~/.agent-meeting -- otherwise none of the other assertions here would be
    trustworthy (and every test run would touch the real install)."""
    meeting_home = daemon_env
    repo_root = tmp_path / "t1-env-repo"
    repo_root.mkdir()
    r = _run_register_hook(meeting_home, "cx-env-check", str(repo_root))
    assert r.returncode == 0, r.stderr
    mapping_file = meeting_home / "codex" / "sessions" / "cx-env-check.json"
    assert mapping_file.exists(), "mapping file was not written under MEETING_HOME"


def test_register_hook_rejects_missing_project_identity(daemon_env, tmp_path):
    meeting_home = daemon_env
    repo_root = tmp_path / "t1-repo"
    repo_root.mkdir()
    before = _all_session_names(meeting_home)

    r = _run_register_hook(meeting_home, "cx-noproj", str(repo_root))

    assert r.returncode == 0, "SessionStart hook must never fail the codex session"
    out = json.loads(r.stdout)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "未注册为" in ctx and "没有权威项目身份" in ctx, ctx
    assert "桥接就绪" not in ctx, "must not claim bridged when registration was refused"

    after = _all_session_names(meeting_home)
    assert after == before, "no session row must be written on a refused registration"


# ---------------------------------------------------------------------------
# T2 - codex-meeting.py's --proj entrypoint writes the same per-root cache
# `meeting online --proj` writes.
# ---------------------------------------------------------------------------

def _load_codex_meeting_module(meeting_home: Path):
    spec = importlib.util.spec_from_file_location(
        "codex_meeting_under_test", str(CODEX_MEETING_PY))
    mod = importlib.util.module_from_spec(spec)
    old = os.environ.get("MEETING_HOME")
    os.environ["MEETING_HOME"] = str(meeting_home)
    try:
        spec.loader.exec_module(mod)
    finally:
        if old is None:
            os.environ.pop("MEETING_HOME", None)
        else:
            os.environ["MEETING_HOME"] = old
    # meeting_common may already be cached in sys.modules from an earlier
    # import elsewhere in this process (e.g. a different MEETING_HOME) --
    # force the module-level global the cache functions actually read from,
    # exactly as test_authoritative_project.py's TC2 does.
    mod.meeting_common.MEETING_HOME = str(meeting_home)
    return mod


def test_codex_meeting_proj_entry_writes_same_cache_online_uses(daemon_env, tmp_path):
    meeting_home = daemon_env
    repo_root = tmp_path / "t2-declare-repo"
    repo_root.mkdir()

    mod = _load_codex_meeting_module(meeting_home)

    class _StopAfterProjHandling(RuntimeError):
        pass

    def _stub_setup(self):
        raise _StopAfterProjHandling("test stub: halt right after --proj handling, "
                                      "before any real codex/app-server process spawns")

    mod.Launcher.setup = _stub_setup

    old_argv, old_cwd = sys.argv, os.getcwd()
    sys.argv = ["codex-meeting.py", "cx-declare", "--proj", "declared-proj"]
    os.chdir(repo_root)
    try:
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 1, "setup() failure must roll back and exit 1"
    finally:
        sys.argv = old_argv
        os.chdir(old_cwd)

    root = mod.meeting_common._project_root(str(repo_root))
    cached = mod.meeting_common.proj_cache_get(root)
    assert cached == "declared-proj", f"cache not written by codex-meeting.py's --proj entry: {cached!r}"


# ---------------------------------------------------------------------------
# T3 - codex-register.py on the SAME root as T2: cache hit via --cwd alone,
# no --proj passed by the hook, project resolves to the DECLARED value.
# ---------------------------------------------------------------------------

def test_register_hook_hits_cache_from_codex_meeting_declaration(daemon_env, tmp_path):
    meeting_home = daemon_env
    repo_root = tmp_path / "t3-declare-then-register-repo"
    repo_root.mkdir()

    # Declare once, exactly as T2 does (same repo root, real cache write).
    mod = _load_codex_meeting_module(meeting_home)
    mod.Launcher.setup = lambda self: (_ for _ in ()).throw(RuntimeError("stub"))
    old_argv, old_cwd = sys.argv, os.getcwd()
    sys.argv = ["codex-meeting.py", "cx-fromcache", "--proj", "cache-hit-proj"]
    os.chdir(repo_root)
    try:
        with pytest.raises(SystemExit):
            mod.main()
    finally:
        sys.argv = old_argv
        os.chdir(old_cwd)

    # Now the SessionStart hook registers on the same root, passing only
    # --cwd (never --proj) -- exactly codex-register.py's real call shape.
    r = _run_register_hook(meeting_home, "cx-fromcache", str(repo_root))
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "已注册为" in ctx and "桥接就绪" in ctx, ctx

    row = _sessions_row(meeting_home, "cache-hit-proj", "cx-fromcache")
    assert row is not None, "session must be registered under the DECLARED project (cache hit)"

    # And explicitly NOT under what path-based derivation would have produced
    # for this root (derive_project() itself checks the cache FIRST, so it is
    # not a usable comparison once the cache is populated -- replicate its
    # cache-free fallback branch directly instead).
    home = os.path.expanduser("~")
    path_derived = ("~" + str(repo_root)[len(home):]) if str(repo_root).startswith(home + os.sep) \
        else str(repo_root)
    assert path_derived != "cache-hit-proj"  # sanity: they really are different strings
    assert _sessions_row(meeting_home, path_derived, "cx-fromcache") is None, (
        "registration must not have fallen through to path-based derivation"
    )


# ---------------------------------------------------------------------------
# Mock `meeting` CLI for the codex-bridge.py process tests (T4/T5): controls
# the exit code of `online` via env var, logs every invocation's argv.
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
if cmd == "online":
    sys.exit(int(os.environ.get("MOCK_MEETING_ONLINE_EXIT", "0")))
elif cmd in ("offline", "read"):
    sys.exit(0)
else:
    sys.exit(0)
'''


def _write_mock_meeting_cli(bin_dir: Path) -> None:
    p = bin_dir / "meeting"
    p.write_text(_MOCK_MEETING_CLI, encoding="utf-8")
    p.chmod(0o755)


def _mock_bridge_env(meeting_home: Path, calls_log: Path, online_exit: int) -> dict:
    env = os.environ.copy()
    env["MEETING_HOME"] = str(meeting_home)
    env["MOCK_MEETING_CALLS_LOG"] = str(calls_log)
    env["MOCK_MEETING_ONLINE_EXIT"] = str(online_exit)
    return env


def _online_calls(calls_log: Path) -> list:
    if not calls_log.exists():
        return []
    out = []
    for line in calls_log.read_text(encoding="utf-8").splitlines():
        args = json.loads(line)
        if args and args[0] == "online":
            out.append(args)
    return out


# ---------------------------------------------------------------------------
# T4 - missing identity (exit 4) is fatal at startup, exactly once, no retry.
# ---------------------------------------------------------------------------

@requires_venv
def test_bridge_missing_identity_is_fatal_no_retry(tmp_path):
    meeting_home = tmp_path / "t4-home"
    bin_dir = meeting_home / "bin"
    codex_dir = meeting_home / "codex"
    for d in (bin_dir, codex_dir / "sessions", codex_dir / "cursors", codex_dir / "logs"):
        d.mkdir(parents=True)
    _write_mock_meeting_cli(bin_dir)
    shutil.copy2(MEETING_COMMON_PATH, bin_dir / "meeting_common.py")

    session_name = "cx-t4"
    session_cwd = str(tmp_path / "project")
    Path(session_cwd).mkdir()

    # A parseable-but-unreachable control_url is enough: _resolve_control()
    # only parses it (no connection attempt) before _register() runs, and
    # _register() must fatal-exit BEFORE the bridge ever tries to connect.
    (codex_dir / "runtime.json").write_text(json.dumps({
        "name": session_name, "ws_addr": "ws://127.0.0.1:1",
        "control_url": "http://127.0.0.1:1",
    }), encoding="utf-8")
    (codex_dir / "sessions" / f"{session_name}.json").write_text(json.dumps({
        "name": session_name, "session_id": "fake-thread", "ws_addr": "ws://127.0.0.1:1",
        "cwd": session_cwd,
    }), encoding="utf-8")

    calls_log = tmp_path / "calls.log"
    env = _mock_bridge_env(meeting_home, calls_log, online_exit=4)

    proc = subprocess.Popen(
        [str(VENV_PY), str(BRIDGE_PY), session_name],
        env=env, cwd=str(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        out, _ = proc.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
        assert False, f"bridge did not exit promptly on exit-4 refusal; output:\n{out}"

    assert proc.returncode == 4, f"expected exit 4 passed through unchanged, got {proc.returncode}:\n{out}"
    calls = _online_calls(calls_log)
    assert len(calls) == 1, f"expected exactly one `online` call (no retry), got {calls}"
    assert not any(a == "--proj" for a in calls[0]), (
        f"bridge must never pass a derived --proj: {calls[0]}"
    )


# ---------------------------------------------------------------------------
# T5 - a transient failure (exit 1) is NOT fatal; the bridge retries
# `meeting online` again on every WS reconnect and stays alive.
# ---------------------------------------------------------------------------

def _ws_accept_key(client_key: str) -> str:
    import base64
    import hashlib
    return base64.b64encode(
        hashlib.sha1((client_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
    ).decode()


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


def _ws_send_unmasked(sock: socket.socket, opcode: int, payload: bytes) -> None:
    n = len(payload)
    if n < 126:
        header = struct.pack("!BB", 0x80 | opcode, n)
    elif n < 65536:
        header = struct.pack("!BBH", 0x80 | opcode, 126, n)
    else:
        header = struct.pack("!BBQ", 0x80 | opcode, 127, n)
    sock.sendall(header + payload)


class _AutoCloseControlServer:
    """Fake control WS /subscribe endpoint: completes the handshake then
    immediately closes the connection, `close_times` times in a row, forcing
    the bridge's WSSubscribeClient to reconnect that many times. After that
    it holds connections open (answers pings) so the process settles."""

    def __init__(self, close_times=2):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(5)
        self.host, self.port = self._srv.getsockname()
        self.close_times = close_times
        self.connect_count = 0
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
            self.connect_count += 1
            if self.connect_count <= self.close_times:
                conn.close()
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        conn.settimeout(1.0)
        while not self._stop:
            try:
                opcode, payload = meeting_common.ws_read_frame(conn)
            except socket.timeout:
                continue
            except (IOError, OSError):
                return
            if opcode == 0x9:
                _ws_send_unmasked(conn, 0xA, payload)
            elif opcode == 0x8:
                return

    def close(self):
        self._stop = True
        try:
            self._srv.close()
        except Exception:
            pass


@requires_venv
def test_bridge_transient_failure_retries_on_reconnect(tmp_path):
    meeting_home = tmp_path / "t5-home"
    bin_dir = meeting_home / "bin"
    codex_dir = meeting_home / "codex"
    for d in (bin_dir, codex_dir / "sessions", codex_dir / "cursors", codex_dir / "logs"):
        d.mkdir(parents=True)
    _write_mock_meeting_cli(bin_dir)
    shutil.copy2(MEETING_COMMON_PATH, bin_dir / "meeting_common.py")

    session_name = "cx-t5"
    session_cwd = str(tmp_path / "project")
    Path(session_cwd).mkdir()

    fake_control = _AutoCloseControlServer(close_times=2)
    (codex_dir / "runtime.json").write_text(json.dumps({
        "name": session_name, "ws_addr": "ws://127.0.0.1:1",
        "control_url": f"http://{fake_control.host}:{fake_control.port}",
    }), encoding="utf-8")
    (codex_dir / "sessions" / f"{session_name}.json").write_text(json.dumps({
        "name": session_name, "session_id": "fake-thread", "ws_addr": "ws://127.0.0.1:1",
        "cwd": session_cwd,
    }), encoding="utf-8")

    calls_log = tmp_path / "calls.log"
    env = _mock_bridge_env(meeting_home, calls_log, online_exit=1)  # transient, not in {3,4}

    proc = subprocess.Popen(
        [str(VENV_PY), str(BRIDGE_PY), session_name],
        env=env, cwd=str(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        # 1 startup call + 1 per successful connect (on_connect re-registers);
        # close_times=2 forces 2 reconnects on top of the first connect, so a
        # steady, comfortably-reached target is >= 4 calls, well under 15s
        # given a ~1s base backoff.
        deadline = time.time() + 15
        calls = []
        while time.time() < deadline:
            calls = _online_calls(calls_log)
            if len(calls) >= 4:
                break
            assert proc.poll() is None, (
                f"bridge exited early on a transient (non-3/4) failure -- "
                f"it must retry, not fatal-exit; calls so far: {calls}"
            )
            time.sleep(0.3)

        assert len(calls) >= 4, f"expected repeated retries across reconnects, got {calls}"
        assert proc.poll() is None, "bridge must still be running after transient-failure retries"
        assert not any(a == "--proj" for c in calls for a in c), (
            "bridge must never pass a derived --proj on any (re)register attempt"
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        fake_control.close()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
