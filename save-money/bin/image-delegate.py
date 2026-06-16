#!/usr/bin/env python3
"""
cost-image-delegate PreToolUse hook

Intercepts the main agent's Read calls on image files and denies them,
prompting it to delegate to an explore subagent instead. Subagent reads
are always allowed (identified by the presence of agent_id in stdin),
preventing a deadlock where explore can't read images either.

Protocol: see tools/cost-image-delegate/README.md
"""

import json
import os
import sys

CONFIG_PATH = os.path.expanduser("~/.claude/cost-opt.json")

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}

# Path prefixes exempt from the guard: the main agent may read images here
# directly even when the guard is on. AMBridge writes its PWA render self-check
# screenshots under /tmp/amb-shot* and needs to read them itself (pixel-level
# verification an explore subagent can't do). Hardcoded here, NOT in cost-opt.json
# — amp overwrites that file and would clobber an allowlist living there.
ALLOWLIST_PREFIXES = ("/tmp/amb-shot",)

DENY_REASON = (
    "默认不在主上下文直接读图片（图会赖在主会话每轮复读、涨成本）。"
    "请改派一个 explore subagent 去看这张图、回文字结论——"
    "图只进它的临时上下文，用完即弃。"
    "若你确需亲眼看像素，把 ~/.claude/cost-opt.json 的 image_delegate.enabled 设为 false 再读。"
)


def load_enabled():
    """
    Returns True only when image_delegate.enabled is explicitly true.
    Missing config / bad JSON / missing key / unset → False (opt-in, default off).
    This guard is invasive — it blocks ALL main-agent image reads — so it stays
    off until explicitly enabled (e.g. via the PWA Save Money toggle), rather
    than intercepting the moment it's installed.
    """
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
        return (data.get("image_delegate") or {}).get("enabled") is True
    except Exception:
        return False


def is_image_path(path):
    _, ext = os.path.splitext(path)
    return ext.lower() in IMAGE_EXTENSIONS


def main():
    try:
        stdin_data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    try:
        # Only act on Read tool calls.
        if stdin_data.get("tool_name") != "Read":
            sys.exit(0)

        # Subagent reads are always allowed — this is the deadlock guard.
        if "agent_id" in stdin_data:
            sys.exit(0)

        file_path = stdin_data.get("tool_input", {}).get("file_path", "")
        if not is_image_path(file_path):
            sys.exit(0)

        # Allowlisted paths bypass the guard (e.g. AMBridge self-check screenshots).
        if any(file_path.startswith(prefix) for prefix in ALLOWLIST_PREFIXES):
            sys.exit(0)

        # At this point: main agent + Read + image file.
        if not load_enabled():
            sys.exit(0)

        output = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": DENY_REASON,
            }
        }
        print(json.dumps(output))

    except Exception:
        sys.exit(0)


if __name__ == "__main__":
    main()
