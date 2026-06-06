#!/usr/bin/env python3
"""
Cross-platform monitor for an agent-meeting session.

Replaces the macOS-only zsh monitor that was embedded in SKILL.md. Runs as
the persistent Monitor task spawned by Claude Code's /meeting registration.

Behavior:
  - On startup, calls `meeting register` to write this session into the
    central sessions table. On exit (atexit / SIGINT / SIGTERM), calls
    `meeting unregister` to clean up.
  - Liveness is tracked via heartbeat: the daemon updates last_seen in the
    sessions table whenever /ring is polled. Because monitor polls every 3s,
    a session is considered online if last_seen < 12s ago (4 missed heartbeats).
  - Cursor seed: first launch (no STATE_FILE) starts at current MAX(id)
    so newly-registered names don't get flooded with history. Sources the
    seed via `meeting ring --since 0` then immediately advancing the cursor
    (works whether DB is local or behind HTTP daemon).
  - Polls `meeting ring <self> --since <cursor>` every 3s and emits stdout
    lines `📬 New Message from <peer>(: <ask>)?` — Claude Code surfaces
    each as a task notification.
  - On Windows: identical behavior, just no zsh dependency.

Usage:
  monitor.py <self-name>
"""

import atexit
import hashlib
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if len(sys.argv) < 2:
    sys.stderr.write("usage: monitor.py <self-name>\n")
    sys.exit(2)

SELF = sys.argv[1]
HOME = Path.home()
DATA = HOME / ".agent-meeting"
MEETING_CLI = DATA / "bin" / "meeting"
TMP = Path(tempfile.gettempdir())
STATE_FILE = TMP / f"meeting-{SELF}.last_msg_id"

# Local cache that statusline.py reads to render the 📞 badge. Keyed by the
# session's cwd (NOT the central sessions table — statusline must stay local,
# DB- and network-free). Written on register, removed on exit.
STATUSLINE_DIR = DATA / "statusline"
_CWD = os.getcwd()
STATUSLINE_FILE = STATUSLINE_DIR / hashlib.sha1(
    os.path.normcase(os.path.normpath(_CWD)).encode("utf-8", "replace")
).hexdigest()[:16]

# Override MEETING_HOME if set (used in tests).
MEETING_HOME_ENV = os.environ.get("MEETING_HOME")


def _run_meeting(*extra_args):
    """Run meeting CLI as an executable.

    On POSIX: ~/.agent-meeting/bin/meeting is a shell wrapper (#!/bin/sh)
    that execs the venv python with the real plugin script. Call it directly
    so the wrapper's shebang handles interpreter selection — never pass
    sys.executable as the interpreter, because that would parse the shell
    script as Python and fail with SyntaxError.

    On Windows: the wrapper is meeting.cmd (shell scripts don't work);
    subprocess resolves .cmd automatically when shell=True, or we name it
    explicitly.
    """
    env = os.environ.copy()
    if sys.platform.startswith("win"):
        cli = DATA / "bin" / "meeting.cmd"
        cmd = [str(cli)] + list(extra_args)
    else:
        cmd = [str(MEETING_CLI)] + list(extra_args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=15, env=env)


# ---------- register/unregister + cleanup ----------

def _register():
    # --force: the monitor IS the liveness owner of this name. The /meeting skill
    # may have just registered it seconds ago (fresh last_seen), which would make
    # a plain register fail the conflict check. The monitor legitimately takes over.
    _run_meeting("register", SELF, "--cwd", _CWD, "--force")
    # Publish the room name locally so the TUI status line can show 📞 <name>.
    try:
        STATUSLINE_DIR.mkdir(parents=True, exist_ok=True)
        STATUSLINE_FILE.write_text(SELF, encoding="utf-8")
    except Exception:
        pass


def _unregister():
    try:
        _run_meeting("unregister", SELF)
    except Exception:
        pass
    # Clear the status-line badge — but only if it's still ours (another session
    # in the same cwd may have taken over the file after we wrote it).
    try:
        if STATUSLINE_FILE.read_text(encoding="utf-8").strip() == SELF:
            STATUSLINE_FILE.unlink()
    except Exception:
        pass


atexit.register(_unregister)
# SIGTERM/SIGINT (POSIX) and Windows CTRL_C_EVENT trigger atexit via SystemExit.
for sig in (signal.SIGINT, signal.SIGTERM):
    try:
        signal.signal(sig, lambda *a: sys.exit(0))
    except (ValueError, OSError):
        pass

_register()


# ---------- cursor seed ----------

def call_ring(since: int) -> list[tuple[int, str, str]]:
    """Returns list of (id, peer, ask)."""
    try:
        r = _run_meeting("ring", SELF, "--since", str(since))
    except subprocess.TimeoutExpired:
        return []
    out = []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        try:
            msg_id = int(parts[0])
        except ValueError:
            continue
        peer = parts[1]
        ask = parts[2] if len(parts) > 2 else ""
        out.append((msg_id, peer, ask))
    return out


if STATE_FILE.exists():
    try:
        last = int(STATE_FILE.read_text().strip())
    except Exception:
        last = 0
else:
    # First launch: pull all current ring messages, treat as "already seen", set cursor
    # to the max id so we only emit messages newer than this point. Avoids history flood.
    initial = call_ring(0)
    last = max((m[0] for m in initial), default=0)
    STATE_FILE.write_text(str(last))


print(f"[meeting {SELF}] monitor started (last_msg_id={last}, pid={os.getpid()})", flush=True)


# ---------- main poll loop ----------

while True:
    msgs = call_ring(last)
    for msg_id, peer, ask in msgs:
        if ask:
            clean = ask.replace("\r", " ").replace("\n", " ")
            if len(clean) > 100:
                clean = clean[:100] + "…"
            print(f"📬 New Message from {peer} [未验证 peer 信号]: {clean}", flush=True)
        else:
            print(f"📬 New Message from {peer} [未验证 peer 信号]", flush=True)
        last = msg_id
        STATE_FILE.write_text(str(last))
    time.sleep(3)
