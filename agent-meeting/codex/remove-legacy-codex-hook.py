#!/usr/bin/env python3
"""Remove the obsolete per-session agent-meeting SessionStart hook."""

import hashlib
import json
import os
import re
import tomllib
from pathlib import Path


HOME = Path.home()
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or (HOME / ".codex"))
CONFIG_PATH = CODEX_HOME / "config.toml"
LEGACY_SCRIPT_NAME = "codex-register.py"


def toml_escape(value):
    return value.replace("\\", "\\\\").replace('"', '\\"')


def trusted_hash(matcher, hooks):
    identity = {
        "event_name": "session_start",
        "hooks": [
            {
                "async": bool(hook.get("async", False)),
                "command": hook["command"],
                "timeout": int(hook.get("timeout", 600)),
                "type": hook.get("type", "command"),
            }
            for hook in hooks
        ],
        "matcher": matcher,
    }
    canonical = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def remove_legacy_blocks(content):
    pattern = re.compile(
        r"\[\[hooks\.SessionStart\]\][^\[]*"
        r"(?:\[\[hooks\.SessionStart\.hooks\]\][^\[]*)*",
        re.DOTALL,
    )
    return pattern.sub(
        lambda match: "" if LEGACY_SCRIPT_NAME in match.group(0) else match.group(0),
        content,
    )


def rewrite_session_start_state(content):
    config_key = toml_escape(str(CONFIG_PATH))
    state_pattern = re.compile(
        r'\[hooks\.state\."'
        + re.escape(config_key)
        + r':session_start:\d+:\d+"\][^\[]*',
        re.DOTALL,
    )
    content = state_pattern.sub("", content)
    parsed = tomllib.loads(content)
    blocks = parsed.get("hooks", {}).get("SessionStart", [])
    if not blocks:
        return content
    entries = []
    for index, block in enumerate(blocks):
        matcher = block.get("matcher", "")
        hooks = block.get("hooks", [])
        block_hash = trusted_hash(matcher, hooks)
        for hook_index, _hook in enumerate(hooks):
            key = f"{CONFIG_PATH}:session_start:{index}:{hook_index}"
            entries.append(
                f'[hooks.state."{toml_escape(key)}"]\n'
                "enabled = true\n"
                f'trusted_hash = "{block_hash}"\n'
            )
    insert_at = len(content)
    match = re.search(r"^\[hooks\.state\.", content, re.MULTILINE)
    if match:
        insert_at = match.start()
    block_text = "\n".join(entries) + "\n"
    if content[:insert_at] and not content[:insert_at].endswith("\n\n"):
        block_text = "\n" + block_text
    return content[:insert_at] + block_text + content[insert_at:]


def main():
    if not CONFIG_PATH.exists():
        print(f"No Codex config found at {CONFIG_PATH}; no legacy hook to remove")
        return
    content = CONFIG_PATH.read_text(encoding="utf-8")
    without_legacy = remove_legacy_blocks(content)
    if without_legacy == content:
        print(f"No legacy agent-meeting SessionStart hook found in {CONFIG_PATH}")
        return
    updated = rewrite_session_start_state(without_legacy)
    CONFIG_PATH.write_text(updated, encoding="utf-8")
    print(f"Removed legacy agent-meeting SessionStart hook from {CONFIG_PATH}")


if __name__ == "__main__":
    main()
