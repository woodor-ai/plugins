#!/usr/bin/env python3
"""
Cross-platform monitor for an agent-meeting session.

Replaces the macOS-only zsh monitor that was embedded in SKILL.md. Runs as
the persistent Monitor task spawned by Claude Code's /meeting registration.

Behavior:
  - Writes own PID to /tmp/meeting-<self>.monitor_pid (liveness signal for
    `meeting list`). trap-cleans on exit, EVEN ON WINDOWS via atexit.
  - On exit, also removes the session's directory.json entry — same logic
    as session-cleanup.sh — so normal /exit doesn't leave `empty` zombies.
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
import json
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
DIRECTORY = DATA / "directory.json"
MEETING_CLI = DATA / "bin" / "meeting"
TMP = Path(tempfile.gettempdir())
PID_FILE = TMP / f"meeting-{SELF}.monitor_pid"
STATE_FILE = TMP / f"meeting-{SELF}.last_msg_id"


# ---------- liveness signal + cleanup ----------

def cleanup(*_):
    """Remove pid file + directory entry on exit. Mirror of session-cleanup.sh."""
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass

    # Atomic-ish edit of directory.json: read, del entry, write.
    # Real concurrency safety would need a lock, but writes here are rare.
    try:
        if DIRECTORY.exists():
            d = json.loads(DIRECTORY.read_text())
            if SELF in d:
                del d[SELF]
                tmp = DIRECTORY.with_suffix(".tmp")
                tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False))
                tmp.replace(DIRECTORY)
    except Exception:
        pass


atexit.register(cleanup)
# SIGTERM/SIGINT (POSIX) and Windows CTRL_C_EVENT trigger atexit via SystemExit.
for sig in (signal.SIGINT, signal.SIGTERM):
    try:
        signal.signal(sig, lambda *a: sys.exit(0))
    except (ValueError, OSError):
        pass

PID_FILE.write_text(str(os.getpid()))


# ---------- cursor seed ----------

def call_ring(since: int) -> list[tuple[int, str, str]]:
    """Returns list of (id, peer, ask)."""
    try:
        # Invoke the CLI through the *current* interpreter, not the bare script
        # path. The script is extensionless with a `#!/usr/bin/env python3`
        # shebang — fine on POSIX, but Windows has no shebang support and bare
        # `python3` there is a non-functional Microsoft Store stub. sys.executable
        # is whatever launched this monitor (the venv python on Windows), which is
        # guaranteed to run the CLI and to have zeroconf available.
        r = subprocess.run(
            [sys.executable, str(MEETING_CLI), "ring", SELF, "--since", str(since)],
            capture_output=True, text=True, timeout=15,
        )
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


print(f"[meeting {SELF}] monitor started (last_msg_id={last}, monitor_pid={os.getpid()})", flush=True)


# ---------- main poll loop ----------

while True:
    msgs = call_ring(last)
    for msg_id, peer, ask in msgs:
        if ask:
            print(f"📬 New Message from {peer}: {ask}", flush=True)
        else:
            print(f"📬 New Message from {peer}", flush=True)
        last = msg_id
        STATE_FILE.write_text(str(last))
    time.sleep(3)
