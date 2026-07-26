#!/usr/bin/env python3
"""
SessionStart hook for agent-meeting plugin. Cross-platform (macOS / Windows / Linux).

Responsibilities (idempotent — runs every SessionStart):
  1. Ensure ~/.agent-meeting/ structure exists (db/, bin link)
  2. Ensure venv at ~/.agent-meeting/venv with zeroconf installed
  3. Read ~/.agent-meeting/config.json (auto-create if missing). The `is_host`
     flag determines whether this machine launches the central amctl.
  4. If is_host=true and central amctl is not already running → spawn amctl
     detached as a background process. Tracks pid in /tmp/amctl.pid
     (on Windows: %TEMP%\\amctl.pid).
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

if sys.platform.startswith("win"):
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

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
AMCTL_PID_FILE = TMP / "amctl.pid"

IS_MAC = sys.platform == "darwin"
IS_WINDOWS = sys.platform.startswith("win")
IS_LINUX = sys.platform.startswith("linux")

LAUNCHD_LABEL = "com.tommy.agent-meeting.amctl"
LAUNCHD_PLIST = HOME / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
_PRE_AMCTL_LAUNCHD_LABEL = "com.tommy.agent-meeting"
_PRE_AMCTL_LAUNCHD_PLIST = HOME / "Library" / "LaunchAgents" / f"{_PRE_AMCTL_LAUNCHD_LABEL}.plist"

# Windows: no-admin persistence is a Startup-folder launcher (primary logon
# auto-start) + a /SC MINUTE schtasks task (resurrects the supervisor process
# if it is killed mid-session). ONLOGON tasks need admin, so they are NOT used.
# Sentinel records the task command so we only recreate it when the plugin path
# moves (mirrors ensure_launchd).
SCHTASKS_TN = "agent-meeting-amctl"
_PRE_AMCTL_SCHTASKS_TN = "agent-meeting-daemon"
SCHTASKS_SENTINEL = DATA / ".schtasks-cmd"
SUPERVISOR_PID_FILE = TMP / "meeting-supervisor.pid"
STOP_SENTINEL = DATA / "amctl.stopped"
STARTUP_DIR = HOME / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
_PRE_AMCTL_STARTUP_CMD = STARTUP_DIR / "agent-meeting-daemon.cmd"

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


def _amctl_health_info(port: int = 8765, timeout: float = 1.0) -> dict:
    """Return the central amctl health payload, or an empty dict."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=timeout
        ) as resp:
            if not 200 <= resp.status < 300:
                return {}
            payload = json.loads(resp.read().decode("utf-8"))
            return payload if payload.get("ok") else {}
    except Exception:
        return {}


def _amctl_healthy(port: int = 8765, timeout: float = 1.0) -> bool:
    """Return whether the central amctl /health endpoint is available."""
    return bool(_amctl_health_info(port, timeout))


def _amctl_version_matches(expected: str, port: int = 8765) -> bool:
    """Return whether the live amctl is running the installed plugin version."""
    health = _amctl_health_info(port)
    return _health_version_matches(health, expected)


def _health_version_matches(health: dict, expected: str) -> bool:
    """Return whether a health payload reports the expected plugin version."""
    return bool(
        health
        and (
            expected == "unknown"
            or str(health.get("version") or "") == expected
        )
    )


