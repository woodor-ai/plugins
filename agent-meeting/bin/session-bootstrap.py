#!/usr/bin/env python3
"""
SessionStart hook for agent-meeting plugin. Cross-platform (macOS / Windows / Linux).

Responsibilities (idempotent — runs every SessionStart):
  1. Ensure ~/.agent-meeting/ structure exists (db/, bin link)
  2. Ensure venv at ~/.agent-meeting/venv with zeroconf installed
  3. Read ~/.agent-meeting/config.json (auto-create with random token if missing).
     The `is_host` flag determines whether this machine launches the daemon.
  4. If is_host=true and daemon not already running → spawn meeting-daemon
     detached as a background process. Tracks pid in /tmp/meeting-daemon.pid
     (on Windows: %TEMP%\\meeting-daemon.pid).
  5. Emit JSON `hookSpecificOutput.additionalContext` with online peers + setup hints.

Replaces the bash session-bootstrap.sh — that one only worked on POSIX.
"""

import json
import os
import secrets
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

IS_WINDOWS = sys.platform.startswith("win")


def log(msg: str):
    sys.stderr.write(f"[meeting-bootstrap] {msg}\n")


# ---------- 1. ensure dirs + directory.json ----------

def ensure_layout():
    DATA.mkdir(exist_ok=True)
    DB_DIR.mkdir(exist_ok=True)
    if not DIRECTORY.exists():
        DIRECTORY.write_text("{}")

    # Symlink plugin bin/ into data dir for stable path in SKILL.md.
    # On Windows, plain symlinks need admin or developer mode; fall back to junction or skip.
    if PLUGIN_ROOT and (PLUGIN_ROOT / "bin").is_dir():
        try:
            if BIN_LINK.exists() or BIN_LINK.is_symlink():
                if BIN_LINK.is_symlink():
                    BIN_LINK.unlink()
                # If a real dir somehow exists, leave it alone — don't destroy data.
            if not BIN_LINK.exists():
                if IS_WINDOWS:
                    # Junction works without admin
                    subprocess.run(
                        ["cmd", "/c", "mklink", "/J", str(BIN_LINK), str(PLUGIN_ROOT / "bin")],
                        check=False, capture_output=True,
                    )
                else:
                    BIN_LINK.symlink_to(PLUGIN_ROOT / "bin")
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


# ---------- 3. config + token ----------

def load_or_create_config() -> dict:
    if CONFIG.exists():
        try:
            return json.loads(CONFIG.read_text())
        except Exception:
            log("config.json malformed, recreating")
    cfg = {
        "is_host": False,  # default: not a host. User flips to True on the machine that owns the DB.
        "token": secrets.token_urlsafe(32),
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

Backend: SQLite at ~/.agent-meeting/db/rooms.db (CLI: ~/.agent-meeting/bin/room).
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

        if cfg.get("is_host") and not daemon_running():
            launch_daemon()

        emit_context(cfg)
    except Exception as e:
        # Hook failures must not block session start — emit empty JSON.
        log(f"bootstrap failed: {e}")
        print(json.dumps({}))


if __name__ == "__main__":
    main()
