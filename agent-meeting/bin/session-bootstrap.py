#!/usr/bin/env python3
"""
SessionStart hook for agent-meeting plugin. Cross-platform (macOS / Windows / Linux).

Responsibilities (idempotent — runs every SessionStart):
  1. Ensure ~/.agent-meeting/ structure exists (db/, bin link)
  2. Ensure venv at ~/.agent-meeting/venv with zeroconf installed
  3. Read ~/.agent-meeting/config.json (auto-create if missing). The `is_host`
     flag determines whether this machine launches the daemon.
  4. If is_host=true and daemon not already running → spawn meeting-daemon
     detached as a background process. Tracks pid in /tmp/meeting-daemon.pid
     (on Windows: %TEMP%\\meeting-daemon.pid).
  5. Emit JSON `hookSpecificOutput.additionalContext` with online peers + setup hints.

Replaces the bash session-bootstrap.sh — that one only worked on POSIX.
"""

import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

HOME = Path.home()
# Honor MEETING_HOME (same env the meeting CLI and monitor.py already respect) so
# the whole runtime can be relocated — required for isolated codex-only installs
# and testing on a machine that already has a live ~/.agent-meeting.
DATA = Path(os.environ.get("MEETING_HOME") or (HOME / ".agent-meeting"))
DB_DIR = DATA / "db"
DB = DB_DIR / "rooms.db"
CONFIG = DATA / "config.json"
VENV = DATA / "venv"
BIN_LINK = DATA / "bin"

PLUGIN_ROOT = Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or os.environ.get("PLUGIN_ROOT") or "")
TMP = Path(tempfile.gettempdir())
DAEMON_PID_FILE = TMP / "meeting-daemon.pid"

IS_MAC = sys.platform == "darwin"
IS_WINDOWS = sys.platform.startswith("win")
IS_LINUX = sys.platform.startswith("linux")

LAUNCHD_LABEL = "com.tommy.agent-meeting"
LAUNCHD_PLIST = HOME / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"

# Windows: no-admin persistence is a Startup-folder launcher (primary logon
# auto-start) + a /SC MINUTE schtasks task (resurrects the supervisor process
# if it is killed mid-session). ONLOGON tasks need admin, so they are NOT used.
# Sentinel records the task command so we only recreate it when the plugin path
# moves (mirrors ensure_launchd).
SCHTASKS_TN = "agent-meeting-daemon"
SCHTASKS_SENTINEL = DATA / ".schtasks-cmd"
SUPERVISOR_PID_FILE = TMP / "meeting-supervisor.pid"
STOP_SENTINEL = DATA / "daemon.stopped"
STARTUP_DIR = HOME / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"

TELEMETRY_URL = "https://www.woodor.ai/_functions/t"

LOG_DIR = DATA / "logs"

# 模块级全局：ensure_launchd() 自愈失败时写入警告文本；emit_context() 读取后追加到 additionalContext。
LAUNCHD_WARNING = ""


