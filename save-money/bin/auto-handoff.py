#!/usr/bin/env python3
"""
cost-auto-handoff hook — dual-host (Claude Code Stop / Codex PostToolUse)

Claude Code path (hook_event_name == "Stop"):
  Reads the session transcript JSONL for the last assistant usage block,
  derives context_tokens, compares against per-family thresholds from
  ~/.claude/cost-opt.json, and if exceeded dispatches a local am-ctl
  handoff+restart operation after the hook returns.

Codex path (hook_event_name == "PostToolUse"):
  Reads the session transcript JSONL for the latest token_count event,
  derives input_tokens and model_context_window directly from the JSONL
  (no static WINDOW_TOKENS lookup needed — Codex embeds the window size).
  If context_pct >= configured threshold, dispatches the same local am-ctl
  operation. No lifecycle instruction is sent through agent chat/messages.

Dedup: ~/.cache/cost-auto-handoff/fired/<session_id> prevents repeat fires
within the same session on both hosts.
"""

import json
import os
import subprocess
import sys
import time

# Effective context window per Claude family. Opus/sonnet run the 1M-context
# beta; haiku runs the 200k standard window. Not detectable from the Stop-hook
# payload itself, so asserted by family. Erring large is safe: if a session
# isn't actually on 1M it auto-compacts near 200k and this threshold never
# fires — no false restart. The reverse (assuming 200k on a real 1M session)
# would restart at ~120k, far too early.
# Not used for Codex: model_context_window comes from the transcript directly.
WINDOW_TOKENS = {"opus": 1_000_000, "sonnet": 1_000_000, "haiku": 200_000}

# Absolute floor: never fire below this many context tokens regardless of the
# per-family pct threshold. Guards against restart-loops on freshly respawned
# sessions whose baseline context (system prompt + handoff card + tool defs)
# already exceeds a pathologically-low pct threshold.
MIN_FIRE_TOKENS = 100_000

CONFIG_PATH = os.path.expanduser("~/.claude/cost-opt.json")
FIRED_DIR = os.path.expanduser("~/.cache/cost-auto-handoff/fired")
AM_CTL_BIN = os.path.expanduser("~/.agent-meeting/bin/am-ctl")

FAMILIES = ("opus", "sonnet", "haiku")

def load_config():
    """Return (enabled, thresholds_pct) or (False, {}) on any error."""
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
        ah = data.get("auto_handoff") or {}
        enabled = ah.get("enabled") is True
        thresholds = ah.get("thresholds_pct") or {}
        if not isinstance(thresholds, dict):
            return False, {}
        return enabled, thresholds
    except Exception:
        return False, {}


def last_assistant_usage(transcript_path):
    """
    Claude Code path: scan transcript JSONL in reverse for the last assistant
    message that has a usage block.
    Returns (model_id, context_tokens) or (None, None).

    context_tokens = input_tokens + cache_creation_input_tokens
                   + cache_read_input_tokens
    """
    try:
        with open(transcript_path, "r") as f:
            lines = f.readlines()
        for raw in reversed(lines):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if obj.get("type") != "assistant":
                continue
            msg = obj.get("message", {})
            usage = msg.get("usage")
            if not usage:
                continue
            model = msg.get("model", "")
            tokens = (
                usage.get("input_tokens", 0)
                + usage.get("cache_creation_input_tokens", 0)
                + usage.get("cache_read_input_tokens", 0)
            )
            return model, tokens
    except Exception:
        pass
    return None, None


def last_codex_token_count(transcript_path):
    """
    Codex path: scan transcript JSONL in reverse for the latest token_count
    event (type=="event_msg", payload.type=="token_count").
    Returns (input_tokens, model_context_window) or (None, None).

    Uses total_token_usage.input_tokens (cumulative across the whole session,
    not last_token_usage which is per-turn only).
    """
    try:
        with open(transcript_path, "r") as f:
            lines = f.readlines()
        for raw in reversed(lines):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if obj.get("type") != "event_msg":
                continue
            payload = obj.get("payload", {})
            if payload.get("type") != "token_count":
                continue
            info = payload.get("info", {})
            total = info.get("total_token_usage", {})
            input_tokens = total.get("input_tokens")
            window = info.get("model_context_window")
            if input_tokens is not None and window:
                return input_tokens, window
    except Exception:
        pass
    return None, None


def model_family(model_id):
    """Map model id string to opus/sonnet/haiku, or None if unrecognised."""
    lower = (model_id or "").lower()
    for fam in FAMILIES:
        if fam in lower:
            return fam
    return None


