#!/usr/bin/env python3
"""
cost-auto-handoff hook — dual-host (Claude Code Stop / Codex PostToolUse)

Claude Code path (hook_event_name == "Stop"):
  Reads the session transcript JSONL for the last assistant usage block,
  derives context_tokens, compares against per-family thresholds from
  ~/.claude/cost-opt.json, and if exceeded writes a trigger file at
  ~/.ambridge/handoff-triggers/<agent>.json for AMBridge to pick up.

Codex path (hook_event_name == "PostToolUse"):
  Reads the session transcript JSONL for the latest token_count event,
  derives input_tokens and model_context_window directly from the JSONL
  (no static WINDOW_TOKENS lookup needed — Codex embeds the window size).
  If context_pct >= configured threshold, outputs {"additionalContext": "..."}
  to stdout to inject a handoff-now prompt into the running agent.

Dedup: ~/.cache/cost-auto-handoff/fired/<session_id> prevents repeat fires
within the same session on both hosts.
"""

import json
import os
import socket
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
TRIGGERS_DIR = os.path.expanduser("~/.ambridge/handoff-triggers")
FIRED_DIR = os.path.expanduser("~/.cache/cost-auto-handoff/fired")
MEETING_BIN = os.path.expanduser("~/.agent-meeting/bin/meeting")

FAMILIES = ("opus", "sonnet", "haiku")

CODEX_HANDOFF_PROMPT = (
    "⚠️ AUTO-HANDOFF: Your session context has exceeded the configured threshold. "
    "Before your next response, call the /handoff skill to write a handoff card, "
    "then tell the user to open a new session. Do this now."
)


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


def resolve_agent_name(cwd):
    """
    Query `meeting list` for a session that is online, matches this host and
    cwd. Returns the name string, or None if not found / ambiguous.
    """
    hostname = socket.gethostname()
    try:
        result = subprocess.run(
            [MEETING_BIN, "list"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        matches = []
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 6:
                continue
            status, name, _msgs, _role, sess_cwd, sess_host = (
                parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]
            )
            if (
                status == "online"
                and sess_cwd == cwd
                and sess_host == hostname
            ):
                matches.append(name)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            print(
                f"cost-auto-handoff: ambiguous meeting matches for cwd={cwd}: "
                f"{matches}; skipping trigger",
                file=sys.stderr,
            )
        return None
    except Exception:
        return None


def write_trigger(agent, context_tokens, threshold_tokens):
    """Atomically write trigger file to TRIGGERS_DIR."""
    os.makedirs(TRIGGERS_DIR, exist_ok=True)
    payload = json.dumps({
        "agent": agent,
        "reason": "auto-handoff",
        "context_tokens": context_tokens,
        "threshold_tokens": threshold_tokens,
        "ts": int(time.time()),
    })
    target = os.path.join(TRIGGERS_DIR, f"{agent}.json")
    tmp = target + ".tmp"
    with open(tmp, "w") as f:
        f.write(payload)
    os.rename(tmp, target)


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

        mark_fired(session_id)
        print(json.dumps({"additionalContext": CODEX_HANDOFF_PROMPT}))

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

        agent = resolve_agent_name(cwd)
        if agent is None:
            sys.exit(0)

        write_trigger(agent, context_tokens, effective_threshold)
        mark_fired(session_id)


if __name__ == "__main__":
    main()
