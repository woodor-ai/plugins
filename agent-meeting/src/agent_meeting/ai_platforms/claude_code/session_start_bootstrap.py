"""
Claude Code SessionStart integration for agent-meeting.

Responsibilities (idempotent — runs every SessionStart):
  1. Ensure ~/.agent-meeting/ structure exists (db/, bin link)
  2. Ensure venv at ~/.agent-meeting/venv with zeroconf installed
  3. Read ~/.agent-meeting/config.json and migrate the legacy `is_host` value
     into ~/.agent-meeting/am-msgd.json when needed.
  4. Repair the loopback-first am-msgd user service without overriding a
     persisted operator stop.
  5. Emit JSON `hookSpecificOutput.additionalContext` with online peers + setup hints.

Replaces the bash session-bootstrap.sh — that one only worked on POSIX.
"""

import json
import os
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


from agent_meeting.ai_platforms.claude_code import (
    meeting_status_line,
    session_start_context,
)
from agent_meeting.installation import python_environment
from agent_meeting.installation.message_hub_service_installation import (
    ensure_configuration,
    ensure_local_message_hub_service,
)
from agent_meeting.operating_systems.macos import (
    message_hub_launch_agent,
)
from agent_meeting.operating_systems.windows import (
    message_hub_persistence,
)

if sys.platform.startswith("win"):
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

HOME = Path.home()
# Honor MEETING_HOME (same env the am CLI and monitor.py already respect) so
# the whole runtime can be relocated — required for isolated codex-only installs
# and testing on a machine that already has a live ~/.agent-meeting.
DATA = Path(os.environ.get("MEETING_HOME") or (HOME / ".agent-meeting"))
DB_DIR = DATA / "db"
DB = DB_DIR / "rooms.db"
CONFIG = DATA / "config.json"
VENV = DATA / "venv"
BIN_LINK = DATA / "bin"
ACTIVE_RUNTIME_MANIFEST = DATA / "active-runtime.json"

_CLAUDE_PLUGIN_ROOT = os.environ.get("CLAUDE_PLUGIN_ROOT")
_GENERIC_PLUGIN_ROOT = os.environ.get("PLUGIN_ROOT")
PLUGIN_ROOT = Path(_CLAUDE_PLUGIN_ROOT or _GENERIC_PLUGIN_ROOT or "")
TMP = Path(tempfile.gettempdir())
AM_MSGD_PID_FILE = TMP / "am-msgd.pid"
_LEGACY_AMCTL_PID_FILE = TMP / "amctl.pid"

IS_MAC = sys.platform == "darwin"
IS_WINDOWS = sys.platform.startswith("win")
IS_LINUX = sys.platform.startswith("linux")

LAUNCHD_LABEL = "com.tommy.agent-meeting.am-msgd"
LAUNCHD_PLIST = HOME / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
_LEGACY_AMCTL_LAUNCHD_LABEL = "com.tommy.agent-meeting.amctl"
_LEGACY_AMCTL_LAUNCHD_PLIST = (
    HOME / "Library" / "LaunchAgents" / f"{_LEGACY_AMCTL_LAUNCHD_LABEL}.plist"
)
_PRE_AM_MSGD_LAUNCHD_LABEL = "com.tommy.agent-meeting"
_PRE_AM_MSGD_LAUNCHD_PLIST = HOME / "Library" / "LaunchAgents" / f"{_PRE_AM_MSGD_LAUNCHD_LABEL}.plist"

