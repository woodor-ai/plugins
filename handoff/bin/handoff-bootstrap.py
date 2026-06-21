#!/usr/bin/env python3
"""
handoff plugin: SessionStart bootstrap — idempotent injection of auto-handoff
strategy into global agent docs.

Runs once per cold startup (startup matcher only). Writes a version-stamped
block into ~/.claude/CLAUDE.md and, if ~/.codex/ exists, into
~/.codex/AGENTS.md and installs the Codex hook.

Does NOT emit any additionalContext (stdout JSON) — pure side-effects only.
"""

import glob
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Version — bump ONLY when the injected text changes, not on every plugin bump
# ---------------------------------------------------------------------------
INJECT_VERSION = 1

# ---------------------------------------------------------------------------
# Text for ~/.claude/CLAUDE.md  (summary + pointer; no personal names)
# ---------------------------------------------------------------------------
INJECT_TEXT_CLAUDE = """\
## Auto-handoff trigger strategy

When ALL of the following conditions are met simultaneously, the main agent
MUST call /handoff immediately — no confirmation needed (handoff is reversible):

1. **Clear task boundary**: just completed a git commit with no queued follow-up
   steps, or a PR is merged/pushed awaiting review, or a large subagent result
   has been reviewed, or the user said "done / wrap up / that's all for now".
2. **No in-flight work**: no subagent running, no unanswered user question, no
   unresolved error.
3. **Cooldown**: ≥ 30 minutes since the last handoff fired in this session.
4. **Session has accumulated**: ≥ 20 conversation turns OR ≥ 1 hour elapsed.

Do NOT auto-fire when the user's last message says "continue / next step / keep
going", when a task-list item is in_progress, when there are unresolved errors,
or when the user has said "no handoff today" in this session.

After firing: write the card, tell the user in one line that handoff is done
and the session can be closed, then stop taking new tasks.

Complete rules and card format: see the handoff plugin's SKILL.md
(skills/handoff/SKILL.md inside the plugin directory), section
"自动 handoff 触发策略".
"""

# ---------------------------------------------------------------------------
# Text for ~/.codex/AGENTS.md  (self-contained; Codex cannot read SKILL.md)
# ---------------------------------------------------------------------------
INJECT_TEXT_CODEX = """\
## Auto-handoff trigger strategy

When ALL of the following conditions are met simultaneously, the main agent
MUST call /handoff immediately — no confirmation needed (handoff is reversible):

1. **Clear task boundary**: just completed a git commit with no queued follow-up
   steps, or a PR is merged/pushed awaiting review, or a large subagent result
   has been reviewed, or the user said "done / wrap up / that's all for now".
2. **No in-flight work**: no subagent running, no unanswered user question, no
   unresolved error.
3. **Cooldown**: ≥ 30 minutes since the last handoff fired in this session.
4. **Session has accumulated**: ≥ 20 conversation turns OR ≥ 1 hour elapsed.

Do NOT auto-fire when the user's last message says "continue / next step / keep
going", when a task-list item is in_progress, when there are unresolved errors,
or when the user has said "no handoff today" in this session.

### Handoff card format

Write to `<cwd>/.claude/handoff-pending.md` (use the actual shell cwd, not the
git root). Hard limit: ≤ 50 lines. Exactly 3 sections:

```
# Handoff <YYYY-MM-DD HH:MM>

## 1. 当前阶段 in-flight
<1–3 items that are unfinished; use pointers like "see commit <hash>" or
"see <doc> §X.Y" instead of copy-pasting content>

## 2. Pending 用户决定
<Specific N-of-M choices, ≤ 2 lines each. Write "无" if none — never leave blank>

## 3. 新会话接手第一步
<Actionable: exact command / which section to read / which subagent to dispatch.
Never write "check git log yourself".>
```

After firing: write the card, tell the user in one line that handoff is done,
then stop taking new tasks for this session.
"""

# ---------------------------------------------------------------------------
# Block markers
# ---------------------------------------------------------------------------
_TAG = "woodor-handoff"
_BEGIN = f"<!-- BEGIN {_TAG} v{{version}} -->"
_END = f"<!-- END {_TAG} v{{version}} -->"
_PATTERN = re.compile(
    r"<!-- BEGIN " + re.escape(_TAG) + r" v(\d+) -->.*?<!-- END " + re.escape(_TAG) + r" v\1 -->",
    re.DOTALL,
)


def _make_block(text: str, version: int) -> str:
    return f"{_BEGIN.format(version=version)}\n{text}\n{_END.format(version=version)}"


def upsert(doc_path: Path, inject_text: str, version: int) -> None:
    """Idempotently inject a versioned block into doc_path.

    - Same version already present → no-op (file untouched, no backup).
    - Older version present → replace block (backup first, keep only 1 backup).
    - No block present → append (backup first, keep only 1 backup).
    """
    if not doc_path.parent.exists():
        return  # target directory absent — skip silently

    existing = doc_path.read_text(encoding="utf-8") if doc_path.exists() else ""

    match = _PATTERN.search(existing)
    if match:
        found_version = int(match.group(1))
        if found_version == version:
            return  # already up-to-date, no-op

        # Older version — replace
        _backup(doc_path)
        new_content = existing[: match.start()] + _make_block(inject_text, version) + existing[match.end() :]
        doc_path.write_text(new_content, encoding="utf-8")
    else:
        # No block — append
        _backup(doc_path)
        separator = "\n" if existing and not existing.endswith("\n") else ""
        doc_path.write_text(existing + separator + "\n" + _make_block(inject_text, version) + "\n", encoding="utf-8")


def _backup(doc_path: Path) -> None:
    """Keep exactly one backup: delete all old .handoff-bak* then write a fresh one."""
    for old in glob.glob(str(doc_path) + ".handoff-bak*"):
        try:
            os.remove(old)
        except OSError:
            pass
    if doc_path.exists():
        shutil.copy2(str(doc_path), str(doc_path) + ".handoff-bak")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    home = Path.home()

    # 1. Inject into ~/.claude/CLAUDE.md
    claude_md = home / ".claude" / "CLAUDE.md"
    upsert(claude_md, INJECT_TEXT_CLAUDE, INJECT_VERSION)

    # 2. Codex — skip entirely if ~/.codex/ does not exist
    codex_home = Path(os.environ.get("CODEX_HOME", str(home / ".codex")))
    if codex_home.exists():
        # 2a. Install Codex hook
        install_script = Path(__file__).resolve().parent.parent / "codex" / "install-codex-hook.py"
        if install_script.exists():
            subprocess.run(
                [sys.executable, str(install_script)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        # 2b. Inject into ~/.codex/AGENTS.md
        agents_md = codex_home / "AGENTS.md"
        upsert(agents_md, INJECT_TEXT_CODEX, INJECT_VERSION)

    sys.exit(0)


if __name__ == "__main__":
    main()