def _health_instance_id(health: dict) -> str:
    """Return the process instance identifier from a health payload."""
    return str(health.get("instance_id") or "")


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
    client fell back to local SQLite instead of connecting to the central amctl.

    New design: bin/ is a real directory. Extensionless scripts (meeting,
    amctl) become thin shell wrappers that exec the venv
    python with the real plugin script path. .py files (monitor.py, statusline.py,
    session-bootstrap.py, meeting_common.py) are COPIED, because callers
    explicitly pass `python3 ~/.agent-meeting/bin/foo.py` and so they must be
    real .py files. codex-meeting.py also imports the copied
    meeting_common.py from this directory.
    We copy rather than symlink: symlink_to() needs Administrator / Developer-Mode
    privilege on Windows and would crash the whole bootstrap (taking statusLine
    registration down with it); a copy is privilege-free and identical on every OS.

    Wrappers are regenerated whenever PLUGIN_ROOT or its plugin version changes,
    which keeps copied .py files fresh even when a Codex upgrade overwrites the
    same install path. The sentinel file .bin-plugin-root records both values.
    """
    import shutil as _shutil

    if not PLUGIN_ROOT or not (PLUGIN_ROOT / "bin").is_dir():
        return

    plugin_bin = (PLUGIN_ROOT / "bin").resolve()
    # Do NOT resolve() the venv python — following symlinks would land on the
    # system python binary and bypass the venv's site-packages (losing zeroconf).
    py = venv_python()

    sentinel = DATA / ".bin-plugin-root"
    current_stamp = f"{plugin_bin}\n{_read_plugin_version()}"
    existing_stamp = sentinel.read_text().strip() if sentinel.exists() else ""

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
        # Also check the codex/ wrapper entries: an upgrade from an install that had
        # codex-meeting to one that expects mycodex would be skipped by the sentinel
        # even though mycodex is absent.
        if not (BIN_LINK / ("mycodex.cmd" if IS_WINDOWS else "mycodex")).exists():
            return False
        if IS_WINDOWS and not (BIN_LINK / "mycodex-impl.ps1").exists():
            return False
        return True

    if (existing_stamp == current_stamp
            and BIN_LINK.is_dir()
            and not BIN_LINK.is_symlink()
            and not _is_reparse_point(BIN_LINK)
            and _all_present()):
        # Even when nothing else needs regenerating, sweep known stale files —
        # otherwise a leftover Windows extensionless `mycodex` (see
        # _cleanup_stale_codex_plugins) would never get cleaned once the
        # sentinel settles, since the full-swap path below never runs again.
        _cleanup_stale_codex_plugins(BIN_LINK)
        return  # Already up to date for this plugin version

    # Build wrappers into a temp dir first — if the copy loop fails/interrupts
    # partway, the existing BIN_LINK stays intact and concurrent `meeting` calls
    # still find valid scripts. The only moment BIN_LINK is absent is the tiny
    # window between "remove old" and os.rename in the swap below.
    tmp_bin = BIN_LINK.parent / (".bin.tmp." + str(os.getpid()))
    if tmp_bin.exists():
        _shutil.rmtree(str(tmp_bin))
    tmp_bin.mkdir(parents=True)

    try:
        for src in sorted(plugin_bin.iterdir()):
            if src.is_dir():
                continue  # skip __pycache__ and friends
            dest = tmp_bin / src.name
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

        # `mycodex`: copied verbatim from agent-meeting/codex/mycodex-posix.sh (+
        # mycodex.cmd/mycodex-impl.ps1 on Windows) — the single source of truth
        # also used by the root installer (install-codex.py), so both sites
        # regenerate the exact same file. Unconditional: mycodex must always
        # self-heal here even if codex-meeting.py itself is (temporarily)
        # missing — its own "not installed" check handles that case at runtime.
        _mycodex_src_dir = PLUGIN_ROOT / "codex"
        if IS_WINDOWS and (_mycodex_src_dir / "mycodex-impl.ps1").exists():
            _shutil.copyfile(str(_mycodex_src_dir / "mycodex-impl.ps1"), str(tmp_bin / "mycodex-impl.ps1"))
            _shutil.copyfile(str(_mycodex_src_dir / "mycodex.cmd"), str(tmp_bin / "mycodex.cmd"))
        elif not IS_WINDOWS and (_mycodex_src_dir / "mycodex-posix.sh").exists():
            _dest_sh = tmp_bin / "mycodex"
            _shutil.copyfile(str(_mycodex_src_dir / "mycodex-posix.sh"), str(_dest_sh))
            _dest_sh.chmod(0o755)

    except Exception:
        _shutil.rmtree(str(tmp_bin), ignore_errors=True)
        raise

    # Swap: remove old BIN_LINK then rename tmp_bin into place. Order matters on
    # Windows:
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

    os.rename(str(tmp_bin), str(BIN_LINK))
    sentinel.write_text(current_stamp)
    _cleanup_stale_codex_plugins(BIN_LINK)
    log(f"generated venv-python wrappers in bin/ (plugin: {plugin_bin.name})")


def _cleanup_stale_codex_plugins(bin_dir: Path) -> None:
    """Delete only exact leftover filenames from a prior install (superseded by
    mycodex --update). Refuses to act unless bin_dir resolves to exactly
    DATA/bin, and only ever unlinks known files by name — never recurses.

    Windows only: an extensionless `mycodex` here is always a leftover from a
    pre-dual-extension install, and a same-named `mycodex.ps1` is always a
    leftover from a pre-single-entry install (the regen path above only ever
    writes mycodex-impl.ps1 / mycodex.cmd on Windows). A `mycodex.ps1` sibling
    to `mycodex.cmd` would get resolved first by PowerShell and blocked by the
    default execution policy, which is exactly the bug this rename fixes. On
    POSIX `mycodex` (no extension) IS the current artifact, so it must never
    be swept here.
    """
    if bin_dir.resolve() != (DATA / "bin").resolve():
        return
    names = (
        "codex-plugins",
        "codex-plugins.cmd",
        "codex-plugins.ps1",
        "meeting-say",
        "meeting-say.cmd",
    )
    if IS_WINDOWS:
        names = names + ("mycodex", "mycodex.ps1")
    for name in names:
        p = bin_dir / name
        if p.is_file():
            p.unlink()


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
    # Required by the machine-wide Codex broker, which proxies the remote TUI
    # and speaks JSON-RPC to the shared official Codex app-server.
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
        if "is_host" not in cfg:
            legacy_host = False
            if IS_MAC:
                legacy_host = (
                    _PRE_AMCTL_LAUNCHD_PLIST.exists()
                    or LAUNCHD_PLIST.exists()
                )
            elif IS_WINDOWS:
                legacy_host = (
                    _PRE_AMCTL_STARTUP_CMD.exists()
                    or (STARTUP_DIR / "agent-meeting-amctl.cmd").exists()
                )
            elif IS_LINUX:
                legacy_host = (
                    (TMP / "meeting-daemon.pid").exists()
                    or AMCTL_PID_FILE.exists()
                )
            cfg["is_host"] = legacy_host
            dirty = True
            if legacy_host:
                log("migrated legacy central-node installation to is_host=true")
        if "machine_id" not in cfg:
            cfg["machine_id"] = uuid.uuid4().hex
            dirty = True
        if cfg.get("plugin_version") != version:
            cfg["plugin_version"] = version
            dirty = True
        if dirty:
            CONFIG.write_text(json.dumps(cfg, indent=2))

    return cfg, is_new_install, cfg["machine_id"]


# ---------- 4. central amctl launch ----------

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


def amctl_running() -> bool:
    if not AMCTL_PID_FILE.exists():
        return False
    try:
        pid = int(AMCTL_PID_FILE.read_text().strip())
    except Exception:
        return False
    return pid_alive(pid)


def launch_amctl():
    """Launch the session-bound central amctl on Linux or Windows."""
    amctl_path = PLUGIN_ROOT / "bin" / "amctl"
    if not amctl_path.exists():
        log(f"central amctl script missing: {amctl_path}")
        return
    py = venv_python()
    log_file = TMP / "amctl.log"
    # Detach central amctl so it survives hook exit and Claude Code session close.
    if IS_WINDOWS:
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        flags = 0x00000008 | 0x00000200
        proc = subprocess.Popen(
            [str(py), str(amctl_path), "--port", "8765"],
            stdout=open(log_file, "a"),
            stderr=subprocess.STDOUT,
            creationflags=flags,
            close_fds=True,
        )
    else:
        proc = subprocess.Popen(
            [str(py), str(amctl_path), "--port", "8765"],
            stdout=open(log_file, "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    AMCTL_PID_FILE.write_text(str(proc.pid))
    log(f"central amctl launched pid={proc.pid}, log={log_file}")


# ---------- 4b. launchd integration (Mac host only) ----------

def kill_bootstrap_amctl():
    """Stop a previous bootstrap-launched central amctl before launchd takes over."""
    if not AMCTL_PID_FILE.exists():
        return
    try:
        pid = int(AMCTL_PID_FILE.read_text().strip())
        os.kill(pid, 15)  # SIGTERM
        time.sleep(0.5)
    except (ValueError, OSError):
        pass
    try:
        AMCTL_PID_FILE.unlink()
    except FileNotFoundError:
        pass


def ensure_launchd():
    """Install a launchd plist that manages the central amctl control node.

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
    _remove_pre_amctl_launchd_service()

    # launchd must depend only on the stable runtime path. The wrapper may be
    # refreshed from either Claude's or Codex's plugin cache without changing
    # the plist or restarting an otherwise healthy central service.
    amctl_path = BIN_LINK / "amctl"
    if not amctl_path.exists():
        msg = f"central amctl script missing: {amctl_path}"
        log(msg)
        blog(msg)
        return
    log_file = TMP / "amctl.log"

    plist = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [str(amctl_path), "--port", "8765"],
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
                    msg = "ensure_launchd: lock timeout (30s), skipping launchd operation"
                    log(msg)
                    blog(msg)
                    return
                time.sleep(0.5)

        _ensure_launchd_locked(new_bytes)
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