def lifecycle_sessions():
    """Return the authenticated local am-ctld inventory."""
    try:
        result = subprocess.run(
            [AM_CTL_BIN, "status", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return []
        payload = json.loads(result.stdout)
        sessions = payload.get("sessions") or []
        return sessions if isinstance(sessions, list) else []
    except Exception:
        return []


def resolve_lifecycle_agent(cwd, platform, session_id=None):
    """Resolve one local lifecycle identity without guessing among matches."""
    matches = []
    normalized_cwd = os.path.realpath(cwd) if cwd else ""
    for session in lifecycle_sessions():
        session_cwd = session.get("cwd") or ""
        if normalized_cwd and os.path.realpath(session_cwd) != normalized_cwd:
            continue
        if session.get("platform") != platform:
            continue
        matches.append(session)
    if session_id:
        exact = [
            session
            for session in matches
            if session_id
            in {
                session.get("transcript_session_id"),
                session.get("thread_id"),
            }
        ]
        if len(exact) == 1:
            matches = exact
    if len(matches) != 1:
        if len(matches) > 1:
            print(
                "cost-auto-handoff: local lifecycle identity is ambiguous for "
                f"cwd={cwd}; skipping",
                file=sys.stderr,
            )
        return None
    name = matches[0].get("name")
    project = matches[0].get("project")
    if not name or not project:
        return None
    return str(name), str(project)


def dispatch_lifecycle(name, project):
    """Wait for idle, then checkpoint and restart through the local control plane."""
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        matches = [
            session
            for session in lifecycle_sessions()
            if session.get("name") == name and session.get("project") == project
        ]
        if (
            len(matches) == 1
            and matches[0].get("state") == "idle"
            and matches[0].get("confidence") == "high"
        ):
            break
        time.sleep(1)
    else:
        return False

    common = [
        AM_CTL_BIN,
        "agent",
        "--name",
        name,
        "--proj",
        project,
        "--cmd",
    ]
    handoff = subprocess.run(
        [*common, "handoff"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=240,
        check=False,
    )
    if handoff.returncode != 0:
        return False
    restarted = subprocess.run(
        [*common, "restart"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=90,
        check=False,
    )
    return restarted.returncode == 0


def spawn_lifecycle_dispatch(name, project):
    """Detach orchestration so a Stop/PostToolUse hook cannot deadlock its TUI."""
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(
            [
                sys.executable,
                os.path.abspath(__file__),
                "--dispatch",
                name,
                project,
            ],
            **kwargs,
        )
        return True
    except OSError:
        return False


def mark_fired(session_id):
    """Write per-session dedup flag. Silently skips if session_id is None."""
    if not session_id:
        return
    try:
        os.makedirs(FIRED_DIR, exist_ok=True)
        with open(os.path.join(FIRED_DIR, session_id), "w"):
            pass
    except Exception as e:
        print(f"cost-auto-handoff: failed to write fired flag: {e}", file=sys.stderr)


def already_fired(session_id):
    """Return True if this session has already triggered a handoff."""
    if not session_id:
        return False
    return os.path.exists(os.path.join(FIRED_DIR, session_id))


def main():
    if len(sys.argv) == 4 and sys.argv[1] == "--dispatch":
        raise SystemExit(0 if dispatch_lifecycle(sys.argv[2], sys.argv[3]) else 1)
    try:
        stdin_data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    enabled, thresholds = load_config()
    if not enabled:
        sys.exit(0)

    transcript_path = stdin_data.get("transcript_path", "")
    cwd = stdin_data.get("cwd", "")
    session_id = stdin_data.get("session_id")
    hook_event = stdin_data.get("hook_event_name", "")

    if already_fired(session_id):
        sys.exit(0)

    if hook_event == "PostToolUse":
        # Codex path: read token_count from transcript JSONL
        input_tokens, window = last_codex_token_count(transcript_path)
        if input_tokens is None or not window:
            sys.exit(0)

        # Codex doesn't give us a model family name in the token_count event.
        # Use the model field from the hook payload if present, else default to
        # the cheapest match (sonnet) so we don't fire too aggressively.
        model_id = stdin_data.get("model", "")
        family = model_family(model_id) or "sonnet"

        threshold_pct = thresholds.get(family)
        if threshold_pct is None:
            sys.exit(0)

        threshold_tokens = int(threshold_pct / 100 * window)
        effective_threshold = max(threshold_tokens, MIN_FIRE_TOKENS)

        if input_tokens <= effective_threshold:
            sys.exit(0)

        identity = resolve_lifecycle_agent(cwd, "codex", session_id)
        if identity and spawn_lifecycle_dispatch(*identity):
            mark_fired(session_id)

    else:
        # Claude Code path (hook_event_name == "Stop" or unset)
        model_id, context_tokens = last_assistant_usage(transcript_path)
        if model_id is None or context_tokens is None:
            sys.exit(0)

        family = model_family(model_id)
        if family is None:
            sys.exit(0)

        threshold_pct = thresholds.get(family)
        if threshold_pct is None:
            sys.exit(0)

        threshold_tokens = int(threshold_pct / 100 * WINDOW_TOKENS[family])
        effective_threshold = max(threshold_tokens, MIN_FIRE_TOKENS)

        if context_tokens <= effective_threshold:
            sys.exit(0)

        identity = resolve_lifecycle_agent(cwd, "claude", session_id)
        if identity and spawn_lifecycle_dispatch(*identity):
            mark_fired(session_id)


if __name__ == "__main__":
    main()
