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

DENY_REASON = (
    "默认不在主上下文直接读图片（图会赖在主会话每轮复读、涨成本）。"
    "请改派一个 explore subagent 去看这张图、回文字结论——"
    "图只进它的临时上下文，用完即弃。"
    "若你确需亲眼看像素，把 ~/.claude/cost-opt.json 的 image_delegate.enabled 设为 false 再读。"
)


def load_enabled():
    """
    Returns True if the hook should be active.
    Missing config / bad JSON / missing image_delegate key → True (default on).
    Only explicit enabled=false turns it off.
    """
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
        section = data.get("image_delegate", {})
        if section.get("enabled") is False:
            return False
        return True
    except Exception:
        return True


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
