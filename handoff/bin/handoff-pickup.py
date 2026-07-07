#!/usr/bin/env python3
"""
handoff plugin: SessionStart auto-pickup. Cross-platform (Windows / macOS / Linux).

If <project>/.claude/handoff-pending.md exists, emit its content as
additionalContext for the new session and atomically archive it under
<project>/docs/handoff/archive/handoff-<timestamp>.md.

Race-resistant: hooks.json registers 4 matchers (startup/resume/clear/compact).
Multiple may fire concurrently for the same SessionStart event. The atomic
rename is the claim — winner emits context, losers exit 0 silently.
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

# 多 agent 共用一个 git 仓时，CLAUDE_PROJECT_DIR 塌缩到 git 顶层；
# 用 stdin 的 cwd 字段（Claude Code 喂进来的真实工作目录）优先，各 agent 才能各读各的卡。
_stdin_cwd = None
if not sys.stdin.isatty():
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)
        candidate = payload.get("cwd")
        if isinstance(candidate, str) and candidate:
            _stdin_cwd = candidate
    except (ValueError, json.JSONDecodeError, AttributeError):
        pass

PROJECT_DIR = Path(_stdin_cwd or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
HANDOFF = PROJECT_DIR / ".claude" / "handoff-pending.md"

if not HANDOFF.is_file():
    sys.exit(0)

ARCHIVE_DIR = PROJECT_DIR / "docs" / "handoff" / "archive"
ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

ts = datetime.now().strftime("%Y-%m-%d-%H%M%S")
archive_path = ARCHIVE_DIR / f"handoff-{ts}.md"

try:
    content = HANDOFF.read_text(encoding="utf-8")
except OSError:
    sys.exit(0)

if not content.strip():
    sys.exit(0)

# Atomic claim via rename. If a parallel hook already moved the file, rename
# raises FileNotFoundError — exit 0 silently so we don't double-emit.
try:
    HANDOFF.rename(archive_path)
except (FileNotFoundError, OSError):
    sys.exit(0)

todo_note = (
    "\n\n> **接手要求**：请阅读上方「第 5 段：本轮遗留 todo」，"
    "将其中各条纳入本 session 的待办（task list），不得只归档不处理。"
)
ctx = f"## 上 session 交接（auto-loaded，已归档 → {archive_path}）\n\n{content}{todo_note}"

print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": ctx,
    }
}))
