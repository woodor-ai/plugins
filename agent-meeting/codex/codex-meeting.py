#!/usr/bin/env python3
"""
agent-meeting: codex-meeting launcher — one command to run a bridged, live codex
interactive session (form-B "live session wake").

    codex-meeting <name> [--port N] [--control-url URL] [--proj X]

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
import asyncio
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlparse

try:
    import websockets  # noqa: F401 — used by the warm-up helper (best-effort; optional)
except ImportError:
    websockets = None

HOME = Path.home()
DATA = Path(os.environ.get("MEETING_HOME") or (HOME / ".agent-meeting"))
CODEX_DIR = DATA / "codex"
LOGS_DIR = CODEX_DIR / "logs"
RUN_DIR = CODEX_DIR / "run"
RUNTIME_JSON = CODEX_DIR / "runtime.json"
LAUNCHER_JSON = CODEX_DIR / "launcher.json"   # persisted defaults (control_url) from install
MEETING_CLI = DATA / "bin" / "meeting"
BRIDGE_SCRIPT = Path(__file__).resolve().parent / "codex-bridge.py"
IS_WINDOWS = sys.platform.startswith("win")

# Shared meeting-CLI/discovery kernel (also used by monitor.py and codex-bridge.py).
# Soft-fail on import: this module is loaded directly by tests via importlib
# (test_codex_meeting_warmup.py) without a bootstrapped runtime, and none of the
# warm-up/launch-cmd logic under test touches meeting_common -- only the actual
# CLI-invoking functions (_run_meeting, _discover_control_url) need it, and they
# already tolerate failure the same way they tolerate a missing `meeting` binary.
sys.path.insert(0, str(DATA / "bin"))
try:
    import meeting_common
except ImportError:
    meeting_common = None


def _default_control_url() -> str:
    """Remembered control_url written at install time, so a bare
    `codex-meeting <name>` needs no --control-url."""
    try:
        return (json.loads(LAUNCHER_JSON.read_text(encoding="utf-8")).get("control_url") or "").strip()
    except Exception:
        return ""


def _default_name() -> str:
    """A stable per-machine default session name, so even the name is optional."""
    host = re.sub(r"[^A-Za-z0-9-]", "-", socket.gethostname().split(".")[0]).strip("-") or "host"
    return f"codex-{host}"[:20]


def _venv_python() -> str:
    # Use the agent-meeting venv python; fall back to the current interpreter
    # if the venv has not been created yet.
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
        # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP -- NOT DETACHED_PROCESS.
        # Real-machine finding: app-server and the codex bridge each spawn their
        # OWN console-subsystem grandchildren (codex's code-mode-host.exe helper,
        # and powershell.exe it shells out to run commands). A console child with
        # no creation flags of its own INHERITS its parent's console by default.
        # CREATE_NO_WINDOW gives OUR process a real (but hidden) console, so that
        # whole descendant chain inherits the SAME hidden console and stays
        # invisible. DETACHED_PROCESS instead gives our process NO console at
        # all, so each console-subsystem grandchild has nothing to inherit and
        # pops its own NEW visible window instead -- the exact opposite of what
        # we want, and the actual source of the blank python.exe /
        # code-mode-host.exe / powershell.exe windows Tommy saw. NEW_PROCESS_GROUP
        # is kept so Ctrl-C in our own console doesn't also signal these children.
        kwargs["creationflags"] = 0x08000000 | 0x00000200
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)


def _run_meeting(*extra, control_url="", timeout=15):
    if meeting_common is None:
        return None
    try:
        return meeting_common.run_meeting_cli(
            MEETING_CLI, *extra, python=_venv_python(),
            host=(control_url or None), timeout=timeout)
    except Exception:
        return None


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WINDOWS:
        try:
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER
        except Exception:
            pass
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _process_command_line(pid: int) -> str:
    if IS_WINDOWS:
        ps_cmd = (
            f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}'; "
            "if ($p) { $p.CommandLine }"
        )
        try:
            kw = {"creationflags": 0x08000000}  # CREATE_NO_WINDOW
            r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive",
                                "-Command", ps_cmd],
                               capture_output=True, text=True, timeout=4, **kw)
            if r.returncode == 0:
                return (r.stdout or "").strip()
        except Exception:
            return ""
        return ""

    proc_cmdline = Path("/proc") / str(pid) / "cmdline"
    try:
        raw = proc_cmdline.read_bytes()
        if raw:
            return raw.replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        pass
    try:
        r = subprocess.run(["ps", "-p", str(pid), "-o", "args="],
                           capture_output=True, text=True, timeout=4)
        if r.returncode == 0:
            return (r.stdout or "").strip()
    except Exception:
        pass
    return ""


def _is_codex_meeting_process(pid: int) -> bool:
    if pid == os.getpid():
        return True
    if not _pid_exists(pid):
        return False
    cmdline = _process_command_line(pid).lower()
    if not cmdline:
        return False
    return "codex-meeting.py" in cmdline or "codex-bridge.py" in cmdline


class AlreadyRunningError(RuntimeError):
    pass


class SingleInstanceLock:
    def __init__(self, name: str):
        self.name = name
        safe_name = quote(name, safe="-_.@")
        self.path = RUN_DIR / f"{safe_name}.pid"
        self.pid = os.getpid()
        self.acquired = False

    def acquire(self):
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            except FileExistsError:
                old_pid = self._read_pid()
                if old_pid and _is_codex_meeting_process(old_pid):
                    raise AlreadyRunningError(f"codex-meeting {self.name} 已在运行，pid={old_pid}")
                stale = f"pid={old_pid}" if old_pid else "unreadable pid"
                _log(f"removing stale pidfile for {self.name} ({stale})")
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                continue
            with os.fdopen(fd, "w", encoding="ascii") as f:
                f.write(f"{self.pid}\n")
            self.acquired = True
            return

    def release(self):
        if not self.acquired:
            return
        try:
            if self._read_pid() == self.pid:
                self.path.unlink()
        except FileNotFoundError:
            pass
        finally:
            self.acquired = False

    def _read_pid(self) -> int:
        try:
            return int(self.path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return 0


def _discover_control_url() -> str:
    if meeting_common is None:
        return ""
    return meeting_common.discover_control(_run_meeting).get("base_url", "")


# ---------------------------------------------------------------------------
# Auto-warm: the codex SessionStart register hook fires on the session's first
# TURN, not on process launch — the TUI defers `thread/start` until the user
# actually sends something. That leaves a window between `mycodex` starting
# and the user's first keystroke where the bridge has no mapping to inject
# into. Fix: fire one minimal turn ourselves via the app-server protocol
# (same style as codex-bridge.py's _codex_inject) right after the app-server
# is up — this creates a thread + fires the hook immediately, and the
# foreground codex is then told to `resume` that exact thread instead of
# opening a brand-new one. Best-effort: any failure just falls back to a
# plain fresh `codex --remote` session; it must never block the launch.
# ---------------------------------------------------------------------------
_WARM_PROMPT = ("[agent-meeting warm-up] Automated startup turn from the codex-meeting "
                "launcher. No reply needed — just let this settle and wait for real messages.")
_WARM_CALL_TIMEOUT_S = 15
_WARM_TOTAL_TIMEOUT_S = 30


async def _warm_up_thread_async(ws_addr: str, cwd: str):
    """Create a fresh thread and fire one minimal turn on it. Returns the new
    thread id on success, None on any failure. Mirrors codex-bridge.py's
    _codex_inject connection/call plumbing."""
    ws = await websockets.connect(ws_addr, max_size=None, open_timeout=10)
    pend = {}
    nid = 0

    async def recv_loop():
        try:
            async for data in ws:
                try:
                    m = json.loads(data)
                except Exception:
                    continue
                if isinstance(m, dict) and "id" in m and "method" not in m:
                    fut = pend.pop(m["id"], None)
                    if fut and not fut.done():
                        fut.set_result(m)
        except Exception:
            pass
        finally:
            err = ConnectionError("codex app-server connection closed")
            for fut in list(pend.values()):
                if not fut.done():
                    fut.set_exception(err)
            pend.clear()

    task = asyncio.create_task(recv_loop())

    async def call(method, params=None, timeout=_WARM_CALL_TIMEOUT_S):
        nonlocal nid
        if task.done():
            raise ConnectionError("receiver task ended; connection is dead")
        nid += 1
        rid = nid
        req = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            req["params"] = params
        fut = asyncio.get_event_loop().create_future()
        pend[rid] = fut
        try:
            await ws.send(json.dumps(req))
        except Exception as e:
            pend.pop(rid, None)
            raise ConnectionError(f"send failed: {e}")
        return await asyncio.wait_for(fut, timeout)

    try:
        await call("initialize", {"clientInfo": {"name": "codex-meeting-warmup", "version": "1"}})
        r = await call("thread/start", {"cwd": cwd, "sessionStartSource": "startup"})
        thread_id = ((r.get("result") or {}).get("thread") or {}).get("id")
        if not thread_id:
            _log(f"warm-up: thread/start returned no thread id: {r}")
            return None
        r2 = await call("turn/start",
                        {"threadId": thread_id, "input": [{"type": "text", "text": _WARM_PROMPT}]},
                        timeout=30)
        if not (r2.get("result") or {}).get("turn", {}).get("id"):
            _log(f"warm-up: turn/start returned no turn id: {r2}")
            return None
        return thread_id
    finally:
        task.cancel()
        try:
            await ws.close()
        except Exception:
            pass


def _run_warm_up(ws_addr: str, cwd: str):
    """Sync, best-effort wrapper: never raises, bounded total wait, returns the
    warmed thread id or None (never blocks the actual codex launch on failure)."""
    if websockets is None:
        _log("warm-up skipped: `websockets` not available in this interpreter")
        return None
    try:
        return asyncio.run(asyncio.wait_for(_warm_up_thread_async(ws_addr, cwd), _WARM_TOTAL_TIMEOUT_S))
    except Exception as e:
        _log(f"warm-up failed ({type(e).__name__}: {e}); continuing without a pre-warmed thread")
        return None


def _build_codex_launch_cmd(ws_addr: str, thread_id):
    """Foreground codex command: resume the pre-warmed thread if we have one,
    else fall back to a plain fresh --remote session."""
    if thread_id:
        return ["codex", "resume", thread_id, "--remote", ws_addr]
    return ["codex", "--remote", ws_addr]


# ---------------------------------------------------------------------------
# Terminal window title: codex's TUI has no programmable status bar (unlike
# Claude Code's), so the identity cue is the terminal window/tab title
# instead. Real-machine finding: codex's own TUI startup writes its own title
# (the cwd dirname) shortly AFTER launch, silently clobbering a one-shot title
# set before handoff. Fix: a daemon thread re-asserts our title every few
# seconds for as long as the foreground codex subprocess is alive, so ours
# always wins the last write. ASCII-only (no emoji): old cmd.exe/conhost
# consoles can render emoji as garbage glyphs and there is no reliable way to
# detect the host terminal, so plain ASCII is the safe default.
#
# POSIX: OSC 0 written to /dev/tty (same escape sequence as before, just
# repeated). Windows: this process shares the console with the foreground
# `codex` child (subprocess.run with no creation flags inherits it), so
# ctypes SetConsoleTitleW from here takes effect directly on that shared
# console — no stdout writes involved, so it can never bleed into the TUI's
# output stream.
# ---------------------------------------------------------------------------
_DEFAULT_TITLE = "codex"
_TITLE_REFRESH_INTERVAL_S = 5.0


def _title_text(name: str, project: str, control_url: str) -> str:
    # Show the session identity as name@project (bare name for a global '*'
    # identity or when project is unknown), the same convention the statusline
    # and peer displays use. No "[meeting]" prefix.
    label = name if (not project or project == "*") else f"{name}@{project}"
    hostport = ""
    if control_url:
        u = urlparse(control_url)
        if u.hostname and u.port:
            hostport = f"{u.hostname}:{u.port}"
    return f"{label} | {hostport}" if hostport else label


def _set_terminal_title(title: str):
    if IS_WINDOWS:
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW(title)
        except Exception:
            pass
        return
    try:
        with open("/dev/tty", "w", encoding="ascii", errors="replace") as tty:
            tty.write(f"\033]0;{title}\a")
            tty.flush()
    except OSError:
        pass


class _TitlePinner:
    """Background daemon thread that re-asserts the terminal title every
    _TITLE_REFRESH_INTERVAL_S seconds while the foreground codex TUI is
    alive, so codex's own post-startup title write never wins for long."""

    def __init__(self, title: str):
        self._title = title
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        _set_terminal_title(self._title)
        self._thread.start()

    def _run(self):
        while not self._stop.wait(_TITLE_REFRESH_INTERVAL_S):
            _set_terminal_title(self._title)

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2)


