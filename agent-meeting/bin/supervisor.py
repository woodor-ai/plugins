#!/usr/bin/env python3
"""
meeting-supervisor — Windows keep-alive babysitter for meeting-daemon.

Windows analog of macOS launchd KeepAlive. On Windows there is no built-in
"restart this process when it exits" facility for a user-level, no-admin
service, so this supervisor provides it. It is launched by a user-level
scheduled task (schtasks /SC ONLOGON, installed by session-bootstrap.py's
ensure_schtasks) and runs for the lifetime of the logon session.

Responsibilities
----------------
1. Single-instance guard — refuse to run if another live supervisor exists.
2. Launch meeting-daemon detached (venv pythonw → no console window).
3. Keep it alive: relaunch whenever the daemon process exits, for ANY reason
   — crash, the daemon's own watchdog os._exit(1) on 假死, or an external
   taskkill — UNLESS the stop sentinel is present.
4. Redundant external health probe. The daemon has an in-process watchdog
   that os._exit(1)s when its self /health check fails. But a total GIL-level
   wedge (a C extension holding the GIL) could stall that in-process thread
   too. We probe /health out-of-process every PROBE_INTERVAL seconds; two
   consecutive failures → taskkill the daemon so the relaunch loop revives it.
   This is the only layer that can recover a wedge the in-process watchdog
   can't.

Contract with `meeting daemon` (CLI, cmd_daemon Windows branch)
---------------------------------------------------------------
Stop sentinel: ~/.agent-meeting/daemon.stopped
  - `meeting daemon stop`    → writes the sentinel (then taskkills daemon +
    supervisor). Seeing it, the supervisor exits WITHOUT relaunching.
  - `meeting daemon start/restart` → removes the sentinel before (re)launching.
The sentinel is the authoritative "operator wants it down" signal; the daemon
exit code alone can't distinguish a deliberate stop from a crash/wedge, so the
sentinel — not the exit code — gates relaunch.

Testing hook: set MEETING_NO_MDNS=1 to launch the daemon with --no-mdns
(local-only, no LAN announce) — used for single-machine self-tests.
"""

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOME = Path.home()
MEETING_HOME = Path(os.environ.get("MEETING_HOME") or (HOME / ".agent-meeting"))
BIN = MEETING_HOME / "bin"
STOP_SENTINEL = MEETING_HOME / "daemon.stopped"

TMP = Path(tempfile.gettempdir())
DAEMON_PID_FILE = TMP / "meeting-daemon.pid"
SUPERVISOR_PID_FILE = TMP / "meeting-supervisor.pid"
DAEMON_LOG = TMP / "meeting-daemon.log"
SUPERVISOR_LOG = TMP / "meeting-supervisor.log"

PORT = int(os.environ.get("MEETING_PORT", "8765"))
POLL = 5               # main loop tick (seconds)
PROBE_INTERVAL = 20    # how often to run the redundant /health probe
PROBE_FAILS_TO_KILL = 2
RELAUNCH_BACKOFF = 3   # min seconds between consecutive daemon launches

IS_WINDOWS = sys.platform.startswith("win")


def log(msg: str):
    line = f"[meeting-supervisor {time.strftime('%H:%M:%S')}] {msg}\n"
    sys.stderr.write(line)
    try:
        with open(SUPERVISOR_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


# ---------- process helpers ----------

def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WINDOWS:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        exit_code = ctypes.c_ulong(0)
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return exit_code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def read_pid(path: Path) -> int:
    try:
        return int(path.read_text().strip())
    except Exception:
        return 0


def daemon_alive() -> bool:
    return pid_alive(read_pid(DAEMON_PID_FILE))


def taskkill(pid: int):
    if pid <= 0:
        return
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True)
        else:
            os.kill(pid, 9)
    except Exception as e:
        log(f"taskkill {pid} failed: {e}")


