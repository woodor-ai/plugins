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
import time
from pathlib import Path

HOME = Path.home()
DATA = HOME / ".agent-meeting"
DB_DIR = DATA / "db"
DB = DB_DIR / "rooms.db"
CONFIG = DATA / "config.json"
DIRECTORY = DATA / "directory.json"
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


def log(msg: str):
    sys.stderr.write(f"[meeting-bootstrap] {msg}\n")


# ---------- 1. ensure dirs + directory.json ----------

def ensure_layout():
    DATA.mkdir(exist_ok=True)
    DB_DIR.mkdir(exist_ok=True)
    if not DIRECTORY.exists():
        DIRECTORY.write_text("{}")


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
    meeting-daemon, meeting-migrate) become thin shell wrappers that exec the venv
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
            dest.with_suffix(".cmd").write_text(
                f'@echo off\r\n"{py}" "{src}" %*\r\n'
            )
        else:
            dest.write_text(f'#!/bin/sh\nexec "{py}" "{src}" "$@"\n')
            dest.chmod(0o755)

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


# ---------- 3. config ----------

def load_or_create_config() -> dict:
    if CONFIG.exists():
        try:
            return json.loads(CONFIG.read_text())
        except Exception:
            log("config.json malformed, recreating")
    cfg = {
        "is_host": False,  # default: not a host. User flips to True on the machine that owns the DB.
        "created_at": int(time.time()),
    }
    CONFIG.write_text(json.dumps(cfg, indent=2))
    try:
        os.chmod(CONFIG, 0o600)
    except Exception:
        pass
    return cfg


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
    """Install ~/Library/LaunchAgents/<label>.plist and load it. Idempotent:
    - Write fresh plist every time (paths may change if plugin reinstalls).
    - If already loaded with the same paths, no-op.
    - If loaded but plist content changed, bootout + bootstrap to pick up new ProgramArguments.
    macOS handles RunAtLoad + KeepAlive so the daemon survives reboots and crashes.
    """
    import plistlib

    LAUNCHD_PLIST.parent.mkdir(parents=True, exist_ok=True)

    daemon_path = PLUGIN_ROOT / "bin" / "meeting-daemon"
    if not daemon_path.exists():
        log(f"daemon script missing: {daemon_path}")
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
    old_bytes = LAUNCHD_PLIST.read_bytes() if LAUNCHD_PLIST.exists() else b""
    plist_changed = new_bytes != old_bytes
    if plist_changed:
        LAUNCHD_PLIST.write_bytes(new_bytes)

    # Is it currently loaded?
    uid = os.getuid()
    domain_target = f"gui/{uid}"
    listed = subprocess.run(
        ["launchctl", "print", f"{domain_target}/{LAUNCHD_LABEL}"],
        capture_output=True,
    ).returncode == 0

    if listed and not plist_changed:
        log(f"launchd already manages {LAUNCHD_LABEL}")
        return

    if listed and plist_changed:
        # Bootout to pick up the new plist
        subprocess.run(
            ["launchctl", "bootout", f"{domain_target}/{LAUNCHD_LABEL}"],
            capture_output=True,
        )

    # Kill any session-bound daemon so port 8765 is free
    kill_bootstrap_daemon()

    r = subprocess.run(
        ["launchctl", "bootstrap", domain_target, str(LAUNCHD_PLIST)],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        log(f"launchd loaded {LAUNCHD_LABEL} (auto-start on boot, KeepAlive on)")
    else:
        # Fallback to legacy syntax
        r2 = subprocess.run(
            ["launchctl", "load", "-w", str(LAUNCHD_PLIST)],
            capture_output=True, text=True,
        )
        if r2.returncode == 0:
            log(f"launchd loaded via legacy syntax: {LAUNCHD_LABEL}")
        else:
            log(f"launchd bootstrap failed: {r.stderr.strip() or r2.stderr.strip()}")


# ---------- 4c. status line (Claude Code TUI) ----------

def claude_settings_path() -> Path:
    """User-level Claude Code settings.json (honors CLAUDE_CONFIG_DIR)."""
    cfg_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(cfg_dir) if cfg_dir else (HOME / ".claude")
    return base / "settings.json"


def ensure_statusline():
    """Idempotently register our status-line command in Claude Code settings.

    Shows `📞 <room>  |  <model>  |  <dir>  |  <branch>` once a session has
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
    """Quick online-peer summary by checking pid files. Doesn't require daemon."""
    online = []
    if DIRECTORY.exists():
        try:
            directory = json.loads(DIRECTORY.read_text())
        except Exception:
            directory = {}
        for name in directory:
            pid_file = TMP / f"meeting-{name}.monitor_pid"
            if pid_file.exists():
                try:
                    pid = int(pid_file.read_text().strip())
                    if pid_alive(pid):
                        online.append(name)
                except (ValueError, OSError):
                    pass
    return ", ".join(online) if online else "(none online)"


def emit_context(cfg: dict):
    role = "host" if cfg.get("is_host") else "client"
    peers = online_peers_str()
    hostname = socket.gethostname()
    ctx = f"""📞 Meeting-room system is active.

This session has NO meeting name yet — you cannot make or receive calls until registered.

**MANDATORY first action**: if the user's first prompt is NOT any form of `/meeting` (with or without arguments), do NOT proceed with their task. Instead reply:

> 📞 Please name this session first. Three options:
> - `/meeting` — show picker of available names
> - `/meeting <name>` — register directly with a chosen name (2–20 chars, alphanumeric + hyphen)
> - `/meeting list` — see existing rooms / session names
>
> Once you pick a name, your phone is active and I'll continue with your request.

Backend: SQLite at ~/.agent-meeting/db/rooms.db (CLI: ~/.agent-meeting/bin/meeting).
Machine: `{hostname}` (role: {role}).
Online peers: {peers}
"""
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": ctx,
        }
    }))


# ---------- main ----------

def main():
    try:
        ensure_layout()       # base dirs first
        ensure_venv()         # venv must exist before wrappers reference its python
        ensure_zeroconf()
        ensure_bin_wrappers() # now venv python path is valid
        ensure_statusline()   # register TUI status line (idempotent, no-clobber)
        cfg = load_or_create_config()

        if cfg.get("is_host"):
            if IS_MAC:
                ensure_launchd()  # plist + KeepAlive — survives reboots
            elif not daemon_running():
                launch_daemon()   # Linux / Windows: session-bound for now

        emit_context(cfg)
    except Exception as e:
        # Hook failures must not block session start — emit empty JSON.
        log(f"bootstrap failed: {e}")
        print(json.dumps({}))


if __name__ == "__main__":
    main()