class Launcher:
    def __init__(self, name, preferred_port, control_url, project=""):
        self.name = name
        self.preferred_port = preferred_port
        self.control_url = control_url
        self.project = project
        self.port = None
        self.appserver_proc = None      # our app-server Popen (None if reused)
        self.bridge_proc = None
        self.warm_thread_id = None      # set by setup() if auto-warm succeeded
        self.lock = SingleInstanceLock(name)
        self._torn_down = False

    # ---- setup ----
    def setup(self):
        self.lock.acquire()
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
        # Raw launch cwd, NOT normalized to the git main root: this becomes the
        # actual working directory for the warm-up thread (and the bridge's CWD
        # fallback), so a launch from inside a worktree must keep operating on
        # that worktree's checkout. Project-name matching between this launcher,
        # the register hook, and the bridge is already guaranteed by
        # meeting_common.derive_project's --git-common-dir resolution regardless
        # of which cwd (worktree or main root) is passed in -- no normalization
        # of the cwd itself is needed for that. (Removed _normalize_runtime_cwd /
        # _git_main_root, which forced this to the main root and would have
        # pointed a worktree-launched codex session at the wrong checkout.)
        runtime_cwd = os.getcwd()
        CODEX_DIR.mkdir(parents=True, exist_ok=True)
        RUNTIME_JSON.write_text(json.dumps({
            "name": self.name,
            "ws_addr": ws_addr,
            "control_url": self.control_url,
            "cwd": runtime_cwd,
        }, ensure_ascii=False), encoding="utf-8")
        _log(f"wrote runtime.json (ws_addr={ws_addr}, control_url={self.control_url or 'autodiscover'})")

        # auto-warm: fire one minimal turn now so the name<->session mapping is
        # ready before the user types anything (best-effort, never fatal)
        _log("warming up session (firing an initial turn on the app-server)")
        self.warm_thread_id = _run_warm_up(ws_addr, runtime_cwd)
        if self.warm_thread_id:
            _log(f"warm-up ok (thread={self.warm_thread_id}); codex will resume this thread")
        else:
            _log("warm-up did not complete; codex will open a fresh session instead (non-fatal)")

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
        cmd = _build_codex_launch_cmd(ws_addr, self.warm_thread_id)
        pinner = _TitlePinner(_title_text(self.name, self.project, self.control_url))
        pinner.start()
        _log(f"launching foreground: {' '.join(cmd)}  (Ctrl-C to end + teardown)")
        try:
            subprocess.run(cmd)
        except FileNotFoundError:
            _log("ERROR: `codex` not found on PATH")
        finally:
            pinner.stop()

    # ---- teardown ----
    def teardown(self):
        if self._torn_down:
            return
        self._torn_down = True
        _log("teardown")
        # No original title to restore (no portable way to query it back from
        # the terminal) -- reset to a plain default instead.
        _set_terminal_title(_DEFAULT_TITLE)
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
        self.lock.release()

    def rollback(self):
        # setup failure: kill anything we started, leave reused resources alone
        if self.bridge_proc and self.bridge_proc.poll() is None:
            _terminate(self.bridge_proc)
        if self.appserver_proc and self.appserver_proc.poll() is None:
            _terminate(self.appserver_proc)
        if self.lock.acquired:
            try:
                RUNTIME_JSON.unlink()
            except FileNotFoundError:
                pass
        self.lock.release()


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
    ap.add_argument("name", nargs="?", default=None,
                    help=f"agent-meeting session name (default: {_default_name()})")
    ap.add_argument("--port", type=int, default=8790, help="app-server port (default 8790)")
    ap.add_argument("--control-url", default="",
                    help="agent-meeting control base url (default: the one saved at install time)")
    ap.add_argument("--proj", default=None,
                    help="explicit project identity for this codex session; cached per repo root so "
                         "registration + bridge pick it up (symmetric with /meeting --proj)")
    ap.add_argument("--no-codex", action="store_true",
                    help="setup + hold + teardown without the foreground codex (testing)")
    args = ap.parse_args()

    if not MEETING_CLI.exists():
        _log(f"FATAL: meeting CLI not found at {MEETING_CLI}")
        sys.exit(5)
    if not BRIDGE_SCRIPT.exists():
        _log(f"FATAL: codex-bridge.py not found at {BRIDGE_SCRIPT}")
        sys.exit(5)

    name = args.name or _default_name()

    if args.proj is not None:
        if meeting_common is None:
            _log("--proj given but meeting_common is unavailable; cannot cache project identity")
        else:
            try:
                proj = meeting_common.validate_proj(args.proj)
            except ValueError as e:
                _log(str(e))
                sys.exit(2)
            root = meeting_common._project_root(os.getcwd())
            meeting_common.proj_cache_set(root, proj)
            _log(f"--proj: cached project '{proj}' for root {root}")

    control_url = args.control_url or _default_control_url()
    # Project for the terminal-title label — the same value this session will
    # register under (derive_project reads the --proj cache just written above,
    # or falls back to the cwd-based identity).
    title_project = ""
    if meeting_common is not None:
        try:
            title_project = meeting_common.derive_project(os.getcwd())
        except Exception:
            title_project = ""
    launcher = Launcher(name, args.port, control_url, title_project)
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
    except AlreadyRunningError as e:
        _log(str(e))
        sys.exit(2)
    except Exception as e:
        _log(f"setup failed: {e}; rolling back")
        launcher.rollback()
        sys.exit(1)

    try:
        if args.no_codex:
            # Testing hold: wait for SIGINT/SIGTERM, or a stop-file (deterministic
            # trigger on Windows where catchable signals are awkward to deliver).
            stop_file = CODEX_DIR / f".stop-{name}"
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