def blog(msg: str):
    """追加一行到 ~/.agent-meeting/logs/bootstrap.log（带本机时间戳）。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    with open(LOG_DIR / "bootstrap.log", "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")


def _daemon_healthy(port: int = 8765, timeout: float = 1.0) -> bool:
    """GET /health 探测 daemon 是否在线。2xx 视为健康，任何异常/超时返回 False。"""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=timeout
        ) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


if IS_MAC:
    _OS_LABEL = "mac"
elif IS_WINDOWS:
    _OS_LABEL = "win"
else:
    _OS_LABEL = "linux"


def log(msg: str):
    sys.stderr.write(f"[meeting-bootstrap] {msg}\n")


# ---------- telemetry ----------

def beacon(event: str, version: str, machine_id: str, cfg: dict | None = None):
    """Fire-and-forget telemetry. Skipped when MEETING_NO_TELEMETRY is set or
    config.json has telemetry=false (absent/null counts as enabled)."""
    if os.environ.get("MEETING_NO_TELEMETRY"):
        return
    if cfg is not None and cfg.get("telemetry") is False:
        return

    def _send():
        try:
            params = urllib.parse.urlencode({
                "e": event,
                "id": machine_id,
                "v": version,
                "os": _OS_LABEL,
            })
            url = f"{TELEMETRY_URL}?{params}"
            urllib.request.urlopen(url, timeout=2)
        except Exception:
            pass

    t = threading.Thread(target=_send, daemon=True)
    t.start()


# ---------- 1. ensure dirs ----------

def ensure_layout():
    DATA.mkdir(exist_ok=True)
    DB_DIR.mkdir(exist_ok=True)


# ---------- 3b. bin wrappers (called after venv is ready) ----------

def _is_reparse_point(p: Path) -> bool:
    """True for a Windows junction / reparse-point dir.

    Critical: Python's Path.is_symlink() returns False for NTFS *junctions*, so a
    junction would otherwise fall through to shutil.rmtree() — which recurses INTO
    the junction and deletes the *target's* contents (e.g. the plugin cache). We
    detect the reparse-point attribute and remove the link itself with os.rmdir.
    On POSIX st_file_attributes doesn't exist → AttributeError → False.
    """
    try:
        return bool(p.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except (AttributeError, OSError):
        return False


def ensure_bin_wrappers():
    """Create ~/.agent-meeting/bin/ as a real directory of venv-python wrapper scripts.

    The old design used a symlink/junction bin/ → plugin's bin/, which made the CLI
    scripts run under system python3 (shebang: #!/usr/bin/env python3). System
    python3 often lacks zeroconf, so discover_host() always returned None and the
    client fell back to local SQLite instead of connecting to the LAN daemon.

    New design: bin/ is a real directory. Extensionless scripts (meeting,
    meeting-daemon) become thin shell wrappers that exec the venv
    python with the real plugin script path. .py files (monitor.py, statusline.py,
    session-bootstrap.py) are COPIED, because callers explicitly pass
    `python3 ~/.agent-meeting/bin/foo.py` and so they must be real .py files.
    We copy rather than symlink: symlink_to() needs Administrator / Developer-Mode
    privilege on Windows and would crash the whole bootstrap (taking statusLine
    registration down with it); a copy is privilege-free and identical on every OS.

    Wrappers are regenerated whenever PLUGIN_ROOT changes (plugin version upgrade),
    which keeps the copied .py files fresh. The sentinel file .bin-plugin-root
    records the last generated plugin path.
    """
    import shutil as _shutil

    if not PLUGIN_ROOT or not (PLUGIN_ROOT / "bin").is_dir():
        return

    plugin_bin = (PLUGIN_ROOT / "bin").resolve()
    # Do NOT resolve() the venv python — following symlinks would land on the
    # system python binary and bypass the venv's site-packages (losing zeroconf).
    py = venv_python()

    sentinel = DATA / ".bin-plugin-root"
    current_root = str(plugin_bin)
    existing_root = sentinel.read_text().strip() if sentinel.exists() else ""

    def _all_present() -> bool:
        # Every plugin bin entry must have a corresponding dest (.py copied as-is,
        # extensionless scripts become .cmd on Windows). Missing one (e.g. a
        # newly-added statusline.py on an unchanged plugin path) forces regen.
        for src in plugin_bin.iterdir():
            if src.is_dir():
                continue  # skip __pycache__ and friends
            name = src.name if (src.suffix == ".py" or not IS_WINDOWS) else src.with_suffix(".cmd").name
            if not (BIN_LINK / name).exists():
                return False
        return True

    if (existing_root == current_root
            and BIN_LINK.is_dir()
            and not BIN_LINK.is_symlink()
            and not _is_reparse_point(BIN_LINK)
            and _all_present()):
        return  # Already up to date for this plugin version

    # Remove whatever occupies BIN_LINK. Order matters on Windows:
    #   - a file/dir *symlink* → unlink() (never touches the target)
    #   - a *junction* (reparse-point dir; is_symlink() is False for these!) →
    #     os.rmdir() removes the link itself; rmtree() would recurse INTO the
    #     junction and wipe the plugin cache it points at.
    #   - a real directory → rmtree
    if BIN_LINK.is_symlink():
        BIN_LINK.unlink()
    elif _is_reparse_point(BIN_LINK):
        os.rmdir(str(BIN_LINK))
    elif BIN_LINK.is_dir():
        _shutil.rmtree(str(BIN_LINK))
    elif BIN_LINK.exists():
        BIN_LINK.unlink()

    BIN_LINK.mkdir()

    for src in sorted(plugin_bin.iterdir()):
        if src.is_dir():
            continue  # skip __pycache__ and friends
        dest = BIN_LINK / src.name
        if src.suffix == ".py":
            # Copy, NOT symlink — callers invoke `python3 .../foo.py` directly, so
            # these must be real files, and symlink_to() needs admin/Developer-Mode
            # on Windows (would crash bootstrap). Copy is privilege-free everywhere.
            _shutil.copyfile(str(src), str(dest))
        elif IS_WINDOWS:
            # .cmd wrapper for PATH/shell resolution (monitor, bare `meeting`).
            dest.with_suffix(".cmd").write_text(
                f'@echo off\r\n"{py}" "{src}" %*\r\n'
            )
            # ALSO a real extensionless copy so callers can run
            #   python.exe "<bin>\meeting" <args>
            # via CreateProcess, bypassing cmd.exe — which mangles `<`/`>` in
            # args as redirection when the .cmd forwards them through %*. Any
            # CLI call carrying user content (send --ask/--body) MUST use this.
            _shutil.copyfile(str(src), str(dest))
        else:
            dest.write_text(f'#!/bin/sh\nexec "{py}" "{src}" "$@"\n')
            dest.chmod(0o755)

    # Convenience entries for codex bridge scripts that live in codex/ (not bin/):
    # the launcher `codex-meeting` and the outbound helper `meeting-say`.
    for _stem in ("codex-meeting", "meeting-say"):
        _src = (PLUGIN_ROOT / "codex" / f"{_stem}.py")
        if not _src.exists():
            continue
        if IS_WINDOWS:
            (BIN_LINK / f"{_stem}.cmd").write_text(f'@echo off\r\n"{py}" "{_src}" %*\r\n')
        else:
            _w = BIN_LINK / _stem
            _w.write_text(f'#!/bin/sh\nexec "{py}" "{_src}" "$@"\n')
            _w.chmod(0o755)

    sentinel.write_text(current_root)
    log(f"generated venv-python wrappers in bin/ (plugin: {plugin_bin.name})")


# ---------- 2. venv + zeroconf ----------

def venv_python() -> Path:
    if IS_WINDOWS:
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def ensure_venv():
    py = venv_python()
    if py.exists():
        return
    log(f"creating venv at {VENV}")
    subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True, capture_output=True)


def ensure_zeroconf():
    py = venv_python()
    # Quick probe — try importing
    r = subprocess.run([str(py), "-c", "import zeroconf"], capture_output=True)
    if r.returncode == 0:
        return
    log("installing zeroconf into venv (one-time, ~10s)")
    subprocess.run([str(py), "-m", "pip", "install", "--quiet", "zeroconf"], check=True)


def ensure_websockets():
    # Required by the codex bridge daemon (agent-meeting/codex/codex-bridge.py),
    # which speaks JSON-RPC over WebSockets to a codex app-server.
    py = venv_python()
    r = subprocess.run([str(py), "-c", "import websockets"], capture_output=True)
    if r.returncode == 0:
        return
    log("installing websockets into venv (one-time, ~10s)")
    subprocess.run([str(py), "-m", "pip", "install", "--quiet", "websockets"], check=True)


# ---------- 3. config ----------

def _read_plugin_version() -> str:
    """Read version from plugin.json next to this script's PLUGIN_ROOT."""
    try:
        pj = PLUGIN_ROOT / ".claude-plugin" / "plugin.json"
        if pj.exists():
            return json.loads(pj.read_text()).get("version", "unknown")
    except Exception:
        pass
    return "unknown"


def load_or_create_config(min_version: str | None = None) -> tuple[dict, bool, str]:
    """Return (cfg, is_new_install, machine_id).

    Side effects:
    - Generates machine_id if absent (new install → also returns is_new_install=True).
    - Updates plugin_version in config, but never downgrades below min_version.
      Pass min_version=installed_ver when the downgrade guard fired so that
      config.json plugin_version stays at the higher installed version.
    """
    version = _read_plugin_version()
    # Honour the monotonic-upgrade invariant: if the caller knows a higher
    # version is already installed, keep that version in config.json.
    if min_version is not None and min_version != "unknown":
        if _parse_semver(version) < _parse_semver(min_version):
            version = min_version
    is_new_install = False

    if CONFIG.exists():
        try:
            cfg = json.loads(CONFIG.read_text())
        except Exception:
            log("config.json malformed, recreating")
            cfg = None
    else:
        cfg = None

    if cfg is None:
        # First-ever creation: new install.
        machine_id = uuid.uuid4().hex
        cfg = {
            "is_host": False,
            "created_at": int(time.time()),
            "machine_id": machine_id,
            "plugin_version": version,
        }
        CONFIG.write_text(json.dumps(cfg, indent=2))
        try:
            os.chmod(CONFIG, 0o600)
        except Exception:
            pass
        is_new_install = True
    else:
        dirty = False
        if "machine_id" not in cfg:
            cfg["machine_id"] = uuid.uuid4().hex
            dirty = True
        if cfg.get("plugin_version") != version:
            cfg["plugin_version"] = version
            dirty = True
        if dirty:
            CONFIG.write_text(json.dumps(cfg, indent=2))

    return cfg, is_new_install, cfg["machine_id"]


# ---------- 4. daemon launch ----------

def pid_alive(pid: int) -> bool:
    if IS_WINDOWS:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        exit_code = ctypes.c_ulong(0)
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return exit_code.value == 259  # STILL_ACTIVE
    else:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True


def daemon_running() -> bool:
    if not DAEMON_PID_FILE.exists():
        return False
    try:
        pid = int(DAEMON_PID_FILE.read_text().strip())
    except Exception:
        return False
    return pid_alive(pid)


def launch_daemon():
    """Session-bound daemon launch (Linux / Windows). Mac uses launchd instead."""
    daemon_path = PLUGIN_ROOT / "bin" / "meeting-daemon"
    if not daemon_path.exists():
        log(f"daemon script missing: {daemon_path}")
        return
    py = venv_python()
    log_file = TMP / "meeting-daemon.log"
    # Detach the daemon so it survives hook exit and Claude Code session close.
    if IS_WINDOWS:
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        flags = 0x00000008 | 0x00000200
        proc = subprocess.Popen(
            [str(py), str(daemon_path), "--port", "8765"],
            stdout=open(log_file, "a"),
            stderr=subprocess.STDOUT,
            creationflags=flags,
            close_fds=True,
        )
    else:
        proc = subprocess.Popen(
            [str(py), str(daemon_path), "--port", "8765"],
            stdout=open(log_file, "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    DAEMON_PID_FILE.write_text(str(proc.pid))
    log(f"daemon launched pid={proc.pid}, log={log_file}")


# ---------- 4b. launchd integration (Mac host only) ----------

def kill_bootstrap_daemon():
    """If a previous bootstrap-launched daemon is running, kill it.
    Mac launchd is about to take over — two daemons on :8765 = conflict."""
    if not DAEMON_PID_FILE.exists():
        return
    try:
        pid = int(DAEMON_PID_FILE.read_text().strip())
        os.kill(pid, 15)  # SIGTERM
        time.sleep(0.5)
    except (ValueError, OSError):
        pass
    try:
        DAEMON_PID_FILE.unlink()
    except FileNotFoundError:
        pass


def ensure_launchd():
    """在 ~/Library/LaunchAgents/ 安装 plist 并用 launchd 托管 daemon。

    策略：OS 持久化为主，SessionStart hook 降级为兜底体检。
    - 每次调用先做 launchctl enable（幂等清除 disabled 覆盖，确保登录自启）。
    - plist 未变且已 loaded 且 /health 通 → no-op 返回。
    - listed 但 /health 不通（卡死/crashloop）→ 走重装自愈路径。
    - plist 变了 → 先 bootout 再重新 bootstrap。
    - bootstrap 后轮询最多 5 秒校验 /health；若不健康最多重试 2 次自愈。
    - 失败落盘到 ~/.agent-meeting/logs/bootstrap.log；成功/失败均写日志。
    - 最终仍失败 → 设 LAUNCHD_WARNING 供 emit_context() 注入 additionalContext。
    - 整段 launchd 操作用跨进程文件锁串行，防并发 SessionStart 交错。
    """
    global LAUNCHD_WARNING
    import fcntl
    import plistlib

    LAUNCHD_PLIST.parent.mkdir(parents=True, exist_ok=True)

    daemon_path = PLUGIN_ROOT / "bin" / "meeting-daemon"
    if not daemon_path.exists():
        msg = f"daemon script missing: {daemon_path}"
        log(msg)
        blog(msg)
        return
    py = venv_python()
    log_file = TMP / "meeting-daemon.log"

    plist = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [str(py), str(daemon_path), "--port", "8765"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(log_file),
        "StandardErrorPath": str(log_file),
        "ProcessType": "Background",
    }
    new_bytes = plistlib.dumps(plist)

    lock_path = DATA / "run" / "launchd.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(lock_path, "w")
    try:
        # 阻塞等锁，最多 30 秒；超时放弃避免卡死 SessionStart。
        import errno as _errno
        deadline = time.monotonic() + 30
        while True:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if e.errno not in (_errno.EACCES, _errno.EAGAIN):
                    raise
                if time.monotonic() >= deadline:
                    msg = "ensure_launchd: 拿锁超时（30s），跳过本次 launchd 操作"
                    log(msg)
                    blog(msg)
                    return
                time.sleep(0.5)

        _ensure_launchd_locked(new_bytes, py, log_file)
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


def _ensure_launchd_locked(new_bytes: bytes, py: Path, log_file: Path):
    """ensure_launchd 的实体逻辑；调用方持有跨进程文件锁后才能进入。"""
    global LAUNCHD_WARNING
    import plistlib  # noqa: F811 — 此函数独立可调用，保留 import

    old_bytes = LAUNCHD_PLIST.read_bytes() if LAUNCHD_PLIST.exists() else b""
    plist_changed = new_bytes != old_bytes
    if plist_changed:
        LAUNCHD_PLIST.write_bytes(new_bytes)

    uid = os.getuid()
    domain_target = f"gui/{uid}"
    service_target = f"{domain_target}/{LAUNCHD_LABEL}"

    # 先做 enable：清除任何 disabled 覆盖状态，确保登录自启（未注册时会报错，忽略返回码）。
    subprocess.run(
        ["launchctl", "enable", service_target],
        capture_output=True,
    )

    listed = subprocess.run(
        ["launchctl", "print", service_target],
        capture_output=True,
    ).returncode == 0

    if listed and not plist_changed:
        if _daemon_healthy():
            log(f"launchd already manages {LAUNCHD_LABEL} (healthy)")
            return
        # listed 但 /health 不通 → daemon 卡死/crashloop，走重装自愈路径
        blog(f"launchd listed 但 /health 不通，进入自愈路径")

    if listed:
        subprocess.run(
            ["launchctl", "bootout", service_target],
            capture_output=True,
        )

    # Kill any session-bound daemon so port 8765 is free
    kill_bootstrap_daemon()

    def _do_bootstrap() -> bool:
        """执行 bootstrap，失败时降级到 legacy load -w。返回是否命令本身成功。"""
        r = subprocess.run(
            ["launchctl", "bootstrap", domain_target, str(LAUNCHD_PLIST)],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            return True
        r2 = subprocess.run(
            ["launchctl", "load", "-w", str(LAUNCHD_PLIST)],
            capture_output=True, text=True,
        )
        return r2.returncode == 0

    def _wait_healthy(total: float = 5.0, interval: float = 0.5) -> bool:
        """轮询 /health，最多等 total 秒。"""
        steps = int(total / interval)
        for _ in range(steps):
            if _daemon_healthy():
                return True
            time.sleep(interval)
        return False

    # 首次 bootstrap
    _do_bootstrap()
    if _wait_healthy():
        msg = f"launchd loaded {LAUNCHD_LABEL}（auto-start on boot，KeepAlive on）"
        log(msg)
        blog(msg)
        return

    # 自愈重试，最多 2 次
    for attempt in range(1, 3):
        blog(f"bootstrap 后 daemon 未健康，自愈重试 #{attempt}")
        subprocess.run(["launchctl", "bootout", service_target], capture_output=True)
        time.sleep(1.5)
        _do_bootstrap()
        if _wait_healthy():
            msg = f"launchd loaded {LAUNCHD_LABEL}（自愈 #{attempt} 成功）"
            log(msg)
            blog(msg)
            return

    # 全部失败
    warn = (
        "⚠ control daemon 自动拉起失败，建议跑 `meeting daemon restart` "
        "或查看 ~/.agent-meeting/logs/bootstrap.log"
    )
    log(warn)
    blog(f"FAIL: {warn}")
    LAUNCHD_WARNING = warn


# ---------- 4b-win. Windows persistence (host only, no admin) ----------
#
# Windows analog of macOS launchd KeepAlive, under a hard "no admin" constraint.
# Real-machine finding: a logon-triggered task (schtasks /SC ONLOGON) is a
# protected operation that REQUIRES elevation — it fails "Access is denied" for
# a non-elevated user. Time-based tasks (/SC MINUTE) and the Startup folder do
# NOT need admin. So persistence is two no-admin layers:
#   1. Startup-folder .cmd  → launches the supervisor immediately at logon
#      (and after a reboot+logon). This is the primary auto-start.
#   2. schtasks /SC MINUTE  → every 2 min, (re)launch the supervisor. Pure
#      belt-and-suspenders: resurrects the supervisor PROCESS if it is killed
#      mid-session without a re-logon. The supervisor's single-instance guard
#      makes repeated launches a no-op while one is alive.
# The supervisor itself owns daemon keep-alive (instant relaunch on exit + 20s
# 假死 health probe). The only uncovered case — start before interactive logon
# (lock screen) — inherently needs a service = admin, so it is out of scope.

STARTUP_CMD = STARTUP_DIR / "agent-meeting-daemon.cmd"


def _supervisor_running() -> bool:
    try:
        pid = int(SUPERVISOR_PID_FILE.read_text().strip())
    except Exception:
        return False
    return pid_alive(pid)


def _launch_supervisor_now(pyw: Path, supervisor: Path):
    """Start the supervisor immediately (detached, no console) so the daemon is
    up this session without waiting for the Startup launcher or the MINUTE task.
    No-op if one is already alive (the supervisor's own singleton guard would
    make a second one exit anyway)."""
    if _supervisor_running():
        return
    try:
        subprocess.Popen([str(pyw), str(supervisor)],
                         creationflags=0x00000008 | 0x00000200, close_fds=True)
    except Exception as e:
        log(f"supervisor launch failed: {e}")


def ensure_windows_persistence():
    """Install/refresh the no-admin Windows persistence for the daemon and make
    sure the supervisor is running now. Idempotent like ensure_launchd: the
    Startup .cmd and the MINUTE task both embed the venv-pythonw + supervisor
    path, so we only rewrite/recreate when that path changes (plugin move)."""
    supervisor = BIN_LINK / "supervisor.py"
    if not supervisor.exists():
        log(f"supervisor missing: {supervisor}")
        return

    pyw = VENV / "Scripts" / "pythonw.exe"
    if not pyw.exists():
        pyw = venv_python()  # fall back to python.exe (console window)
    tr = f'"{pyw}" "{supervisor}"'

    # A fresh install/refresh means the daemon SHOULD be running — clear any
    # prior stop sentinel so the supervisor doesn't immediately bail.
    try:
        STOP_SENTINEL.unlink()
    except FileNotFoundError:
        pass
    kill_bootstrap_daemon()  # free :8765 from any old session-bound daemon

    # Layer 1: Startup-folder launcher (primary logon auto-start, no admin).
    # Use \n in-memory; text-mode write_text translates to CRLF on disk (what
    # cmd.exe wants) and read_text normalizes back to \n, so the equality check
    # is stable and we don't needlessly rewrite the file every SessionStart.
    startup_line = f'@echo off\nstart "" "{pyw}" "{supervisor}"\n'
    try:
        STARTUP_DIR.mkdir(parents=True, exist_ok=True)
        if not STARTUP_CMD.exists() or STARTUP_CMD.read_text() != startup_line:
            STARTUP_CMD.write_text(startup_line)
            log(f"installed Startup launcher: {STARTUP_CMD}")
    except Exception as e:
        log(f"startup launcher install failed: {e}")

    # Layer 2: MINUTE resurrector task (no admin; recreate only on path change).
    existing = SCHTASKS_SENTINEL.read_text().strip() if SCHTASKS_SENTINEL.exists() else ""
    registered = subprocess.run(
        ["schtasks", "/Query", "/TN", SCHTASKS_TN], capture_output=True
    ).returncode == 0
    if not (registered and existing == tr):
        r = subprocess.run(
            ["schtasks", "/Create", "/TN", SCHTASKS_TN, "/SC", "MINUTE",
             "/MO", "2", "/F", "/TR", tr],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            SCHTASKS_SENTINEL.write_text(tr)
            log(f"installed MINUTE resurrector task: {SCHTASKS_TN}")
        else:
            # Not fatal — the Startup launcher still gives logon auto-start.
            log(f"MINUTE task create failed (Startup launcher still active): "
                f"{(r.stderr or r.stdout).strip()}")

    _launch_supervisor_now(pyw, supervisor)


def remove_windows_persistence():
    """Tear down the Windows persistence (Startup .cmd + MINUTE task) and stop a
    running supervisor/daemon. Called when this machine is NOT a host, so a
    former host stops auto-launching a daemon. Idempotent."""
    removed = False
    try:
        if STARTUP_CMD.exists():
            STARTUP_CMD.unlink(); removed = True
    except Exception:
        pass
    if subprocess.run(["schtasks", "/Query", "/TN", SCHTASKS_TN],
                      capture_output=True).returncode == 0:
        subprocess.run(["schtasks", "/Delete", "/TN", SCHTASKS_TN, "/F"],
                       capture_output=True)
        removed = True
    try:
        SCHTASKS_SENTINEL.unlink()
    except FileNotFoundError:
        pass
    # Stop a running supervisor (sentinel makes it exit without relaunch) + daemon.
    try:
        STOP_SENTINEL.write_text(str(int(time.time())))
    except Exception:
        pass
    for pidf in (DAEMON_PID_FILE, SUPERVISOR_PID_FILE):
        try:
            pid = int(pidf.read_text().strip())
            if IS_WINDOWS:
                subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
            else:
                os.kill(pid, 15)
        except Exception:
            pass
    if removed:
        log("removed Windows persistence (not a host)")


# ---------- 4c. status line (Claude Code TUI) ----------

def claude_settings_path() -> Path:
    """User-level Claude Code settings.json (honors CLAUDE_CONFIG_DIR)."""
    cfg_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(cfg_dir) if cfg_dir else (HOME / ".claude")
    return base / "settings.json"


def ensure_statusline():
    """Idempotently register our status-line command in Claude Code settings.

    Shows `📞 <name>  |  <model>  |  <dir>  |  <branch>` once a session has
    registered via /meeting (the badge self-gates: statusline.py only renders it
    when monitor.py has written the local name cache for this cwd).

    Conservative: if the user already has a *different* statusLine configured,
    we leave it untouched rather than clobber it. We only install/refresh when
    statusLine is absent or already points at our statusline.py.
    """
    settings_path = claude_settings_path()
    # Only act under a real Claude Code install (settings dir present).
    if not settings_path.parent.is_dir():
        return

    script = BIN_LINK / "statusline.py"
    py = venv_python()
    command = f'"{py}" "{script}"'

    settings = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception:
            log("settings.json malformed — skipping statusLine install")
            return

    existing = settings.get("statusLine")
    if isinstance(existing, dict):
        cur = existing.get("command", "")
        if "statusline.py" not in cur:
            log("a custom statusLine is configured — leaving it untouched")
            return
        if cur == command:
            return  # already current

    settings["statusLine"] = {"type": "command", "command": command, "padding": 0}
    try:
        settings_path.write_text(
            json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        log(f"installed statusLine → {script}")
    except Exception as e:
        log(f"statusLine install failed: {e}")


# ---------- 5. context emission ----------

def online_peers_str() -> str:
    """Online peers = sessions-table rows with a fresh heartbeat (last_seen
    within 12s). Reads rooms.db read-only. The old directory.json + monitor
    pid-file scheme was removed — never resurrect it."""
    if not DB.exists():
        return "(none online)"
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=2)
        try:
            cutoff = time.time() - 12
            rows = con.execute(
                "SELECT name FROM sessions WHERE last_seen >= ? ORDER BY name",
                (cutoff,),
            ).fetchall()
        finally:
            con.close()
        names = [r[0] for r in rows]
        return ", ".join(names) if names else "(none online)"
    except Exception:
        return "(none online)"


def emit_context(cfg: dict):
    role = "host" if cfg.get("is_host") else "client"
    peers = online_peers_str()
    hostname = socket.gethostname()

    # Hand the agent the EXACT, OS-resolved invocations for this machine so it
    # never has to translate POSIX→Windows or probe the filesystem to find the
    # CLI. On Windows the CLI goes through python.exe on the real (extensionless)
    # `meeting` script — never meeting.cmd (cmd.exe mangles `<`/`>` in args).
    py = venv_python()
    meeting = BIN_LINK / "meeting"
    monitor = BIN_LINK / "monitor.py"
    if IS_WINDOWS:
        cli = f'"{py}" "{meeting}"'                              # PowerShell CLI tool calls
        mon = f'"{py.as_posix()}" "{monitor.as_posix()}" <name>'  # Monitor tool runs in bash
        os_label = "windows"
    else:
        cli = str(meeting)
        mon = f"python3 {monitor} <name>"
        os_label = "posix"

    ctx = f"""📞 Meeting system is active.

This session has NO meeting name yet — and that's fine. Peer-to-peer agent messaging is OPTIONAL. NEVER gate, block, or delay the user's work to make them register a name.

- Proceed with whatever the user asks, normally — registration is NOT a prerequisite for anything.
- If the user's message contains a `/meeting` command — whether it is the whole message (`/meeting <name>`) OR embedded inside a natural-language directive (e.g. "你是 X，先运行 /meeting X --director 启动监听") — run the meeting skill: they are explicitly opting in to register.
- Reminder line: on your FIRST reply of this session ONLY, you MAY append this single line at the very end — but SKIP it entirely whenever this session registers via `/meeting` (i.e. you run the meeting skill this turn), no matter where the command appeared in the user's message. Only show the reminder when the session does NOT register at all:
  > 💡 本会话未注册 meeting；如需与其他 agent 互通可 `/meeting <名字>` 注册（不影响当前任务）。
  Decide by your own action (did you register?), NOT by whether the message literally starts with `/meeting`. Show it at most once per session, never repeat it, and never let it replace or postpone the actual task.

These paths are ALREADY RESOLVED for this machine — use them verbatim, do NOT probe the filesystem to find the CLI or venv:
- CLI invocation: `{cli} <args>`
- Monitor tool command (bash): `{mon}`

Backend: SQLite at {DB}.
Machine: `{hostname}` (role: {role}, os: {os_label}).
Online peers: {peers}
"""
    if LAUNCHD_WARNING:
        ctx += f"\n{LAUNCHD_WARNING}\n"
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": ctx,
        }
    }))


# ---------- version comparison ----------

def _parse_semver(v: str) -> tuple:
    """Parse 'X.Y.Z' into (X, Y, Z) as ints for comparison. Unknown/malformed → (0, 0, 0)."""
    try:
        parts = [int(x) for x in v.strip().split(".")]
        # Pad to 3 components
        while len(parts) < 3:
            parts.append(0)
        return tuple(parts[:3])
    except Exception:
        return (0, 0, 0)


def _read_installed_version() -> str | None:
    """Read the version currently installed in the shared runtime.

    Priority order (stops at first hit):
    1. Wrapper script exec path — the most reliable indicator after a real install.
       The wrapper's second line is: exec "<venv-py>" "<plugin-root>/bin/meeting-daemon"
       The plugin root is a versioned cache dir like .../agent-meeting/0.8.0/...
    2. config.json plugin_version field.
    3. .bin-plugin-root sentinel (contains the plugin_bin path, version segment embedded).

    Returns None if no runtime is present (fresh install → caller treats as no downgrade).
    """
    # 1. Parse wrapper exec path
    wrapper = DATA / "bin" / "meeting"
    if wrapper.exists() and not wrapper.is_dir():
        try:
            text = wrapper.read_text(encoding="utf-8", errors="replace")
            # Look for a path segment matching a semver directory component
            import re
            m = re.search(r"[/\\]agent-meeting[/\\](\d+\.\d+(?:\.\d+)?)[/\\]", text)
            if m:
                return m.group(1)
        except Exception:
            pass

    # 2. config.json
    if CONFIG.exists():
        try:
            v = json.loads(CONFIG.read_text()).get("plugin_version")
            if v and v != "unknown":
                return v
        except Exception:
            pass

    # 3. .bin-plugin-root sentinel
    sentinel = DATA / ".bin-plugin-root"
    if sentinel.exists():
        try:
            text = sentinel.read_text().strip()
            import re
            m = re.search(r"[/\\]agent-meeting[/\\](\d+\.\d+(?:\.\d+)?)[/\\]", text)
            if m:
                return m.group(1)
        except Exception:
            pass

    return None


# ---------- main ----------

def main():
    try:
        ensure_layout()       # base dirs first
        ensure_venv()         # venv must exist before wrappers reference its python
        ensure_zeroconf()
        ensure_websockets()   # codex bridge daemon speaks WS to the codex app-server

        # Monotonic-upgrade guard: skip runtime rewrite if this session's plugin
        # version is older than what's already installed.
        session_ver = _read_plugin_version()
        installed_ver = _read_installed_version()
        skip_runtime_rewrite = False
        if installed_ver is not None and session_ver != "unknown":
            if _parse_semver(session_ver) < _parse_semver(installed_ver):
                msg = (f"skip downgrade: session {session_ver} < installed {installed_ver}, "
                       f"keeping {installed_ver}")
                log(msg)
                blog(msg)
                skip_runtime_rewrite = True

        if not skip_runtime_rewrite:
            ensure_bin_wrappers()
            ensure_statusline()

        cfg, is_new_install, machine_id = load_or_create_config(
            min_version=installed_ver if skip_runtime_rewrite else None
        )
        version = cfg.get("plugin_version", "unknown")

        if is_new_install:
            beacon("install", version, machine_id, cfg)

        if not skip_runtime_rewrite:
            if cfg.get("is_host"):
                if IS_MAC:
                    ensure_launchd()              # plist + KeepAlive — survives reboots
                elif IS_WINDOWS:
                    ensure_windows_persistence()  # Startup launcher + MINUTE task + supervisor
                elif not daemon_running():
                    launch_daemon()               # Linux: session-bound for now
            elif IS_WINDOWS:
                # Not a host anymore — tear down any persistence a prior host left.
                remove_windows_persistence()

        emit_context(cfg)
    except Exception as e:
        # Hook failures must not block session start — emit empty JSON.
        log(f"bootstrap failed: {e}")
        print(json.dumps({}))


if __name__ == "__main__":
    main()
