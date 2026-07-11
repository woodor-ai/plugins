#!/usr/bin/env python3
"""
Install the agent-meeting SessionStart hook into Codex.

Usage:
    python3 install-codex-hook.py [--project-path /absolute/path] [--uninstall]

What it does:
  1. Writes 4 [[hooks.SessionStart]] entries (matchers startup/resume/clear/
     compact) into ~/.codex/config.toml pointing to codex-register.py — the
     hook that registers this codex session into agent-meeting and drops a
     mapping file for the bridge daemon.
  2. Computes the trusted_hash using codex's algorithm and writes
     [hooks.state."<key>"] so codex auto-trusts the hook (no manual confirm).
  3. If --project-path is given, also adds a [projects."<path>"] trust entry.
  4. Merge-writes: preserves all existing config entries. Idempotent.

Trust-entry indexing — full rescan, not incremental bookkeeping:
  Codex keys hook trust by a [[hooks.SessionStart]] block's ORDINAL POSITION in
  config.toml (`session_start:<block-index>:0`), not by which plugin owns the
  block. Both this installer and handoff's install-codex-hook.py maintain
  their blocks by deleting their own old blocks and re-appending fresh ones,
  which reorders every other plugin's blocks too. Any indexing scheme that
  only fixes up "our" entries (e.g. a position-aware base index computed at
  the moment we run) goes stale the instant another plugin's installer runs
  afterward and reshuffles the file. The only robust fix: after writing our
  own blocks, rescan the ENTIRE file for all [[hooks.SessionStart]] blocks (ours
  and everyone else's) and rewrite ALL session_start trust entries from
  scratch, keyed by actual final position. See rewrite_session_start_state_entries().

Trusted-hash algorithm (mirrors codex-rs/hooks/src/engine/discovery.rs):
  identity = {
      "event_name": "session_start",
      "hooks": [{"async": False, "command": cmd, "timeout": 600, "type": "command"}],
      "matcher": <matcher>,
  }
  hash = "sha256:" + sha256(json.dumps(identity, sort_keys=True,
                                       separators=(',', ':'),
                                       ensure_ascii=False).encode()).hexdigest()

CRITICAL (Windows) — the HOOK_COMMAND is UNQUOTED with space-free paths.
Codex's Windows hook runner splits the command string on whitespace and does
NOT honor quotes: a quoted exe path becomes a filename containing literal
quotes → "not found" → the hook exits 1 before the script runs (this is why
handoff's `python3 "..." || py -3 "..." || python "..."` form fails on Windows
app-server, verified 2026-07-07). We therefore emit a bare two-token command:
    <venv_python_abspath> <codex-register_abspath>
Both tokens are space-free on a normal install (~/.agent-meeting/venv and the
plugin cache path). If either path contains a space this form breaks — see the
space guard below.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import tomllib
from pathlib import Path

# ---------------------------------------------------------------------------
# Locate codex-register.py (same dir as this script) and the agent-meeting venv
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
REGISTER_SCRIPT = (SCRIPT_DIR / "codex-register.py").resolve()

if not REGISTER_SCRIPT.exists():
    sys.exit(f"ERROR: codex-register.py not found at {REGISTER_SCRIPT}")

HOME = Path.home()
# Honor MEETING_HOME so the HOOK_COMMAND points at the right venv during an
# isolated / relocated install (same env the CLI, monitor, bridge respect).
AM_HOME = Path(os.environ.get("MEETING_HOME") or (HOME / ".agent-meeting"))
if os.name == "nt":
    VENV_PYTHON = AM_HOME / "venv" / "Scripts" / "python.exe"
else:
    VENV_PYTHON = AM_HOME / "venv" / "bin" / "python"

CODEX_HOME = Path(os.environ.get("CODEX_HOME", HOME / ".codex"))
CONFIG_PATH = CODEX_HOME / "config.toml"

# SessionStart matchers — match what Claude Code's hooks.json registers
MATCHERS = ["startup", "resume", "clear", "compact"]

# UNQUOTED, forward-slash, space-free two-token command (see module docstring).
_VENV = VENV_PYTHON.as_posix()
_REG = REGISTER_SCRIPT.as_posix()
HOOK_COMMAND = f"{_VENV} {_REG}"

# Space guard: codex splits on whitespace, so a space in either path is fatal.
if " " in _VENV or " " in _REG:
    sys.stderr.write(
        "WARNING: a space was found in the venv-python or register-script path.\n"
        "         Codex's Windows hook runner splits the command on whitespace and\n"
        "         does NOT honor quotes, so this hook will fail to launch.\n"
        f"         venv:     {_VENV}\n"
        f"         register: {_REG}\n"
        "         Relocate agent-meeting/the plugin to a space-free path.\n"
    )


# ---------------------------------------------------------------------------
# Hash computation (mirrors codex-rs/hooks/src/engine/discovery.rs)
# ---------------------------------------------------------------------------
def compute_trusted_hash(event_name: str, matcher: str, command: str) -> str:
    identity = {
        "event_name": event_name,
        "hooks": [{"async": False, "command": command, "timeout": 600, "type": "command"}],
        "matcher": matcher,
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


# ---------------------------------------------------------------------------
# Minimal TOML read/write helpers — avoids a toml dependency
# ---------------------------------------------------------------------------
def read_config() -> str:
    if CONFIG_PATH.exists():
        return CONFIG_PATH.read_text(encoding="utf-8")
    return ""


def write_config(content: str) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(content, encoding="utf-8")


def _toml_escape(s: str) -> str:
    """Escape a string for a TOML basic (double-quoted) value.

    HOOK_COMMAND uses forward slashes and no quotes, so in practice this is a
    no-op for the command; it still matters for the [hooks.state] key, whose
    value embeds CONFIG_PATH (backslashes on Windows). Codex parses the value
    back to the unescaped string, so the trusted_hash still matches.
    """
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _section_header(key: str) -> str:
    return f'[hooks.state."{_toml_escape(key)}"]'


def _project_header(path: str) -> str:
    if os.name == "nt":
        # Match codex's own serialization of Windows project-trust keys: a TOML
        # *literal* string (single quotes) with the path lowercased, e.g.
        # [projects.'d:\aiagent\plugins']. Case-insensitive paths → lowercasing
        # is safe and matches what codex writes/compares.
        return f"[projects.'{path.lower()}']"
    return f'[projects."{_toml_escape(path)}"]'


def _hook_block(matcher: str) -> str:
    return (
        f'[[hooks.SessionStart]]\n'
        f'matcher = "{matcher}"\n'
        f'\n'
        f'[[hooks.SessionStart.hooks]]\n'
        f'type = "command"\n'
        f'command = "{_toml_escape(HOOK_COMMAND)}"\n'
    )


def _state_block(key: str, trusted_hash: str) -> str:
    return (
        f'{_section_header(key)}\n'
        f'enabled = true\n'
        f'trusted_hash = "{trusted_hash}"\n'
    )


def _project_block(path: str) -> str:
    return (
        f'{_project_header(path)}\n'
        f'trust_level = "trusted"\n'
    )


# ---------------------------------------------------------------------------
# Config manipulation
# ---------------------------------------------------------------------------
def ensure_hook_blocks(content: str) -> str:
    """Insert [[hooks.SessionStart]] blocks if not already present."""
    content = remove_agent_meeting_hook_blocks(content)

    insert_at = len(content)
    for m in re.finditer(r'^\[hooks\.state\.', content, re.MULTILINE):
        insert_at = m.start()
        break

    blocks = "\n".join(_hook_block(matcher) for matcher in MATCHERS) + "\n"
    prefix = content[:insert_at]
    if prefix and not prefix.endswith("\n\n"):
        # keep a blank line between the preceding section and our first block
        blocks = "\n" + blocks
    content = prefix + blocks + content[insert_at:]
    return content


def remove_agent_meeting_hook_blocks(content: str) -> str:
    """Remove existing agent-meeting [[hooks.SessionStart]] blocks."""
    pattern = re.compile(
        r'\[\[hooks\.SessionStart\]\][^\[]*'
        r'(?:\[\[hooks\.SessionStart\.hooks\]\][^\[]*)*',
        re.DOTALL,
    )

    def is_ours(match: re.Match) -> bool:
        return REGISTER_SCRIPT.name in match.group(0)

    return pattern.sub(lambda m: "" if is_ours(m) else m.group(0), content)


_SESSION_START_STATE_RE = re.compile(
    r'\[hooks\.state\."[^\n]*?:session_start:\d+:0"\][^\[]*', re.DOTALL
)


def rewrite_session_start_state_entries(content: str) -> str:
    """Recompute trust entries for EVERY [[hooks.SessionStart]] block in the
    final config, keyed by actual file position — supersedes any per-plugin
    incremental index bookkeeping (see module docstring). Strips all existing
    session_start state entries (ours and other plugins') and rebuilds them
    from the blocks actually present in `content`, so this is correct no
    matter which installer ran last or in what order."""
    content = _SESSION_START_STATE_RE.sub("", content)

    parsed = tomllib.loads(content)
    blocks = parsed.get("hooks", {}).get("SessionStart", [])
    config_path_str = str(CONFIG_PATH)
    entries = []
    for i, block in enumerate(blocks):
        matcher = block.get("matcher", "")
        command = block["hooks"][0]["command"]
        trusted_hash = compute_trusted_hash("session_start", matcher, command)
        key = f"{config_path_str}:session_start:{i}:0"
        entries.append(_state_block(key, trusted_hash))
    if not entries:
        return content

    insert_at = len(content)
    for m in re.finditer(r'^\[hooks\.state\.', content, re.MULTILINE):
        insert_at = m.start()
        break
    block_text = "\n".join(entries) + "\n"
    prefix = content[:insert_at]
    if prefix and not prefix.endswith("\n\n"):
        block_text = "\n" + block_text
    return prefix + block_text + content[insert_at:]


def ensure_project_trust(content: str, project_path: str) -> str:
    header = _project_header(project_path)
    if header in content:
        return content
    insert_at = len(content)
    for m in re.finditer(r'^\[hooks\.state\.', content, re.MULTILINE):
        insert_at = m.start()
        break
    block = _project_block(project_path) + "\n"
    return content[:insert_at] + block + content[insert_at:]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def install(project_path: str | None) -> None:
    content = read_config()
    content = ensure_hook_blocks(content)
    content = rewrite_session_start_state_entries(content)
    if project_path:
        content = ensure_project_trust(content, project_path)
    write_config(content)

    print(f"Installed agent-meeting SessionStart hook → {REGISTER_SCRIPT}")
    print(f"HOOK_COMMAND (unquoted): {HOOK_COMMAND}")
    print(f"Registered {len(MATCHERS)} matchers: {', '.join(MATCHERS)}")
    if project_path:
        print(f"Added project trust: {project_path}")
    print(f"Config updated: {CONFIG_PATH}")
    print()
    print("No action needed: codex will auto-trust the hook on next session start.")


def uninstall() -> None:
    content = read_config()
    content = remove_agent_meeting_hook_blocks(content)
    # Rescan+rewrite AFTER removing our blocks so remaining plugins' entries
    # are recomputed at their (possibly shifted) new positions.
    content = rewrite_session_start_state_entries(content)
    write_config(content)
    print(f"Uninstalled agent-meeting SessionStart hook from {CONFIG_PATH}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Install agent-meeting SessionStart hook for Codex")
    parser.add_argument("--project-path", help="Add trust for this absolute project path")
    parser.add_argument("--uninstall", action="store_true", help="Remove the hook")
    args = parser.parse_args()

    if args.uninstall:
        uninstall()
    else:
        project_path = args.project_path
        if project_path:
            project_path = str(Path(project_path).resolve())
        install(project_path)


if __name__ == "__main__":
    main()
