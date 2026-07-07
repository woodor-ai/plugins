#!/usr/bin/env python3
"""
agent-meeting: codex-meeting launcher — one command to run a bridged, live codex
interactive session (form-B "live session wake").

    codex-meeting <name> [--port N] [--control-url URL]

Wires the whole chain and owns its lifecycle:
  1. Pick a port (default 8790). If a codex app-server is already there, reuse it
     (and never kill it on teardown — not ours). If the port is busy with
     something else, pick the next free port.
  2. Background-start `codex app-server --listen ws://127.0.0.1:<port>` (detached,
     logs to ~/.agent-meeting/codex/logs/), unless reusing an existing one.
  3. Write ~/.agent-meeting/codex/runtime.json = {name, ws_addr, control_url} so
     the SessionStart register hook and the bridge share one endpoint.
  4. Background-start the bridge daemon `<venv-python> codex-bridge.py <name>`.
  5. FOREGROUND `codex --remote ws://127.0.0.1:<port>` — inherits the real tty, so
     this IS the user's live interactive session. Its startup fires the
     SessionStart hook → codex-register writes the mapping + registers. The bridge
     re-reads the mapping per message, so the mapping arriving after the bridge
     started is fine.
  6. When the foreground codex exits (or on Ctrl-C / SIGTERM) → teardown: stop the
     bridge, `meeting offline <name>`, stop the app-server IF we started it, remove
     runtime.json (mapping/cursor are left in place — harmless).

Idempotent and self-cleaning: if any setup step fails, everything already started
is rolled back so no orphan app-server / bridge is left behind.

--no-codex : run steps 1-4 + teardown but SKIP the foreground codex (step 5). For
             automated testing where no real tty is available; the launcher holds
             until SIGINT/SIGTERM, then tears down.
"""

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

HOME = Path.home()
DATA = Path(os.environ.get("MEETING_HOME") or (HOME / ".agent-meeting"))
CODEX_DIR = DATA / "codex"
LOGS_DIR = CODEX_DIR / "logs"
RUNTIME_JSON = CODEX_DIR / "runtime.json"
MEETING_CLI = DATA / "bin" / "meeting"
BRIDGE_SCRIPT = Path(__file__).resolve().parent / "codex-bridge.py"
IS_WINDOWS = sys.platform.startswith("win")


def _venv_python() -> str:
    # Prefer the interpreter running this launcher if it is the agent-meeting venv;
    # otherwise fall back to the known venv path.
    if IS_WINDOWS:
        cand = DATA / "venv" / "Scripts" / "python.exe"
    else:
        cand = DATA / "venv" / "bin" / "python"
    return str(cand if cand.exists() else Path(sys.executable))


def _log(msg: str):
    ts = time.strftime("%H:%M:%S")
    print(f"[codex-meeting] {ts} {msg}", flush=True)


def _port_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _is_appserver(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1.5) as r:
            return 200 <= r.status < 500
    except Exception:
        return False


def _pick_port(preferred: int) -> tuple[int, bool]:
    """Return (port, reuse). reuse=True means an app-server is already listening."""
    if _is_appserver(preferred):
        return preferred, True
    if not _port_listening(preferred):
        return preferred, False
    # busy with a non-appserver — scan upward for a free port
    for p in range(preferred + 1, preferred + 50):
        if _is_appserver(p):
            return p, True
        if not _port_listening(p):
            return p, False
    raise RuntimeError(f"no free port found near {preferred}")


def _spawn_detached(cmd, log_path: Path):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logf = open(log_path, "a", encoding="utf-8")
    kwargs = dict(stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    if IS_WINDOWS:
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — survive console events,
        # stay killable by pid.
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)


def _run_meeting(*extra, control_url="", timeout=15):
    cmd = [_venv_python(), str(MEETING_CLI)] + list(extra)
    if control_url:
        cmd += ["--host", control_url]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None


def _discover_control_url() -> str:
    r = _run_meeting("controls", "--json")
    if not r or r.returncode != 0 or not r.stdout.strip():
        return ""
    try:
        controls = json.loads(r.stdout)
        if not controls:
            return ""
        c = next((x for x in controls if x.get("is_current")), controls[0])
        ip, port = c.get("ip", ""), c.get("port", "")
        return f"http://{ip}:{port}" if ip and port else ""
    except Exception:
        return ""


