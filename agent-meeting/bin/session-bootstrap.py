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

    # ~/.agent-meeting/bin must be a symlink → the current plugin's bin/ so that
    # SKILL.md / monitor.py can reference one stable path (~/.agent-meeting/bin/meeting)
    # regardless of which cache version is active. Plugin upgrades change PLUGIN_ROOT;
    # this resync makes the stable path follow the latest code automatically.
    #
    # This is IDEMPOTENT and SELF-HEALING: if bin/ got corrupted into a real directory
    # (e.g. someone cp'd files in, or an old `ln -sfn target dir` nested a bin/bin),
    # we move the junk aside and rebuild the symlink. The data dir never legitimately
    # contains a real bin/ — all user state is in db/, directory.json, config.json.
    if not PLUGIN_ROOT or not (PLUGIN_ROOT / "bin").is_dir():
        return

    desired = (PLUGIN_ROOT / "bin").resolve()
    try:
        # Already correct? no-op.
        if BIN_LINK.is_symlink() and BIN_LINK.resolve() == desired:
            return

        # Anything else occupying the path must go.
        if BIN_LINK.is_symlink():
            BIN_LINK.unlink()
        elif BIN_LINK.is_dir():
            # Real directory (pollution). Move aside instead of deleting, just in case.
            import shutil
            bak = BIN_LINK.with_name(f"bin.corrupt-{int(time.time())}")
            shutil.move(str(BIN_LINK), str(bak))
            log(f"moved corrupted bin/ aside → {bak.name}")
        elif BIN_LINK.exists():
            BIN_LINK.unlink()

        if IS_WINDOWS:
            # Junction works without admin / developer mode.
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(BIN_LINK), str(desired)],
                check=False, capture_output=True,
            )
        else:
            BIN_LINK.symlink_to(desired)
        log(f"linked bin/ → {desired}")
    except Exception as e:
        log(f"could not link bin/: {e}")


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

def daemon_running() -> bool:
    if not DAEMON_PID_FILE.exists():
        return False
    try:
        pid = int(DAEMON_PID_FILE.read_text().strip())
    except Exception:
        return False
    try:
        if IS_WINDOWS:
            # On Windows there's no kill -0; use os.kill(pid, 0) which raises OSError if dead
            os.kill(pid, 0)
        else:
            os.kill(pid, 0)
        return True
    except OSError:
        return False


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
                    os.kill(pid, 0)
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
        ensure_layout()
        ensure_venv()
        ensure_zeroconf()
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
