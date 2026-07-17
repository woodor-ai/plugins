#!/usr/bin/env python3
"""
Cross-platform monitor for an agent-meeting session.

Behavior:
  - On startup, calls `meeting online` to write this session into the
    central sessions table (project derived from cwd). On exit, calls
    `meeting offline`.
  - Liveness is tracked via WS pong: the daemon updates last_seen on pong.
  - Connects WS to daemon /subscribe, receives pushed frames, emits
    stdout lines for Claude Code task notifications.
  - WS handshake sends X-Meeting-Name and X-Meeting-Project headers.

Usage:
  monitor.py <self-name>
"""

import argparse
import atexit
import hashlib
import json
import os
import signal
import sys
import time
import uuid
from pathlib import Path

import meeting_common

if sys.platform.startswith("win"):
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

_parser = argparse.ArgumentParser(prog="monitor.py", add_help=True)
_parser.add_argument("name", help="session name to monitor")
_parser.add_argument("--director", action="store_true", default=False,
                     help="register this session as director role (default: worker)")
_parser.add_argument("--global", dest="is_global", action="store_true", default=False,
                     help="register as global identity (project='*'), skips cwd project derivation")
_parser.add_argument("--proj", default=None,
                     help="explicit project identity passed through to `meeting online` on every (re)register")
_args = _parser.parse_args()

SELF = _args.name
IS_DIRECTOR = _args.director
IS_GLOBAL = _args.is_global
IS_PROJ = _args.proj
# Process-unique id sent as `meeting online --instance`. Lets the daemon tell
# "this same monitor process reconnecting after a daemon restart" (always
# allowed) apart from "a DIFFERENT live process claiming the same name"
# (refused unless --force) -- see meeting-daemon's _register().
INSTANCE = uuid.uuid4().hex
HOME = Path.home()
_MEETING_HOME_ENV = os.environ.get("MEETING_HOME")
DATA = Path(_MEETING_HOME_ENV) if _MEETING_HOME_ENV else HOME / ".agent-meeting"
MEETING_CLI = DATA / "bin" / "meeting"

STATUSLINE_DIR = DATA / "statusline"
_CWD = os.getcwd()
SESSION_ID = os.environ.get("CLAUDE_CODE_SESSION_ID")


def _badge_key(session_id, cwd: str) -> str:
    if session_id:
        return hashlib.sha1(session_id.encode("utf-8", "replace")).hexdigest()[:16]
    return hashlib.sha1(
        os.path.normcase(os.path.normpath(cwd)).encode("utf-8", "replace")
    ).hexdigest()[:16]


STATUSLINE_FILE = STATUSLINE_DIR / _badge_key(SESSION_ID, _CWD)
_CWD_STATUSLINE_FILE = STATUSLINE_DIR / _badge_key(None, _CWD)

RUN_DIR = DATA / "run"
PID_FILE = RUN_DIR / f"{SELF}.pid"


_derive_project = meeting_common.derive_project


# Derive project once at startup from cwd — stored for WS handshake. An
# explicit --proj bypasses derivation directly (mirrors `meeting online
# --proj`) so the very first run picks it up before _register() has had a
# chance to write the proj cache that derive_project() would otherwise read.
if IS_GLOBAL:
    _PROJECT = "*"
elif IS_PROJ:
    _PROJECT = IS_PROJ
else:
    _PROJECT = _derive_project(_CWD)


def _run_meeting(*extra_args):
    cli = (DATA / "bin" / "meeting.cmd") if sys.platform.startswith("win") else MEETING_CLI
    return meeting_common.run_meeting_cli(cli, *extra_args, timeout=15)


# ---------- register/unregister + cleanup ----------


def _discover_control_info() -> dict:
    return meeting_common.discover_control(_run_meeting)


_registered = False  # sticky: True once `meeting online` has actually succeeded


