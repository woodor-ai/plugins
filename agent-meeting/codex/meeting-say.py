#!/usr/bin/env python3
"""
agent-meeting: codex outbound send wrapper.

    meeting-say <peer> <body...>

The codex session's outbound entry point. It reads the session's own name and the
control URL from ~/.agent-meeting/codex/runtime.json, so codex does not need to
know its own meeting name or the control address — it just says who to send to and
what to say.

Body handling: the body may be given as a single (quoted) argument or as several
space-joined arguments; if no body args are given it is read from stdin. Whatever
the body is, it is passed to `meeting send` via a temp --body-file, so quotes,
newlines, and unicode (Chinese prose) survive verbatim with no shell re-quoting —
the only quoting the model has to get right is the ONE PowerShell argument.

Honors MEETING_HOME.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HOME = Path.home()
DATA = Path(os.environ.get("MEETING_HOME") or (HOME / ".agent-meeting"))
RUNTIME_JSON = DATA / "codex" / "runtime.json"
MEETING_CLI = DATA / "bin" / "meeting"


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: meeting-say <peer> <body...>   (body may also come from stdin)")
    peer = sys.argv[1]
    body = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else (sys.stdin.read() if not sys.stdin.isatty() else "")
    if not body.strip():
        sys.exit("meeting-say: empty body")

    try:
        rt = json.loads(RUNTIME_JSON.read_text(encoding="utf-8"))
    except Exception as e:
        sys.exit(f"meeting-say: cannot read {RUNTIME_JSON} ({e}). Is this a bridged codex session?")
    self_name = (rt.get("name") or "").strip()
    control = (rt.get("control_url") or "").strip()
    if not self_name:
        sys.exit("meeting-say: runtime.json has no 'name'")

    # Pass the body through a temp file so `meeting send` receives it verbatim,
    # immune to any further shell parsing.
    fd, tmp = tempfile.mkstemp(suffix=".txt", prefix="meeting-say-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        cmd = [sys.executable, str(MEETING_CLI), "send", self_name, peer,
               f"--body-file={tmp}", "--kind=回应"]
        if control:
            cmd += ["--host", control]
        kw = {"creationflags": 0x08000000} if sys.platform.startswith("win") else {}  # CREATE_NO_WINDOW
        r = subprocess.run(cmd, capture_output=True, text=True, **kw)
        if r.stdout:
            sys.stdout.write(r.stdout)
        if r.returncode != 0:
            sys.stderr.write(r.stderr or "meeting-say: send failed\n")
            sys.exit(r.returncode)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


if __name__ == "__main__":
    main()
