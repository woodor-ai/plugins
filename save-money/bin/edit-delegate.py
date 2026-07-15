#!/usr/bin/env python3
"""
cost-edit-delegate PreToolUse hook

Intercepts the main agent's Edit/Write calls and denies them, prompting it to
delegate to an rd (or explore) subagent instead — the main agent is the most
expensive model in the session; burning it on a one-line edit is double waste
(TDP §3.1). Subagent calls are always allowed (identified by the presence of
agent_id in stdin), preventing a deadlock where a dispatched subagent can't
edit files either.

Protocol: see tools/cost-edit-delegate/README.md
"""

import json
import os
import sys

CONFIG_PATH = os.path.expanduser("~/.claude/cost-opt.json")

DENY_REASON = "本任务需派 rd（或 explore）subagent 执行，主 agent 不直接改文件。"


def load_enabled():
    """
    Returns False only when edit_delegate.enabled is explicitly false.
    Missing config / bad JSON / missing key / unset → True (opt-out, default on).
    Unlike image_delegate, this guard defaults ON — TDP §3.1 delegation is a
    standing rule, not an opt-in experiment.
    """
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
        return (data.get("edit_delegate") or {}).get("enabled") is not False
    except Exception:
        return True


def main():
    try:
        stdin_data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    try:
        # Only act on Edit/Write tool calls.
        if stdin_data.get("tool_name") not in ("Edit", "Write"):
            sys.exit(0)

        # Subagent calls are always allowed — this is the deadlock guard.
        if "agent_id" in stdin_data:
            sys.exit(0)

        # Escape hatch for a deliberate main-agent edit.
        if os.environ.get("CLAUDE_ALLOW_MAIN_EDIT") == "1":
            sys.exit(0)

        # At this point: main agent + Edit/Write.
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