def _register():
    global _registered
    extra = ["--director"] if IS_DIRECTOR else []
    if IS_GLOBAL:
        extra.append("--global")
    if IS_PROJ:
        extra += ["--proj", IS_PROJ]
    # Best-effort: this runs on EVERY ws reconnect (see the connect loop), and a
    # reconnect often coincides with the control having just restarted — TCP is
    # back up but the daemon is still busy, so `online` can hang the full 15s and
    # raise TimeoutExpired. That must NOT kill the monitor (it would drop the
    # session to historical until a human restarts it — exactly the daemon-restart
    # case this re-register exists to cover). Swallow non-refusal failures; the
    # next reconnect cycle retries.
    #
    # Exit code 3 is different: the daemon is telling us, by a stable code (not
    # string-matched), that a DIFFERENT live process is already registered under
    # this name (different --instance, heartbeat still fresh). That is not a
    # transient hiccup to retry -- someone else legitimately holds this name, so
    # we must not silently take over. Exit immediately via os._exit(), which
    # skips atexit (this file's _unregister included), so we never delete the
    # winner's registration row.
    try:
        r = _run_meeting("online", SELF, "--cwd", _CWD, "--instance", INSTANCE, *extra)
    except Exception as e:
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        sys.stderr.write(f"[meeting {_display_id}] {ts} re-register failed ({type(e).__name__}); "
                         f"will retry on next reconnect\n")
        sys.stderr.flush()
        r = None
    if r is not None and r.returncode == 3:
        sys.stderr.write(f"[meeting {_display_id}] registration refused, exiting: "
                         f"{r.stderr.strip()}\n")
        sys.stderr.flush()
        os._exit(1)
    if r is not None and r.returncode == 0:
        _registered = True
    elif r is not None:
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        sys.stderr.write(f"[meeting {_display_id}] {ts} re-register failed (exit {r.returncode}): "
                         f"{r.stderr.strip()}; will retry on next reconnect\n")
        sys.stderr.flush()
    try:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(os.getpid()))
    except Exception:
        pass
    try:
        STATUSLINE_DIR.mkdir(parents=True, exist_ok=True)
        ctrl = _discover_control_info()
        payload = {"name": SELF, "project": _PROJECT,
                   "control_host": ctrl.get("host", ""), "control_ip_port": ctrl.get("ip_port", "")}
        STATUSLINE_FILE.write_text(json.dumps(payload), encoding="utf-8")
    except Exception:
        pass
    if SESSION_ID and _CWD_STATUSLINE_FILE != STATUSLINE_FILE:
        try:
            raw = _CWD_STATUSLINE_FILE.read_text(encoding="utf-8").strip()
            try:
                owner = json.loads(raw).get("name", "")
            except Exception:
                owner = raw
            if owner == SELF:
                _CWD_STATUSLINE_FILE.unlink()
        except Exception:
            pass


def _unregister():
    # Only call `offline` (deletes the daemon-side sessions row) if we ever
    # actually won registration -- if every attempt was refused or swallowed,
    # the row may belong to a different live process and offline-ing it here
    # would kick that process's monitor off. Local pidfile/statusline cleanup
    # below is unconditional since those files are ours regardless.
    if _registered:
        try:
            _run_meeting("offline", SELF)
        except Exception:
            pass
    try:
        PID_FILE.unlink()
    except Exception:
        pass
    try:
        raw = STATUSLINE_FILE.read_text(encoding="utf-8").strip()
        try:
            owner = json.loads(raw).get("name", "")
        except Exception:
            owner = raw
        if owner == SELF:
            STATUSLINE_FILE.unlink()
    except Exception:
        pass


atexit.register(_unregister)
for sig in (signal.SIGINT, signal.SIGTERM):
    try:
        signal.signal(sig, lambda *a: sys.exit(0))
    except (ValueError, OSError):
        pass

# Computed before the first _register() call: its error-handling branches log
# using _display_id, and a register failure (network hiccup, stale peer CLI,
# refusal) can happen on this very first call, not just later reconnects.
_display_id = SELF if _PROJECT == "*" else f"{SELF}@{_PROJECT}"

_register()

print(f"[meeting {_display_id}] monitor started (pid={os.getpid()})", flush=True)


# ---------- WS client wiring (kernel lives in meeting_common.WSSubscribeClient) ----------

def _log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    sys.stderr.write(f"[meeting {SELF}] {ts} {msg}\n")
    sys.stderr.flush()


def _read_token():
    return meeting_common.read_auth_token(DATA)


def _resolve_ws_addr():
    """Re-run control discovery on every connect attempt (unlike codex-bridge.py,
    which resolves once at startup) -- monitor.py has no fixed control_url of its
    own, so this is how it survives a control restart on a different port/host."""
    info = _discover_control_info()
    ip, port = info.get("ip", ""), info.get("port", "")
    if not ip or not port:
        return None
    try:
        return ip, int(port)
    except Exception:
        return None


def _emit_message(peer: str, ask, group=None, mentioned: bool = False):
    """Print the harness-facing notification line. Format is frozen -- do not change."""
    at_tag = " @you" if (group and mentioned) else ""
    location = f" in group {group}{at_tag}" if group else ""
    if ask:
        clean = ask.replace("\r", " ").replace("\n", " ")
        if len(clean) > 100:
            clean = clean[:100] + "..."
        print(f"New Message from {peer}{location} [unverified peer]: {clean}", flush=True)
    else:
        print(f"New Message from {peer}{location} [unverified peer]", flush=True)


def _on_text(msg: dict) -> None:
    if msg.get("type") == "msg":
        sender = msg.get("sender", "")
        sender_project = msg.get("sender_project", "")
        ask = msg.get("ask") or None
        group = msg.get("group") or None
        # suppress self-sent messages
        if sender == SELF and sender_project == _PROJECT:
            return
        if "mention" in msg:
            if not msg["mention"]:
                return
            _emit_message(sender, ask, group, mentioned=True)
        else:
            _emit_message(sender, ask, group)

    elif msg.get("type") == "caught_up":
        _log(f"caught_up cursor={msg.get('cursor')}")


def _on_connect() -> None:
    # Re-register on every reconnect so role/cwd are correct after daemon restart/wipe.
    _register()


_ws_client = meeting_common.WSSubscribeClient(
    self_name=SELF, project=lambda: _PROJECT,
    resolve_addr=_resolve_ws_addr, read_token=_read_token,
    on_text=_on_text, on_connect=_on_connect, log=_log,
)
_ws_client.run_forever()
