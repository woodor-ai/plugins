#!/usr/bin/env python3
"""
Status line renderer for the agent-meeting plugin.

Claude Code invokes this on every status-line refresh, passing a JSON blob on
stdin (session_id, cwd, model, workspace, ...). We print ONE line to stdout
that Claude Code shows in the TUI status bar.

The line is composed of, in order (segments are dropped when unavailable):

    📞 <meeting-name>@<project> 🛰 <control>  |  <model> · <effort>  |  <dir>
      |  ctx <n>% left  |  5h <n>% left  |  wk <n>% left  |  tasks <done>/<n>
      |  v<claude-code-version>  |  <git-branch>

Segment order and wording deliberately mirror the Codex status line, whose
items are selected in ~/.codex/config.toml under [tui] status_line. Codex only
offers a fixed menu of built-in items and has no custom-command hook, so the
meeting badge has no Codex counterpart; every other segment does. Usage limits
render as REMAINING percent on both hosts because Codex offers no used-percent
item.

The meeting name is NOT looked up from the central SQLite DB (that would be
slow and would require mDNS/central-am-msgd discovery on every refresh, and wouldn't
work on client machines). Instead, monitor.py writes the registered session name
to a tiny local cache file keyed by the session's cwd when `/imagent <name>`
registers, and removes it on exit. This script just reads that file — purely
local, no network, no DB. When the session isn't registered (no cache file),
the 📞 badge is simply omitted.

Hard requirement: this must NEVER crash or hang. Any error → fall back to a
minimal line (or empty), never a traceback (which would land in the status bar).
"""

import hashlib
import json
import os
import sys
from pathlib import Path

# stdout is a pipe here, so Python picks the locale encoding -- cp1252 on a
# stock Windows box, which cannot encode the badge emoji. Without this the
# whole line raises UnicodeEncodeError and the status bar goes blank for every
# registered session. errors="replace" keeps a partial line alive if a peer
# name ever carries something even UTF-8-hostile.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DATA = Path(
    os.environ.get("MEETING_HOME") or (Path.home() / ".agent-meeting")
)
STATUSLINE_DIR = DATA / "statusline"
CLAUDE_TASKS_DIR = Path(
    os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")
) / "tasks"

SEP = "  |  "


def _badge_key(session_id: str | None, cwd: str) -> str:
    """Stable badge file key — matches the logic in monitor.py exactly."""
    if session_id:
        return hashlib.sha1(session_id.encode("utf-8", "replace")).hexdigest()[:16]
    norm = os.path.normcase(os.path.normpath(cwd))
    return hashlib.sha1(norm.encode("utf-8", "replace")).hexdigest()[:16]


def _parse_cache_file(f: "Path") -> dict:
    """Read and parse a single cache file. Returns {} if missing/unreadable."""
    try:
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
        # Old plain-text format: just the session name.
        return {"name": raw, "control_host": "", "control_ip_port": ""}
    except Exception:
        return {}


def _read_statusline_cache(cwd: str, session_id: str | None = None) -> dict:
    """Read the statusline cache for this session/cwd. Returns {} if not found.

    Lookup order when session_id is present:
      1. session-keyed file (sha1(session_id)[:16])
      2. cwd-keyed file (fallback for old monitor without session_id support)
    When session_id is absent, only the cwd-keyed file is checked.
    """
    try:
        if session_id:
            result = _parse_cache_file(STATUSLINE_DIR / _badge_key(session_id, cwd))
            if result:
                return result
            # Fallback: old monitor wrote only the cwd-keyed file.
            return _parse_cache_file(STATUSLINE_DIR / _badge_key(None, cwd))
        return _parse_cache_file(STATUSLINE_DIR / _badge_key(None, cwd))
    except Exception:
        return {}


def meeting_name(cwd: str, session_id: str | None = None) -> str:
    """Registered session name for this session/cwd, or '' if not registered."""
    return _read_statusline_cache(cwd, session_id).get("name", "")