# Windows: no-admin persistence is a Startup-folder launcher (primary logon
# auto-start) + a /SC MINUTE schtasks task (resurrects the supervisor process
# if it is killed mid-session). ONLOGON tasks need admin, so they are NOT used.
# Sentinel records the task command so we only recreate it when the plugin path
# moves (mirrors ensure_launchd).
SCHTASKS_TN = "agent-meeting-am-msgd"
_LEGACY_AMCTL_SCHTASKS_TN = "agent-meeting-amctl"
_PRE_AM_MSGD_SCHTASKS_TN = "agent-meeting-daemon"
SCHTASKS_SENTINEL = DATA / ".schtasks-cmd"
SUPERVISOR_PID_FILE = TMP / "meeting-supervisor.pid"
STOP_SENTINEL = DATA / "am-msgd.stopped"
STARTUP_DIR = HOME / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
_PRE_AM_MSGD_STARTUP_CMD = STARTUP_DIR / "agent-meeting-daemon.cmd"
_LEGACY_AMCTL_STARTUP_CMD = STARTUP_DIR / "agent-meeting-amctl.cmd"
_LEGACY_AMCTL_STOP_SENTINEL = DATA / "amctl.stopped"

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


def _am_msgd_health_info(port: int = 8765, timeout: float = 1.0) -> dict:
    """Return the central am-msgd health payload, or an empty dict."""
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


def _am_msgd_healthy(port: int = 8765, timeout: float = 1.0) -> bool:
    """Return whether the central am-msgd /health endpoint is available."""
    return bool(_am_msgd_health_info(port, timeout))


def _am_msgd_version_matches(expected: str, port: int = 8765) -> bool:
    """Return whether the live am-msgd is running the installed plugin version."""
    health = _am_msgd_health_info(port)
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


def _active_runtime_version() -> str | None:
    try:
        payload = json.loads(
            ACTIVE_RUNTIME_MANIFEST.read_text(encoding="utf-8")
        )
        version = str(payload.get("version") or "")
        runtime = Path(payload.get("runtime") or "")
        if version and runtime.is_dir() and not (runtime / ".installing").exists():
            return version
    except Exception:
        pass
    return None


def _runtime_command(name: str) -> Path:
    suffix = ".exe" if IS_WINDOWS else ""
    return BIN_LINK / f"{name}{suffix}"


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
    client fell back to local SQLite instead of connecting to the central am-msgd.

    New design: bin/ is a real directory. Extensionless scripts (meeting,
    am-msgd) become thin shell wrappers that exec the venv
    python with the real plugin script path. .py files (monitor.py, statusline.py,
    session-bootstrap.py, am_common.py) are COPIED, because callers
    explicitly pass `python3 ~/.agent-meeting/bin/foo.py` and so they must be
    real .py files. codex-session.py also imports the copied
    am_common.py from this directory.
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
        # Plugin-path equality alone cannot prove that the canonical launcher
        # was installed.
        if not (BIN_LINK / ("amcodex.cmd" if IS_WINDOWS else "amcodex")).exists():
            return False
        if IS_WINDOWS and not (BIN_LINK / "mycodex-impl.ps1").exists():
            return False
        if IS_WINDOWS and not (BIN_LINK / "amcodex-impl.ps1").exists():
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
    # partway, the existing BIN_LINK stays intact and concurrent `am` calls
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
                # .cmd wrapper for PATH/shell resolution (monitor, bare `am`).
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
        # self-heal here even if codex-session.py itself is (temporarily)
        # missing — its own "not installed" check handles that case at runtime.
        _mycodex_src_dir = PLUGIN_ROOT / "codex"
        if IS_WINDOWS and (_mycodex_src_dir / "mycodex-impl.ps1").exists():
            _shutil.copyfile(str(_mycodex_src_dir / "mycodex-impl.ps1"), str(tmp_bin / "mycodex-impl.ps1"))
            _shutil.copyfile(str(_mycodex_src_dir / "mycodex-impl.ps1"), str(tmp_bin / "amcodex-impl.ps1"))
            _shutil.copyfile(str(_mycodex_src_dir / "mycodex.cmd"), str(tmp_bin / "mycodex.cmd"))
            (tmp_bin / "amcodex.cmd").write_text(
                (_mycodex_src_dir / "mycodex.cmd")
                .read_text(encoding="utf-8")
                .replace("mycodex-impl.ps1", "amcodex-impl.ps1"),
                encoding="utf-8",
            )
        elif not IS_WINDOWS and (_mycodex_src_dir / "mycodex-posix.sh").exists():
            for _launcher_name in ("amcodex", "mycodex"):
                _dest_sh = tmp_bin / _launcher_name
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
        "amctl",
        "amctl.cmd",
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
    return python_environment.environment_python(
        VENV,
        is_windows=IS_WINDOWS,
    )


