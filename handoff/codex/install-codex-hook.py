#!/usr/bin/env python3
"""
Install the handoff SessionStart hook into Codex.

Usage:
    python3 install-codex-hook.py [--project-path /absolute/path] [--uninstall]

What it does:
  1. Writes a [[hooks.SessionStart]] entry into ~/.codex/config.toml pointing
     to handoff-pickup.py (the same script that Claude Code uses).
  2. Computes the trusted_hash using codex's algorithm and writes
     [hooks.state."<key>"] to ~/.codex/config.toml so codex trusts the hook
     without asking for manual confirmation.
  3. If --project-path is given, also adds a [projects."<path>"] trust entry.
  4. Merge-writes: preserves all existing config entries.
  5. Idempotent: re-running updates the hash if the command changed.

Trusted-hash algorithm (from codex source codex-rs/hooks/src/engine/discovery.rs):
  identity = {
      "event_name": "session_start",
      "hooks": [{"async": False, "command": cmd, "timeout": 600, "type": "command"}],
      "matcher": <matcher>,
  }
  hash = "sha256:" + sha256(json.dumps(identity, sort_keys=True,
                                       separators=(',', ':'),
                                       ensure_ascii=False).encode()).hexdigest()
"""

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Locate handoff-pickup.py relative to this script
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
PICKUP_SCRIPT = (SCRIPT_DIR.parent / "bin" / "handoff-pickup.py").resolve()

if not PICKUP_SCRIPT.exists():
    sys.exit(f"ERROR: handoff-pickup.py not found at {PICKUP_SCRIPT}")

CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
CONFIG_PATH = CODEX_HOME / "config.toml"

# SessionStart matchers — match what Claude Code's hooks.json registers
MATCHERS = ["startup", "resume", "clear", "compact"]