def _control_label(cwd: str, session_id: str | None = None) -> str:
    """Return the control badge string, e.g. '🛰 10.0.0.5:8765'.

    Shows only the control's ip:port (no host/device name). Returns '' when
    there is no control info (e.g. a legacy plain-text cache) — it self-heals
    to the real control on the next register (which rewrites the cache in JSON
    form).
    """
    cache = _read_statusline_cache(cwd, session_id)
    if not cache.get("name"):
        return ""
    ip_port = cache.get("control_ip_port", "")
    if not ip_port:
        return ""
    return "\U0001F6F0 " + ip_port  # 🛰


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


def _percent_left(value) -> str:
    """Render a 0-100 usage number as remaining percent. '' when unusable.

    Claude Code reports how much is USED; Codex's built-in status line only
    offers a remaining-percent item, so both hosts render remaining here.
    """
    try:
        used = float(value)
    except (TypeError, ValueError):
        return ""
    left = 100 - used
    if left < 0:
        left = 0.0
    elif left > 100:
        left = 100.0
    return f"{round(left)}%"


def task_progress(session_id: str | None) -> str:
    """'<resolved>/<total>' for this session's task list, or '' when empty.

    Claude Code does not put the task list in the status-line payload, but it
    persists one small JSON file per task under <config>/tasks/<session-id>/.
    Reading that directory is local and cheap enough for a status-line refresh.
    """
    if not session_id:
        return ""
    try:
        directory = CLAUDE_TASKS_DIR / session_id
        if not directory.is_dir():
            return ""
        total = 0
        done = 0
        for entry in directory.iterdir():
            if entry.suffix != ".json":
                continue
            try:
                task = json.loads(entry.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(task, dict):
                continue
            total += 1
            if task.get("status") == "completed":
                done += 1
        return f"{done}/{total}" if total else ""
    except Exception:
        return ""


def _collapse_home(path: str) -> str:
    """Collapse the user's home-dir prefix to '~' so the path fits the status bar.

    '/Users/tommyclaw/AIAgent/plugins' -> '~/AIAgent/plugins'. Paths outside
    home are returned unchanged. Never raises — falls back to the input.
    """
    try:
        home = os.path.normpath(str(Path.home()))
        norm = os.path.normpath(path)
        if norm == home:
            return "~"
        if norm.startswith(home + os.sep):
            return "~" + norm[len(home):]
        return path
    except Exception:
        return path


def main():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}

    workspace = data.get("workspace") or {}
    cwd = workspace.get("current_dir") or data.get("cwd") or os.getcwd()
    model = (data.get("model") or {}).get("display_name") or ""
    effort = (data.get("effort") or {}).get("level") or ""
    session_id = data.get("session_id") or None
    context = data.get("context_window") or {}
    limits = data.get("rate_limits") or {}

    segments = []

    cache = _read_statusline_cache(cwd, session_id)
    name = cache.get("name", "")
    if name:
        # Show the project identity too (mirrors the `name@project` convention
        # used everywhere else); a global identity (project='*') or a missing
        # project renders as a bare name.
        project = cache.get("project", "")
        label = name if (not project or project == "*") else f"{name}@{project}"
        badge = f"\U0001F4DE {label}"  # 📞
        ctrl = _control_label(cwd, session_id)
        if ctrl:
            badge += f" {ctrl}"
        segments.append(badge)
    if model:
        # Codex renders its model item as "<model> <reasoning-level>"; keep the
        # same pairing so the two hosts read alike.
        segments.append(f"{model} · {effort}" if effort else model)
    if cwd:
        segments.append(_collapse_home(cwd))  # home-relative path (~/...) to avoid truncation
    ctx = _percent_left(context.get("used_percentage"))
    if ctx:
        segments.append(f"ctx {ctx} left")
    five_hour = _percent_left(
        (limits.get("five_hour") or {}).get("used_percentage")
    )
    if five_hour:
        segments.append(f"5h {five_hour} left")
    weekly = _percent_left(
        (limits.get("seven_day") or {}).get("used_percentage")
    )
    if weekly:
        segments.append(f"wk {weekly} left")
    tasks = task_progress(session_id)
    if tasks:
        segments.append(f"tasks {tasks}")
    version = data.get("version") or ""
    if version:
        segments.append(f"v{version}")
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