def venv_python(windowless: bool) -> Path:
    if IS_WINDOWS:
        name = "pythonw.exe" if windowless else "python.exe"
        return MEETING_HOME / "venv" / "Scripts" / name
    return MEETING_HOME / "venv" / "bin" / "python"


# ---------- daemon lifecycle ----------

def launch_daemon():
    daemon = BIN / "meeting-daemon"
    if not daemon.exists():
        log(f"daemon script missing: {daemon}")
        return
    py = venv_python(windowless=True)
    cmd = [str(py), str(daemon), "--port", str(PORT)]
    if os.environ.get("MEETING_NO_MDNS"):
        cmd.append("--no-mdns")
    logf = open(DAEMON_LOG, "a", encoding="utf-8")
    if IS_WINDOWS:
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — survive supervisor death;
        # a fresh supervisor re-adopts it via the pid file.
        flags = 0x00000008 | 0x00000200
        proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT,
                                creationflags=flags, close_fds=True)
    else:
        proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT,
                                start_new_session=True, close_fds=True)
    DAEMON_PID_FILE.write_text(str(proc.pid))
    log(f"daemon launched pid={proc.pid} cmd={' '.join(cmd)}")


def probe_health() -> bool:
    try:
        s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
        try:
            s.sendall(b"GET /health HTTP/1.0\r\nHost: localhost\r\n\r\n")
            data = s.recv(256)
        finally:
            s.close()
        return b"200" in data.split(b"\r\n", 1)[0]
    except Exception:
        return False


# ---------- single-instance guard ----------

def acquire_singleton() -> bool:
    """Return True if we are the sole supervisor; False if another is alive."""
    other = read_pid(SUPERVISOR_PID_FILE)
    if other and other != os.getpid() and pid_alive(other):
        log(f"another supervisor is alive (pid={other}); exiting")
        return False
    try:
        SUPERVISOR_PID_FILE.write_text(str(os.getpid()))
    except Exception as e:
        log(f"could not write supervisor pid file: {e}")
    return True


def release_singleton():
    if read_pid(SUPERVISOR_PID_FILE) == os.getpid():
        try:
            SUPERVISOR_PID_FILE.unlink()
        except Exception:
            pass


# ---------- main loop ----------

def main():
    if STOP_SENTINEL.exists():
        log("stop sentinel present at startup — not launching, exiting")
        return
    if not acquire_singleton():
        return

    log(f"supervisor started pid={os.getpid()} port={PORT} "
        f"(no_mdns={'1' if os.environ.get('MEETING_NO_MDNS') else '0'})")
    last_launch = 0.0
    last_probe = 0.0
    health_failures = 0
    try:
        while True:
            if STOP_SENTINEL.exists():
                log("stop sentinel detected — exiting without relaunch")
                return

            if not daemon_alive():
                # Respect backoff so a daemon that exits immediately on launch
                # doesn't spin the loop.
                wait = RELAUNCH_BACKOFF - (time.monotonic() - last_launch)
                if wait > 0:
                    time.sleep(wait)
                if STOP_SENTINEL.exists():
                    log("stop sentinel detected during backoff — exiting")
                    return
                log("daemon not running — (re)launching")
                launch_daemon()
                last_launch = time.monotonic()
                health_failures = 0
                last_probe = time.monotonic()  # grace before first probe
                time.sleep(POLL)
                continue

            # Daemon alive — redundant external health probe.
            now = time.monotonic()
            if now - last_probe >= PROBE_INTERVAL:
                last_probe = now
                if probe_health():
                    health_failures = 0
                else:
                    health_failures += 1
                    log(f"health probe failed ({health_failures}/{PROBE_FAILS_TO_KILL})")
                    if health_failures >= PROBE_FAILS_TO_KILL:
                        log("daemon wedged (in-process watchdog stalled?) — "
                            "taskkill to force relaunch")
                        taskkill(read_pid(DAEMON_PID_FILE))
                        health_failures = 0
            time.sleep(POLL)
    finally:
        release_singleton()


if __name__ == "__main__":
    main()