class Launcher:
    def __init__(self, name, preferred_port, control_url):
        self.name = name
        self.preferred_port = preferred_port
        self.control_url = control_url
        self.port = None
        self.appserver_proc = None      # our app-server Popen (None if reused)
        self.bridge_proc = None
        self._torn_down = False

    # ---- setup ----
    def setup(self):
        self.port, reuse = _pick_port(self.preferred_port)
        ws_addr = f"ws://127.0.0.1:{self.port}"
        if reuse:
            _log(f"reusing existing app-server on :{self.port} (won't stop it on exit)")
        else:
            _log(f"starting app-server on :{self.port}")
            self.appserver_proc = _spawn_detached(
                ["codex", "app-server", "--listen", ws_addr],
                LOGS_DIR / "app-server.log")
            if not self._wait_healthz(self.port, 20):
                raise RuntimeError("app-server did not become healthy in time")

        # runtime.json (shared endpoint for register hook + bridge)
        if not self.control_url:
            self.control_url = _discover_control_url()
        CODEX_DIR.mkdir(parents=True, exist_ok=True)
        # cwd = this launcher's cwd = the cwd the foreground `codex --remote`
        # inherits = the codex thread's cwd the register hook will report. The
        # bridge derives its project (X-Meeting-Project for /subscribe) from this
        # BEFORE the mapping exists, so it must match what the register hook uses
        # for `meeting online --cwd`, or inbound frames won't route to the bridge.
        RUNTIME_JSON.write_text(json.dumps({
            "name": self.name,
            "ws_addr": ws_addr,
            "control_url": self.control_url,
            "cwd": os.getcwd(),
        }, ensure_ascii=False), encoding="utf-8")
        _log(f"wrote runtime.json (ws_addr={ws_addr}, control_url={self.control_url or 'autodiscover'})")

        # bridge daemon
        _log("starting bridge daemon")
        self.bridge_proc = _spawn_detached(
            [_venv_python(), str(BRIDGE_SCRIPT), self.name],
            LOGS_DIR / "bridge.log")
        # brief liveness check
        time.sleep(1.0)
        if self.bridge_proc.poll() is not None:
            raise RuntimeError(f"bridge exited immediately (rc={self.bridge_proc.returncode}); "
                               f"see {LOGS_DIR/'bridge.log'}")
        _log(f"bridge running (pid={self.bridge_proc.pid})")

    def _wait_healthz(self, port, timeout_s) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if _is_appserver(port):
                return True
            if self.appserver_proc and self.appserver_proc.poll() is not None:
                return False
            time.sleep(0.4)
        return False

    # ---- foreground ----
    def run_codex(self):
        ws_addr = f"ws://127.0.0.1:{self.port}"
        _log(f"launching foreground: codex --remote {ws_addr}  (Ctrl-C to end + teardown)")
        try:
            subprocess.run(["codex", "--remote", ws_addr])
        except FileNotFoundError:
            _log("ERROR: `codex` not found on PATH")

    # ---- teardown ----
    def teardown(self):
        if self._torn_down:
            return
        self._torn_down = True
        _log("teardown")
        if self.bridge_proc and self.bridge_proc.poll() is None:
            _terminate(self.bridge_proc)
            _log("stopped bridge")
        r = _run_meeting("offline", self.name, control_url=self.control_url)
        _log(f"meeting offline {self.name}" + ("" if (r and r.returncode == 0) else " (best-effort)"))
        if self.appserver_proc and self.appserver_proc.poll() is None:
            _terminate(self.appserver_proc)
            _log("stopped app-server (ours)")
        try:
            RUNTIME_JSON.unlink()
            _log("removed runtime.json")
        except FileNotFoundError:
            pass

    def rollback(self):
        # setup failure: kill anything we started, leave reused resources alone
        if self.bridge_proc and self.bridge_proc.poll() is None:
            _terminate(self.bridge_proc)
        if self.appserver_proc and self.appserver_proc.poll() is None:
            _terminate(self.appserver_proc)
        try:
            RUNTIME_JSON.unlink()
        except FileNotFoundError:
            pass


def _terminate(proc):
    try:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(prog="codex-meeting")
    ap.add_argument("name", help="agent-meeting session name")
    ap.add_argument("--port", type=int, default=8790, help="app-server port (default 8790)")
    ap.add_argument("--control-url", default="", help="agent-meeting control base url (http://host:port)")
    ap.add_argument("--no-codex", action="store_true",
                    help="setup + hold + teardown without the foreground codex (testing)")
    args = ap.parse_args()

    if not MEETING_CLI.exists():
        _log(f"FATAL: meeting CLI not found at {MEETING_CLI}")
        sys.exit(5)
    if not BRIDGE_SCRIPT.exists():
        _log(f"FATAL: codex-bridge.py not found at {BRIDGE_SCRIPT}")
        sys.exit(5)

    launcher = Launcher(args.name, args.port, args.control_url)
    stop_event = threading.Event()

    def _sig(_signum, _frame):
        stop_event.set()
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(s, _sig)
        except (ValueError, OSError):
            pass

    try:
        launcher.setup()
    except Exception as e:
        _log(f"setup failed: {e}; rolling back")
        launcher.rollback()
        sys.exit(1)

    try:
        if args.no_codex:
            # Testing hold: wait for SIGINT/SIGTERM, or a stop-file (deterministic
            # trigger on Windows where catchable signals are awkward to deliver).
            stop_file = CODEX_DIR / f".stop-{args.name}"
            try:
                stop_file.unlink()
            except FileNotFoundError:
                pass
            _log(f"--no-codex: setup complete; holding (touch {stop_file} or SIGINT to teardown)")
            while not stop_event.is_set():
                if stop_file.exists():
                    try:
                        stop_file.unlink()
                    except FileNotFoundError:
                        pass
                    break
                time.sleep(0.5)
        else:
            launcher.run_codex()
    finally:
        launcher.teardown()


if __name__ == "__main__":
    main()
