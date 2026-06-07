#!/usr/bin/env python3
"""
Status line renderer for the agent-meeting plugin.

Claude Code invokes this on every status-line refresh, passing a JSON blob on
stdin (session_id, cwd, model, workspace, ...). We print ONE line to stdout
that Claude Code shows in the TUI status bar.

The line is composed of, in order (segments are dropped when unavailable):

    📞 <meeting-name>  |  <model>  |  <dir>  |  <git-branch>

The meeting name is NOT looked up from the central SQLite DB (that would be
slow and would require mDNS/daemon discovery on every refresh, and wouldn't
work on client machines). Instead, monitor.py writes the registered room name
to a tiny local cache file keyed by the session's cwd when `/meeting <name>`
registers, and removes it on exit. This script just reads that file — purely
local, no network, no DB. When the session isn't registered (no cache file),
the 📞 badge is simply omitted.

Hard requirement: this must NEVER crash or hang. Any error → fall back to a
minimal line (or empty), never a traceback (which would land in the status bar).
"""

import hashlib
import json
import os
import socket
import sys
from pathlib import Path

DATA = Path.home() / ".agent-meeting"
STATUSLINE_DIR = DATA / "statusline"

SEP = "  |  "


def cwd_key(cwd: str) -> str:
    """Stable per-directory key shared with monitor.py. Case/sep-normalized."""
    norm = os.path.normcase(os.path.normpath(cwd))
    return hashlib.sha1(norm.encode("utf-8", "replace")).hexdigest()[:16]


def _read_statusline_cache(cwd: str) -> dict:
    """Read the statusline cache for this cwd.  Returns {} if missing/unreadable.

    Supports both the old plain-text format (just the room name) and the new
    JSON format {room, control_host, control_ip_port}.
    """
    try:
        f = STATUSLINE_DIR / cwd_key(cwd)
        if not f.exists():
            return {}
        raw = f.read_text(encoding="utf-8").strip()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        # Old plain-text format: just the room name.
        return {"room": raw, "control_host": "", "control_ip_port": ""}
    except Exception:
        return {}


def meeting_name(cwd: str) -> str:
    """Registered room name for this cwd, or '' if not registered."""
    return _read_statusline_cache(cwd).get("room", "")


def _control_label(cwd: str) -> str:
    """Return the control badge string, e.g. '🛰 meeting-control myhost 10.0.0.5:8765'.

    Returns '🛰 meeting-control self' when connected to localhost. Returns ''
    when there is no control info (e.g. a legacy plain-text cache) — we don't
    assert a state we don't know; it self-heals to the real control on the next
    register (which rewrites the cache in JSON form).
    """
    cache = _read_statusline_cache(cwd)
    if not cache.get("room"):
        return ""
    host = cache.get("control_host", "")
    ip_port = cache.get("control_ip_port", "")
    if not host and not ip_port:
        return ""
    # Detect self: host matches local hostname or IP is loopback.
    local_host = socket.gethostname().replace(".local", "")
    ip = ip_port.split(":")[0] if ip_port else ""
    if host == local_host or ip in ("127.0.0.1", "::1", "localhost"):
        return "\U0001F6F0 meeting-control self"  # 🛰
    parts = []
    if host:
        parts.append(host)
    if ip_port:
        parts.append(ip_port)
    return "\U0001F6F0 meeting-control " + " ".join(parts)  # 🛰


def git_branch(cwd: str) -> str:
    """Current branch by reading .git/HEAD (no subprocess). '' if not a repo."""
    try:
        d = Path(cwd)
        for _ in range(40):  # bounded walk toward filesystem root
            git = d / ".git"
            if git.is_dir():
                head_dir = git
            elif git.is_file():
                # worktree / submodule: ".git" is a file → "gitdir: <path>"
                txt = git.read_text(encoding="utf-8", errors="replace").strip()
                if txt.startswith("gitdir:"):
                    head_dir = Path(txt.split(":", 1)[1].strip())
                else:
                    return ""
            else:
                if d.parent == d:
                    return ""
                d = d.parent
                continue

            head = (head_dir / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
            if head.startswith("ref:"):
                return head.split("/")[-1]  # refs/heads/<branch> → <branch>
            return head[:7]  # detached HEAD → short sha
        return ""
    except Exception:
        return ""


def main():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}

    workspace = data.get("workspace") or {}
    cwd = workspace.get("current_dir") or data.get("cwd") or os.getcwd()
    model = (data.get("model") or {}).get("display_name") or ""

    segments = []

    name = meeting_name(cwd)
    if name:
        badge = f"\U0001F4DE {name}"  # 📞
        ctrl = _control_label(cwd)
        if ctrl:
            badge += f" {ctrl}"
        segments.append(badge)
    if model:
        segments.append(model)
    if cwd:
        segments.append(cwd)  # full absolute path (not just basename)
    branch = git_branch(cwd)
    if branch:
        segments.append(branch)

    sys.stdout.write(SEP.join(segments))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Absolute last resort — never emit a traceback into the status bar.
        sys.stdout.write("")
