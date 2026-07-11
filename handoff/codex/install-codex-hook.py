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

Trust-entry indexing — full rescan, not incremental bookkeeping:
  Codex keys hook trust by a [[hooks.SessionStart]] block's ORDINAL POSITION in
  config.toml (`session_start:<block-index>:0`), not by which plugin owns the
  block. Both this installer and agent-meeting's install-codex-hook.py maintain
  their blocks by deleting their own old blocks and re-appending fresh ones,
  which reorders every other plugin's blocks too. Any indexing scheme that
  only fixes up "our" entries goes stale the instant another plugin's
  installer runs afterward and reshuffles the file. The only robust fix: after
  writing our own blocks, rescan the ENTIRE file for all [[hooks.SessionStart]]
  blocks (ours and everyone else's) and rewrite ALL session_start trust
  entries from scratch, keyed by actual final position. See
  rewrite_session_start_state_entries().

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
import shutil
import subprocess
import sys
import tomllib
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

# The HOOK_COMMAND form differs by OS because codex executes hook commands very
# differently on Windows vs POSIX.
#
# POSIX: codex runs the command through a shell, so the `python3 || py -3 ||
# python` fallback chain works (finds a real interpreter when python3 is absent).
#
# Windows (verified on codex 0.140.0): codex's hook runner splits the command on
# whitespace, does NOT honor quotes, and does NOT go through a shell — so `||`
# never falls through, and a quoted path becomes a filename WITH literal quotes
# (not found → the hook exits 1 before the script runs). `python3` is also a
# Store-alias stub. So on Windows we emit a bare, unquoted, space-free two-token
# command: "<resolved-python-abspath> <pickup>". Forward slashes throughout
# (backslashes are invalid TOML escapes; Python accepts `/` on Windows).
_PICKUP = PICKUP_SCRIPT.as_posix()


def _resolve_windows_python() -> str:
    """Return an absolute python.exe for the unquoted Windows hook command.

    `py -3` is the reliable launcher on Windows; ask it for the real interpreter
    path. Fall back to the interpreter running this installer, then a PATH lookup.
    """
    try:
        r = subprocess.run(["py", "-3", "-c", "import sys;print(sys.executable)"],
                           capture_output=True, text=True, timeout=10)
        cand = r.stdout.strip()
        if r.returncode == 0 and cand and Path(cand).exists():
            return cand
    except Exception:
        pass
    if sys.executable and Path(sys.executable).exists():
        return sys.executable
    return shutil.which("python") or shutil.which("py") or "python"


if os.name == "nt":
    _WIN_PY = Path(_resolve_windows_python()).as_posix()
    HOOK_COMMAND = f"{_WIN_PY} {_PICKUP}"
    # The unquoted Windows form only works if BOTH tokens are space-free (codex
    # splits on whitespace). Warn loudly rather than emit a silently-broken hook.
    if " " in _WIN_PY or " " in _PICKUP:
        sys.stderr.write(
            "WARNING: a space in the python or pickup path breaks the unquoted "
            "Windows hook command (codex splits on whitespace, ignores quotes).\n"
            f"         python: {_WIN_PY}\n"
            f"         pickup: {_PICKUP}\n"
            "         Relocate the plugin / python to a space-free path.\n"
        )
else:
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
    if os.name == "nt":
        # Match Codex's own serialization of Windows project-trust keys: a TOML
        # *literal* string (single quotes — backslashes are not escapes there) with
        # the path lowercased, exactly as observed in real ~/.codex/config.toml
        # (e.g. [projects.'d:\aiagent\plugins']). Windows paths are case-insensitive
        # so lowercasing is safe and matches what Codex writes/compares.
        # NOTE: aligned to Codex's stored format, not yet fire-tested (project trust
        # was bypassed in the hook fire-tests). A mismatch only costs a one-time
        # "trust this folder" prompt — it never blocks the hook itself. See docs §7.
        return f"[projects.'{path.lower()}']"
    # POSIX (validated on macOS): case-sensitive paths, basic string, original case.
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
    # Rescan+rewrite AFTER removing our blocks so remaining plugins' entries
    # are recomputed at their (possibly shifted) new positions.
    content = rewrite_session_start_state_entries(content)
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
