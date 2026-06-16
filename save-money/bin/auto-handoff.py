#!/usr/bin/env python3
"""
cost-auto-handoff Stop hook

Reads ~/.claude/cost-opt.json, checks whether the current session's context
token usage exceeds the configured threshold, and if so drops a trigger file
at ~/.ambridge/handoff-triggers/<agent>.json for AMBridge (amp) to pick up.

Protocol: see tools/cost-auto-handoff/README.md
"""

import json
import os
import socket
import subprocess
import sys
import time

# Effective context window per family. Reflects the deployment: opus/sonnet run
# with the 1M-context beta; haiku runs the 200k standard window. The window is
# NOT detectable from transcript or Stop-hook stdin (verified: no context_window
# field exists), so we assert it by family. Erring large is the SAFE direction:
# if a session is not actually on 1M it auto-compacts near 200k and this
# (too-high) threshold simply never fires — no false restart. The reverse
# (assuming 200k on a real 1M session) would restart at ~120k, far too early.
# Edit this map if the deployment changes (e.g. haiku gains a 1M window).
WINDOW_TOKENS = {"opus": 1_000_000, "sonnet": 1_000_000, "haiku": 200_000}

# Absolute floor: never fire below this many context tokens, regardless of the
# per-family pct threshold. Guards against a restart-loop where a freshly
# respawned session's baseline context (system prompt + handoff card + tool defs
# + CLAUDE.md) already exceeds an aggressively-low pct threshold, re-triggering
# immediately on its first Stop. Any real session baseline is far below 100k;
# production thresholds (600k/160k) are far above it, so this only bites when a
# pct is set pathologically low.
MIN_FIRE_TOKENS = 100_000

CONFIG_PATH = os.path.expanduser("~/.claude/cost-opt.json")
TRIGGERS_DIR = os.path.expanduser("~/.ambridge/handoff-triggers")
FIRED_DIR = os.path.expanduser("~/.cache/cost-auto-handoff/fired")
MEETING_BIN = os.path.expanduser("~/.agent-meeting/bin/meeting")

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
    Scan transcript JSONL in reverse for the last assistant message that has
    a usage block. Returns (model_id, context_tokens) or (None, None).

    context_tokens = input_tokens + cache_creation_input_tokens
                   + cache_read_input_tokens
    (matches the three-part formula in the protocol spec)
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

    # Dedup: same session_id should only fire once per session.
    session_id = stdin_data.get("session_id")
    if session_id:
        fired_flag = os.path.join(FIRED_DIR, session_id)
        if os.path.exists(fired_flag):
            sys.exit(0)

    agent = resolve_agent_name(cwd)
    if agent is None:
        sys.exit(0)

    write_trigger(agent, context_tokens, effective_threshold)

    # Mark this session as fired so subsequent Stop hooks are no-ops.
    if session_id:
        try:
            os.makedirs(FIRED_DIR, exist_ok=True)
            with open(os.path.join(FIRED_DIR, session_id), "w"):
                pass
        except Exception as e:
            print(f"cost-auto-handoff: failed to write fired flag: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