def _remove_pre_amctl_launchd_service():
    """Remove the pre-amctl launchd service before managing its replacement."""
    uid = os.getuid()
    old_target = f"gui/{uid}/{_PRE_AMCTL_LAUNCHD_LABEL}"
    subprocess.run(["launchctl", "bootout", old_target], capture_output=True)
    try:
        _PRE_AMCTL_LAUNCHD_PLIST.unlink()
    except FileNotFoundError:
        pass


def _wait_launchd_stopped(
    service_target: str,
    old_health: dict,
    total: float = 10.0,
    interval: float = 0.25,
) -> bool:
    """Wait until launchd drops the job and the previous HTTP instance is gone."""
    old_instance_id = _health_instance_id(old_health)
    deadline = time.monotonic() + max(0.0, total)
    consecutive_clear = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            listed = subprocess.run(
                ["launchctl", "print", service_target],
                capture_output=True,
                timeout=max(0.001, remaining),
            ).returncode == 0
        except subprocess.TimeoutExpired:
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        health = _amctl_health_info(timeout=min(1.0, remaining))
        if old_instance_id:
            old_instance_present = (
                bool(health)
                and _health_instance_id(health) == old_instance_id
            )
        else:
            # If the initial probe failed, absence of an instance id is not
            # proof that no old process exists. Treat any healthy endpoint as
            # occupied until the launchd job and endpoint are both stably gone.
            old_instance_present = bool(health)
        if not listed and not old_instance_present:
            consecutive_clear += 1
            if consecutive_clear >= 2:
                return True
        else:
            consecutive_clear = 0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(interval, remaining))