def ensure_venv():
    python_environment.ensure_python_environment(
        VENV,
        is_windows=IS_WINDOWS,
        log=log,
    )


def ensure_zeroconf():
    python_environment.ensure_python_dependency(
        venv_python(),
        "zeroconf",
        log=log,
    )


def ensure_websockets():
    # Required by the machine-wide am-codexd daemon, which proxies the remote TUI
    # and speaks JSON-RPC to the shared official Codex app-server.
    python_environment.ensure_python_dependency(
        venv_python(),
        "websockets",
        log=log,
    )


# ---------- 3. config ----------

def _read_plugin_version() -> str:
    """Read the invoking AI platform's manifest version.

    Claude hooks provide CLAUDE_PLUGIN_ROOT.  Codex installation provides the
    platform-neutral PLUGIN_ROOT.  Keeping this selection explicit prevents a
    Codex runtime upgrade from silently taking its version from Claude metadata.
    """
    try:
        manifest_dir = ".claude-plugin" if _CLAUDE_PLUGIN_ROOT else ".codex-plugin"
        pj = PLUGIN_ROOT / manifest_dir / "plugin.json"
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
                    _PRE_AM_MSGD_LAUNCHD_PLIST.exists()
                    or _LEGACY_AMCTL_LAUNCHD_PLIST.exists()
                    or LAUNCHD_PLIST.exists()
                )
            elif IS_WINDOWS:
                legacy_host = (
                    _PRE_AM_MSGD_STARTUP_CMD.exists()
                    or _LEGACY_AMCTL_STARTUP_CMD.exists()
                    or (STARTUP_DIR / "agent-meeting-am-msgd.cmd").exists()
                )
            elif IS_LINUX:
                legacy_host = (
                    (TMP / "meeting-daemon.pid").exists()
                    or _LEGACY_AMCTL_PID_FILE.exists()
                    or AM_MSGD_PID_FILE.exists()
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


# ---------- 4. central am-msgd launch ----------

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


def am_msgd_running() -> bool:
    if not AM_MSGD_PID_FILE.exists():
        return False
    try:
        pid = int(AM_MSGD_PID_FILE.read_text().strip())
    except Exception:
        return False
    return pid_alive(pid)


def launch_am_msgd():
    """Launch the session-bound central am-msgd fallback."""
    packaged_command = _runtime_command("am-msgd")
    standalone = packaged_command.exists()
    am_msgd_path = (
        packaged_command
        if standalone
        else PLUGIN_ROOT / "bin" / "am-msgd"
    )
    if not am_msgd_path.exists():
        log(f"central am-msgd command missing: {am_msgd_path}")
        return
    command = (
        [
            str(am_msgd_path),
            "serve",
            "--config",
            str(DATA / "am-msgd.json"),
        ]
        if standalone
        else [
            str(venv_python()),
            str(am_msgd_path),
            "serve",
            "--config",
            str(DATA / "am-msgd.json"),
        ]
    )
    log_file = TMP / "am-msgd.log"
    # Detach central am-msgd so it survives hook exit and Claude Code session close.
    if IS_WINDOWS:
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        flags = 0x00000008 | 0x00000200
        proc = subprocess.Popen(
            command,
            stdout=open(log_file, "a"),
            stderr=subprocess.STDOUT,
            creationflags=flags,
            close_fds=True,
        )
    else:
        proc = subprocess.Popen(
            command,
            stdout=open(log_file, "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    AM_MSGD_PID_FILE.write_text(str(proc.pid))
    log(f"central am-msgd launched pid={proc.pid}, log={log_file}")


# ---------- 4b. launchd integration (Mac host only) ----------

def kill_bootstrap_am_msgd():
    """Stop bootstrap-launched message hubs before OS persistence takes over."""
    for pid_file in (AM_MSGD_PID_FILE, _LEGACY_AMCTL_PID_FILE):
        if not pid_file.exists():
            continue
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 15)  # SIGTERM
            time.sleep(0.5)
        except (ValueError, OSError):
            pass
        try:
            pid_file.unlink()
        except FileNotFoundError:
            pass


def ensure_launchd():
    """Install a launchd plist that manages the central am-msgd session/message hub.

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
    message_hub_launch_agent.ensure_message_hub_launch_agent(
        plist_path=LAUNCHD_PLIST,
        lock_path=DATA / "run" / "launchd.lock",
        label=LAUNCHD_LABEL,
        message_hub_command=BIN_LINK / "am-msgd",
        configuration_path=DATA / "am-msgd.json",
        log_path=TMP / "am-msgd.log",
        remove_legacy_jobs=_remove_pre_am_msgd_launchd_service,
        install_locked=_ensure_launchd_locked,
        log=log,
        persistent_log=blog,
    )


def _remove_pre_am_msgd_launchd_service():
    """Remove service names that predate am-msgd before installing it."""
    message_hub_launch_agent.remove_legacy_message_hub_launch_agents(
        (
            message_hub_launch_agent.LegacyMessageHubLaunchAgent(
                _PRE_AM_MSGD_LAUNCHD_LABEL,
                _PRE_AM_MSGD_LAUNCHD_PLIST,
            ),
            message_hub_launch_agent.LegacyMessageHubLaunchAgent(
                _LEGACY_AMCTL_LAUNCHD_LABEL,
                _LEGACY_AMCTL_LAUNCHD_PLIST,
            ),
        )
    )


def _wait_launchd_stopped(
    service_target: str,
    old_health: dict,
    total: float = 10.0,
    interval: float = 0.25,
) -> bool:
    """Wait until launchd drops the job and the previous HTTP instance is gone."""
    return message_hub_launch_agent.wait_until_message_hub_stopped(
        service_target=service_target,
        old_health=old_health,
        health_probe=_am_msgd_health_info,
        health_instance_id=_health_instance_id,
        total=total,
        interval=interval,
    )


def _wait_new_am_msgd(
    expected_version: str,
    old_instance_id: str,
    total: float = 8.0,
    interval: float = 0.25,
    stable_checks: int = 2,
) -> bool:
    """Wait for one new, correctly versioned instance to stay healthy."""
    return message_hub_launch_agent.wait_for_new_message_hub(
        expected_version=expected_version,
        old_instance_id=old_instance_id,
        health_probe=_am_msgd_health_info,
        health_instance_id=_health_instance_id,
        health_version_matches=_health_version_matches,
        total=total,
        interval=interval,
        stable_checks=stable_checks,
    )


def _ensure_launchd_locked(new_bytes: bytes):
    """ensure_launchd 的实体逻辑；调用方持有跨进程文件锁后才能进入。"""
    global LAUNCHD_WARNING
    warning = (
        message_hub_launch_agent.install_message_hub_launch_agent_locked(
            launch_agent_bytes=new_bytes,
            plist_path=LAUNCHD_PLIST,
            label=LAUNCHD_LABEL,
            expected_version=_read_plugin_version(),
            health_probe=_am_msgd_health_info,
            health_instance_id=_health_instance_id,
            health_version_matches=_health_version_matches,
            stop_bootstrap_message_hub=kill_bootstrap_am_msgd,
            wait_until_stopped=_wait_launchd_stopped,
            wait_for_new=_wait_new_am_msgd,
            log=log,
            persistent_log=blog,
        )
    )
    if warning:
        LAUNCHD_WARNING = warning


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
# The supervisor itself owns central am-msgd keep-alive (instant relaunch on exit + 20s
# 假死 health probe). The only uncovered case — start before interactive logon
# (lock screen) — inherently needs a service = admin, so it is out of scope.

STARTUP_CMD = STARTUP_DIR / "agent-meeting-am-msgd.cmd"


def _supervisor_running() -> bool:
    try:
        pid = int(SUPERVISOR_PID_FILE.read_text().strip())
    except Exception:
        return False
    return pid_alive(pid)


def _launch_supervisor_now(pyw: Path, supervisor: Path):
    """Start the supervisor immediately so central am-msgd is
    up this session without waiting for the Startup launcher or the MINUTE task.
    No-op if one is already alive (the supervisor's own singleton guard would
    make a second one exit anyway)."""
    if _supervisor_running():
        return
    try:
        command = (
            [str(supervisor)]
            if supervisor.suffix.lower() == ".exe"
            else [str(pyw), str(supervisor)]
        )
        subprocess.Popen(command,
                         creationflags=0x00000008 | 0x00000200, close_fds=True)
    except Exception as e:
        log(f"supervisor launch failed: {e}")


def _windows_message_hub_persistence_paths():
    return message_hub_persistence.MessageHubPersistencePaths(
        startup_directory=STARTUP_DIR,
        startup_command=STARTUP_CMD,
        pre_message_hub_startup_command=_PRE_AM_MSGD_STARTUP_CMD,
        legacy_amctl_startup_command=_LEGACY_AMCTL_STARTUP_CMD,
        task_action_sentinel=SCHTASKS_SENTINEL,
        message_hub_stop_sentinel=STOP_SENTINEL,
        legacy_amctl_stop_sentinel=_LEGACY_AMCTL_STOP_SENTINEL,
        message_hub_pid_file=AM_MSGD_PID_FILE,
        legacy_amctl_pid_file=_LEGACY_AMCTL_PID_FILE,
        supervisor_pid_file=SUPERVISOR_PID_FILE,
    )


def ensure_windows_persistence():
    """Install/refresh no-admin Windows persistence for central am-msgd and make
    sure the supervisor is running now. Idempotent like ensure_launchd: the
    Startup .cmd and the MINUTE task both embed the venv-pythonw + supervisor
    path, so we only rewrite/recreate when that path changes (plugin move)."""
    packaged_supervisor = _runtime_command("am-message-hub-supervisor")
    supervisor = (
        packaged_supervisor
        if packaged_supervisor.exists()
        else BIN_LINK / "supervisor.py"
    )
    if not supervisor.exists():
        log(f"supervisor missing: {supervisor}")
        return

    pyw = VENV / "Scripts" / "pythonw.exe"
    if not pyw.exists():
        pyw = venv_python()  # fall back to python.exe (console window)
    message_hub_persistence.ensure_message_hub_persistence(
        paths=_windows_message_hub_persistence_paths(),
        pythonw_executable=pyw,
        supervisor_command=supervisor,
        stop_bootstrap_message_hub=kill_bootstrap_am_msgd,
        launch_supervisor=_launch_supervisor_now,
        log=log,
    )


def remove_windows_persistence():
    """Tear down Windows persistence and stop the central am-msgd session/message hub."""
    message_hub_persistence.remove_message_hub_persistence(
        paths=_windows_message_hub_persistence_paths(),
        log=log,
    )


# ---------- 4c. status line (Claude Code TUI) ----------

def claude_settings_path() -> Path:
    """User-level Claude Code settings.json (honors CLAUDE_CONFIG_DIR)."""
    cfg_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(cfg_dir) if cfg_dir else (HOME / ".claude")
    return base / "settings.json"


def ensure_statusline():
    """Idempotently register our status-line command in Claude Code settings.

    Shows `📞 <name>  |  <model>  |  <dir>  |  <branch>` once a session has
    registered via /imagent (the badge self-gates: statusline.py only renders it
    when monitor.py has written the local name cache for this cwd).

    Conservative: if the user already has a *different* statusLine configured,
    we leave it untouched rather than clobber it. We only install/refresh when
    statusLine is absent or already points at our statusline.py.
    """
    active_statusline = _runtime_command("am-statusline")
    legacy_statusline = BIN_LINK / "statusline.py"
    statusline_command = (
        active_statusline if active_statusline.exists() else legacy_statusline
    )
    meeting_status_line.install_meeting_status_line(
        settings_path=claude_settings_path(),
        python_executable=(
            None if active_statusline.exists() else venv_python()
        ),
        statusline_command=statusline_command,
        log=log,
    )


# ---------- 5. context emission ----------

def online_peers_str() -> str:
    """Online peers = sessions-table rows with a fresh heartbeat (last_seen
    within 12s). Reads rooms.db read-only. The old directory.json + monitor
    pid-file scheme was removed — never resurrect it.

    Displayed as name@project (the composite key), not bare name — two
    live sessions can share a name across different projects, and the CLI's
    send/read/show/turn already accept name@project to disambiguate. Global
    identities (project "*") drop the suffix, matching the display convention
    used everywhere else in this codebase (monitor.py's _display_id, am's
    _fmt_id, am-msgd's _fmt_id)."""
    return session_start_context.read_online_peers(DB)


def emit_context(cfg: dict):
    active_monitor = _runtime_command("am-session-monitor")
    monitor_command = (
        active_monitor
        if active_monitor.exists()
        else BIN_LINK / "monitor.py"
    )
    print(
        session_start_context.serialize_session_start_payload(
            config=cfg,
            database_path=DB,
            am_command=BIN_LINK / "am",
            monitor_script=monitor_command,
            python_executable=venv_python(),
            is_windows=IS_WINDOWS,
            is_codex_thread=bool(
                os.environ.get("CODEX_THREAD_ID")
                or os.environ.get("AGENT_MEETING_CODEX_RUNTIME")
            ),
            launchd_warning=LAUNCHD_WARNING,
            online_peers=online_peers_str(),
            standalone_commands=active_monitor.exists(),
        )
    )


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
       The wrapper's second line is: exec "<venv-py>" "<plugin-root>/bin/am-msgd"
       The plugin root is a versioned cache dir like .../agent-meeting/0.8.0/...
    2. config.json plugin_version field.
    3. .bin-plugin-root sentinel (contains the plugin_bin path, version segment embedded).

    Returns None if no runtime is present (fresh install → caller treats as no downgrade).
    """
    # 1. Parse wrapper exec path
    wrapper = DATA / "bin" / "am"
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
        active_runtime_version = _active_runtime_version()
        if active_runtime_version is None:
            ensure_venv()         # legacy runtime compatibility path
            ensure_zeroconf()
            ensure_websockets()

        # Monotonic-upgrade guard: skip runtime rewrite if this session's plugin
        # version is older than what's already installed.
        session_ver = _read_plugin_version()
        installed_ver = active_runtime_version or _read_installed_version()
        skip_runtime_rewrite = active_runtime_version is not None
        if active_runtime_version is not None:
            log(
                "using activated host runtime "
                f"{active_runtime_version}; SessionStart will not rewrite it"
            )
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

        # Every installation owns a loopback am-msgd user service. `is_host`
        # is migrated only into the initial bind-list and no longer decides
        # whether the daemon exists.
        message_hub_configuration = ensure_configuration(DATA)
        if IS_WINDOWS and message_hub_configuration.enabled:
            ensure_windows_persistence()
        ensure_local_message_hub_service(
            DATA,
            restart_enabled_service=False,
        )

        emit_context(cfg)
    except Exception as e:
        # Hook failures must not block session start — emit empty JSON.
        log(f"bootstrap failed: {e}")
        print(json.dumps({}))


if __name__ == "__main__":
    main()