# Codex runs the `command` string through a shell on each fire, so we use a
# `python3 || py -3 || python` fallback chain to find a working interpreter:
# Windows often has no `python3` (only `py`/`python`), mirroring the same
# fallback Claude Code's hooks.json already uses. Paths use forward slashes
# (Path.as_posix()) because backslashes are invalid escape sequences inside a
# TOML string, and Python accepts `/` paths on Windows. The script path is
# double-quoted to tolerate spaces.
# NOTE (Windows, pending fire-test): assumes Codex shell-executes this command
# (cmd.exe `||` / `py -3` both work). Validated on macOS only — the Windows
# Codex CLI was not yet installed on the test machine. See
# docs/codex-adaptation-investigation.md §7.
_PICKUP = PICKUP_SCRIPT.as_posix()
HOOK_COMMAND = f'python3 "{_PICKUP}" || py -3 "{_PICKUP}" || python "{_PICKUP}"'


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

    HOOK_COMMAND now contains literal double quotes (around the script path),
    which would otherwise terminate the TOML string. Escaping is a
    serialization concern only: Codex parses the value back to the unescaped
    HOOK_COMMAND, so the trusted_hash (computed over HOOK_COMMAND) still matches.
    """
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _section_header(key: str) -> str:
    # The key embeds CONFIG_PATH, which on Windows contains backslashes that are
    # invalid escapes inside a TOML quoted key — escape them so the file parses.
    # NOTE (Windows, pending fire-test): the escaped value round-trips to the
    # native path (e.g. C:\Users\...config.toml:session_start:0:0). Codex must
    # generate the SAME key internally to find this trusted_hash; whether
    # Windows Codex keys on backslash or forward-slash paths is UNVERIFIED (no
    # Codex CLI on the test machine). If the hook installs but never fires,
    # try the forward-slash form here. See docs §7.
    return f'[hooks.state."{_toml_escape(key)}"]'


def _project_header(path: str) -> str:
    # Same backslash-in-TOML-key hazard as _section_header for the project path.
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
    # Remove any stale handoff hook blocks first (idempotent)
    content = remove_handoff_hook_blocks(content)

    # Find insertion point: before first [hooks.state. section, or append
    insert_at = len(content)
    for m in re.finditer(r'^\[hooks\.state\.', content, re.MULTILINE):
        insert_at = m.start()
        break

    blocks = "\n".join(_hook_block(matcher) for matcher in MATCHERS) + "\n"
    content = content[:insert_at] + blocks + content[insert_at:]
    return content


def remove_handoff_hook_blocks(content: str) -> str:
    """Remove existing handoff [[hooks.SessionStart]] blocks."""
    # Match each [[hooks.SessionStart]] section up to next section header or EOF
    pattern = re.compile(
        r'\[\[hooks\.SessionStart\]\][^\[]*'
        r'(?:\[\[hooks\.SessionStart\.hooks\]\][^\[]*)*',
        re.DOTALL,
    )

    def is_handoff_block(match: re.Match) -> bool:
        return PICKUP_SCRIPT.name in match.group(0)

    result = pattern.sub(lambda m: "" if is_handoff_block(m) else m.group(0), content)
    return result


def ensure_state_entries(content: str) -> str:
    """Upsert [hooks.state."<key>"] entries with current hashes."""
    config_path_str = str(CONFIG_PATH)
    for i, matcher in enumerate(MATCHERS):
        key = f"{config_path_str}:session_start:{i}:0"
        trusted_hash = compute_trusted_hash("session_start", matcher, HOOK_COMMAND)
        content = upsert_state_entry(content, key, trusted_hash)
    return content


def upsert_state_entry(content: str, key: str, trusted_hash: str) -> str:
    """Insert or replace a [hooks.state."<key>"] block."""
    header = _section_header(key)
    escaped_header = re.escape(header)

    # Pattern: the section header + everything up to next section header or EOF
    pattern = re.compile(
        escaped_header + r'[^\[]*',
        re.DOTALL,
    )
    new_block = _state_block(key, trusted_hash) + "\n"

    if pattern.search(content):
        # Replace with a function, not a string: re.sub interprets backslash
        # escapes in a string replacement (\\ -> \, \1 -> group ref), which would
        # corrupt the escaped Windows paths in new_block on idempotent re-runs.
        content = pattern.sub(lambda _m: new_block, content)
    else:
        content = content.rstrip("\n") + "\n\n" + new_block
    return content


def ensure_project_trust(content: str, project_path: str) -> str:
    """Add [projects."<path>"] trust_level = "trusted" if not present."""
    header = _project_header(project_path)
    if header in content:
        return content

    # Insert before [hooks.state. sections
    insert_at = len(content)
    for m in re.finditer(r'^\[hooks\.state\.', content, re.MULTILINE):
        insert_at = m.start()
        break

    block = _project_block(project_path) + "\n"
    content = content[:insert_at] + block + content[insert_at:]
    return content


def remove_handoff_state_entries(content: str) -> str:
    """Remove all handoff-related [hooks.state.] entries."""
    config_path_str = str(CONFIG_PATH)
    for i, _ in enumerate(MATCHERS):
        key = f"{config_path_str}:session_start:{i}:0"
        header = _section_header(key)
        escaped = re.escape(header)
        pattern = re.compile(escaped + r'[^\[]*', re.DOTALL)
        content = pattern.sub("", content)
    return content


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def install(project_path: str | None) -> None:
    content = read_config()
    content = ensure_hook_blocks(content)
    content = ensure_state_entries(content)
    if project_path:
        content = ensure_project_trust(content, project_path)
    write_config(content)

    print(f"Installed handoff SessionStart hook → {PICKUP_SCRIPT}")
    print(f"Registered {len(MATCHERS)} matchers: {', '.join(MATCHERS)}")
    if project_path:
        print(f"Added project trust: {project_path}")
    print(f"Config updated: {CONFIG_PATH}")
    print()
    print("No action needed: codex will auto-trust the hook on next session start.")


def uninstall() -> None:
    content = read_config()
    content = remove_handoff_hook_blocks(content)
    content = remove_handoff_state_entries(content)
    write_config(content)
    print(f"Uninstalled handoff SessionStart hook from {CONFIG_PATH}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Install handoff SessionStart hook for Codex")
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