def _wait_new_amctl(
    expected_version: str,
    old_instance_id: str,
    total: float = 8.0,
    interval: float = 0.25,
    stable_checks: int = 2,
) -> bool:
    """Wait for one new, correctly versioned instance to stay healthy."""
    candidate_id = ""
    consecutive = 0
    deadline = time.monotonic() + max(0.0, total)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        health = _amctl_health_info(timeout=min(1.0, remaining))
        instance_id = _health_instance_id(health)
        valid = bool(
            instance_id
            and instance_id != old_instance_id
            and _health_version_matches(health, expected_version)
        )
        if valid:
            if instance_id == candidate_id:
                consecutive += 1
            else:
                candidate_id = instance_id
                consecutive = 1
            if consecutive >= stable_checks:
                return True
        else:
            candidate_id = ""
            consecutive = 0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(interval, remaining))


def _ensure_launchd_locked(new_bytes: bytes):
    """ensure_launchd 的实体逻辑；调用方持有跨进程文件锁后才能进入。"""
    global LAUNCHD_WARNING

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

    expected_version = _read_plugin_version()
    old_health = _amctl_health_info()
    if listed and not plist_changed:
        if (
            _health_version_matches(old_health, expected_version)
            and _health_instance_id(old_health)
        ):
            log(f"launchd already manages {LAUNCHD_LABEL} (healthy)")
            return
        if old_health:
            if _health_version_matches(old_health, expected_version):
                blog("launchd amctl has no instance_id, restarting")
            else:
                blog(
                    "launchd amctl version mismatch "
                    f"(running={old_health.get('version') or 'unknown'}, "
                    f"installed={expected_version}), restarting"
                )
        else:
            # A registered but unhealthy central amctl enters the self-heal path.
            blog("launchd listed but /health unreachable, entering self-heal path")

    if listed:
        subprocess.run(
            ["launchctl", "bootout", service_target],
            capture_output=True,
        )

    # Stop any session-bound central amctl so port 8765 is free.
    kill_bootstrap_amctl()
    if not _wait_launchd_stopped(service_target, old_health):
        warn = (
            "central amctl did not stop cleanly; "
            "refusing to start a second instance"
        )
        log(warn)
        blog(f"FAIL: {warn}")
        LAUNCHD_WARNING = warn
        return

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

    # 首次 bootstrap
    _do_bootstrap()
    previous_instance_id = _health_instance_id(old_health)
    if _wait_new_amctl(expected_version, previous_instance_id):
        msg = f"launchd loaded {LAUNCHD_LABEL}（auto-start on boot，KeepAlive on）"
        log(msg)
        blog(msg)
        return

    # 自愈重试，最多 2 次
    for attempt in range(1, 3):
        blog(f"post-bootstrap central amctl unhealthy, self-heal retry #{attempt}")
        retry_health = _amctl_health_info()
        subprocess.run(["launchctl", "bootout", service_target], capture_output=True)
        if not _wait_launchd_stopped(service_target, retry_health):
            blog(f"self-heal retry #{attempt}: previous instance did not stop")
            continue
        _do_bootstrap()
        if _wait_new_amctl(
            expected_version,
            _health_instance_id(retry_health),
        ):
            msg = f"launchd loaded {LAUNCHD_LABEL} (self-heal #{attempt} succeeded)"
            log(msg)
            blog(msg)
            return

    # 全部失败
    warn = (
        "central amctl failed to start automatically; "
        "run `meeting amctl restart` or check ~/.agent-meeting/logs/bootstrap.log"
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
# The supervisor itself owns central amctl keep-alive (instant relaunch on exit + 20s
# 假死 health probe). The only uncovered case — start before interactive logon
# (lock screen) — inherently needs a service = admin, so it is out of scope.

STARTUP_CMD = STARTUP_DIR / "agent-meeting-amctl.cmd"


def _supervisor_running() -> bool:
    try:
        pid = int(SUPERVISOR_PID_FILE.read_text().strip())
    except Exception:
        return False
    return pid_alive(pid)


def _launch_supervisor_now(pyw: Path, supervisor: Path):
    """Start the supervisor immediately so central amctl is
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
    """Install/refresh no-admin Windows persistence for central amctl and make
    sure the supervisor is running now. Idempotent like ensure_launchd: the
    Startup .cmd and the MINUTE task both embed the venv-pythonw + supervisor
    path, so we only rewrite/recreate when that path changes (plugin move)."""
    supervisor = BIN_LINK / "supervisor.py"
    if not supervisor.exists():
        log(f"supervisor missing: {supervisor}")
        return

    # The prior lifecycle names must not survive alongside central amctl:
    # the legacy task can restart a removed executable and keep port 8765 busy.
    subprocess.run(
        ["schtasks", "/Delete", "/TN", _PRE_AMCTL_SCHTASKS_TN, "/F"],
        capture_output=True,
    )
    try:
        _PRE_AMCTL_STARTUP_CMD.unlink()
    except FileNotFoundError:
        pass

    pyw = VENV / "Scripts" / "pythonw.exe"
    if not pyw.exists():
        pyw = venv_python()  # fall back to python.exe (console window)
    tr = f'"{pyw}" "{supervisor}"'

    # A fresh install/refresh means central amctl should be running — clear any
    # prior stop sentinel so the supervisor doesn't immediately bail.
    try:
        STOP_SENTINEL.unlink()
    except FileNotFoundError:
        pass
    kill_bootstrap_amctl()  # free :8765 from any old session-bound central amctl

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
    """Tear down Windows persistence and stop the central amctl control node."""
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
    # Stop a running supervisor (sentinel prevents relaunch) and central amctl.
    try:
        STOP_SENTINEL.write_text(str(int(time.time())))
    except Exception:
        pass
    for pidf in (AMCTL_PID_FILE, SUPERVISOR_PID_FILE):
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
    pid-file scheme was removed — never resurrect it.

    Displayed as name@project (the composite key), not bare name — two
    live sessions can share a name across different projects, and the CLI's
    send/read/show/turn already accept name@project to disambiguate. Global
    identities (project "*") drop the suffix, matching the display convention
    used everywhere else in this codebase (monitor.py's _display_id, meeting's
    _fmt_id, amctl's _fmt_id)."""
    if not DB.exists():
        return "(none online)"
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=2)
        try:
            cutoff = time.time() - 12
            rows = con.execute(
                "SELECT name, project FROM sessions WHERE last_seen >= ? ORDER BY name, project",
                (cutoff,),
            ).fetchall()
        finally:
            con.close()
        peers = [r[0] if r[1] == "*" else f"{r[0]}@{r[1]}" for r in rows]
        return ", ".join(peers) if peers else "(none online)"
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

    if os.environ.get("CODEX_THREAD_ID"):
        registration_context = """This is a Codex session. A `mycodex` launch supplies its exact agent-meeting recipient and control URL through thread and turn request parameters. Pass those values as explicit `meeting` CLI arguments; do not use `MEETING_SELF` or `MEETING_HOST`.

If no agent-meeting recipient is present in the current runtime context, this Codex session is not registered — and that's fine. Peer-to-peer agent messaging is optional. Never gate, block, or delay the user's work to register a name.

- Proceed with whatever the user asks, normally — registration is NOT a prerequisite for anything.
- If the user's message contains a `/meeting` command — whether it is the whole message (`/meeting <name>`) OR embedded inside a natural-language directive (e.g. "You are X, first run /meeting X --director to start listening") — run the meeting skill: they are explicitly opting in to register.
- Reminder line: on your first reply only, you may append the line below only when no agent-meeting recipient was injected and this session did not register:
  > 💡 This session has no meeting name yet; to communicate with other agents, use `/meeting <name>` to register (does not affect your current task).
  Show it at most once and never let it replace or postpone the actual task."""
    else:
        registration_context = """This session has NO meeting name yet — and that's fine. Peer-to-peer agent messaging is OPTIONAL. NEVER gate, block, or delay the user's work to make them register a name.

- Proceed with whatever the user asks, normally — registration is NOT a prerequisite for anything.
- If the user's message contains a `/meeting` command — whether it is the whole message (`/meeting <name>`) OR embedded inside a natural-language directive (e.g. "You are X, first run /meeting X --director to start listening") — run the meeting skill: they are explicitly opting in to register.
- Reminder line: on your FIRST reply of this session ONLY, you MAY append this single line at the very end — but SKIP it entirely whenever this session registers via `/meeting` (i.e. you run the meeting skill this turn), no matter where the command appeared in the user's message. Only show the reminder when the session does NOT register at all:
  > 💡 This session has no meeting name yet; to communicate with other agents, use `/meeting <name>` to register (does not affect your current task).
  Decide by your own action (did you register?), NOT by whether the message literally starts with `/meeting`. Show it at most once per session, never repeat it, and never let it replace or postpone the actual task."""

    ctx = f"""📞 Meeting system is active.

{registration_context}

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
       The wrapper's second line is: exec "<venv-py>" "<plugin-root>/bin/amctl"
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
        ensure_websockets()   # machine-wide Codex broker uses WebSockets

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
                else:
                    if amctl_running() and not _amctl_version_matches(version):
                        log("restarting Linux amctl after plugin version change")
                        kill_bootstrap_amctl()
                    if not amctl_running():
                        launch_amctl()             # Linux: session-bound for now
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
