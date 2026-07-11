#!/usr/bin/env python3
"""
agent-meeting: codex-only install entrypoint.

One command to make a fresh, codex-only machine (NO Claude Code) able to use
agent-meeting with codex. Assumes this plugin repo is already cloned locally.

    python install.py [--control-url http://<mac-tailnet-ip>:8765]

What it does:
  1. Runs session-bootstrap.py (with PLUGIN_ROOT pointed at this clone) to build
     the ~/.agent-meeting runtime: venv + zeroconf + websockets + bin/ (the
     `meeting` CLI and friends). On a machine without Claude Code the statusline
     registration self-skips and, because a fresh config is is_host=false, no
     daemon / Windows persistence is installed (the control stays on the host).
  2. Runs install-codex-hook.py to install the codex SessionStart register hook
     into ~/.codex/config.toml (unquoted venv-python command; trust entries placed
     after any pre-existing SessionStart hooks so nothing else is clobbered).
  3. Prints how to start a bridged live codex session.

The codex scripts (codex-bridge.py / codex-register.py / codex-meeting.py) run
in place from this clone — the hook + launcher reference each other by __file__,
and the hook command embeds this clone's codex-register.py absolute path — so
DO NOT move the clone after installing (re-run install.py if you do).

Honors MEETING_HOME / CODEX_HOME / CLAUDE_CONFIG_DIR for isolated testing.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent          # <clone>/agent-meeting/codex/
PLUGIN_ROOT = HERE.parent                        # <clone>/agent-meeting/
BOOTSTRAP = PLUGIN_ROOT / "bin" / "session-bootstrap.py"
HOOK_INSTALLER = HERE / "install-codex-hook.py"
IS_WINDOWS = sys.platform.startswith("win")


def _run(cmd, env, label):
    print(f"\n=== {label} ===", flush=True)
    print("+ " + " ".join(str(c) for c in cmd), flush=True)
    r = subprocess.run(cmd, env=env)
    if r.returncode != 0:
        sys.exit(f"install: {label} failed (rc={r.returncode})")


def _venv_python(meeting_home: Path) -> Path:
    if IS_WINDOWS:
        return meeting_home / "venv" / "Scripts" / "python.exe"
    return meeting_home / "venv" / "bin" / "python"


_AGENTS_BEGIN = "<!-- agent-meeting:begin (auto-managed by agent-meeting/codex/install.py) -->"
_AGENTS_END = "<!-- agent-meeting:end -->"


def _ensure_windows_sandbox(codex_home: Path):
    """Windows only: codex's 'elevated' sandbox needs a helper exe
    (codex-windows-sandbox-setup.exe) this install lacks, which makes EVERY shell
    command codex runs fail (orchestrator_helper_launch_failed). codex must be able
    to run shell commands to call the meeting CLI, so force `[windows] sandbox =
    "unelevated"`. Idempotent."""
    if not IS_WINDOWS:
        return
    import re
    cfg = codex_home / "config.toml"
    text = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
    m = re.search(r'(?ms)^\[windows\][ \t]*\r?\n(.*?)(?=^\[|\Z)', text)
    if m and re.search(r'(?m)^[ \t]*sandbox[ \t]*=[ \t]*"unelevated"', m.group(1)):
        print("  [windows] sandbox already \"unelevated\" — no change")
        return
    if m:
        body = m.group(1)
        if re.search(r'(?m)^[ \t]*sandbox[ \t]*=', body):
            new_body = re.sub(r'(?m)^[ \t]*sandbox[ \t]*=.*$', 'sandbox = "unelevated"', body, count=1)
        else:
            new_body = 'sandbox = "unelevated"\n' + body
        text = text[:m.start(1)] + new_body + text[m.end(1):]
    else:
        text = (text.rstrip("\n") + "\n\n" if text.strip() else "") + '[windows]\nsandbox = "unelevated"\n'
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(text, encoding="utf-8")
    print('  set [windows] sandbox = "unelevated" '
          '(codex shell needs this; "elevated" requires a helper exe not present here)')


def _ensure_agents_md(codex_home: Path, meeting_home: Path, control: str):
    """Append (or refresh) an agent-meeting usage section to ~/.codex/AGENTS.md so
    codex knows it is a peer and how to send. Idempotent — replaces only the block
    between our markers, never the user's own content."""
    # Always use the extensionless script form (plain Python file) with the venv
    # python explicitly — do NOT use the .cmd wrapper, which routes through cmd.exe
    # and mangles < / > in %* as redirection when the body contains those characters.
    say = meeting_home / "bin" / "meeting-say"
    vpy = _venv_python(meeting_home)
    cli = meeting_home / "bin" / "meeting"
    ctrl = control or "http://<your-mac-tailnet-ip>:8765"
    block = f"""{_AGENTS_BEGIN}
## agent-meeting (peer messaging)

You are a peer on **agent-meeting** — other agents can message you and you can
message them.

- **Direct message (1:1)**: an incoming turn whose text begins with
  `[peer=X msg_id=N]` means agent **X** is talking to you directly.
  Reply (or message anyone) with ONE command:
  ```
  & "{vpy}" "{say}" X '你的正文放在单引号里'
  ```
  Put the body in **single quotes** (PowerShell treats them literally — safe for
  Chinese prose and punctuation; a literal `'` inside must be doubled `''`). You do
  NOT need to know your own name or the control address — meeting-say fills them in.
- **Group message**: an incoming turn whose text begins with
  `[group=G peer=X msg_id=N]` means agent **X** sent to group **G**. Reply to the
  group (so all members see it) using the group name as the recipient:
  ```
  & "{vpy}" "{say}" G '你的正文'
  ```
  Use **G** (the group name), not **X** (the sender), as the recipient.
- **See who is online**:
  ```
  & "{vpy}" "{cli}" list --host {ctrl}
  ```
- **Etiquette**: reply only when you have something substantive (an answer, a
  question, a decision, a status change). Do NOT send bare acks ("收到 / ok / 好的")
  — a reply wakes the other agent's whole session, so silence is fine when you have
  nothing to add. You may also start a conversation or message a third agent on your
  own initiative.
{_AGENTS_END}"""
    agents = codex_home / "AGENTS.md"
    existing = agents.read_text(encoding="utf-8") if agents.exists() else ""
    import re
    if _AGENTS_BEGIN in existing and _AGENTS_END in existing:
        new = re.sub(re.escape(_AGENTS_BEGIN) + r".*?" + re.escape(_AGENTS_END),
                     block, existing, flags=re.S)
        action = "refreshed"
    else:
        new = (existing.rstrip("\n") + "\n\n" if existing.strip() else "") + block + "\n"
        action = "appended"
    agents.parent.mkdir(parents=True, exist_ok=True)
    agents.write_text(new, encoding="utf-8")
    print(f"  {action} agent-meeting section in {agents}")


def _write_launcher_defaults(meeting_home: Path, control_url: str):
    """Persist the control_url so `codex-meeting <name>` needs no --control-url."""
    if not control_url:
        return
    p = meeting_home / "codex" / "launcher.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        existing = {}
    existing["control_url"] = control_url
    p.write_text(json.dumps(existing, ensure_ascii=False), encoding="utf-8")
    print(f"  saved default control_url -> {p}")


def _path_needs_entry(current_path: str, entry: str) -> bool:
    """True if `entry` is not already a component of `current_path` (case/sep-insensitive)."""
    norm = os.path.normcase(entry.rstrip("\\/"))
    parts = [os.path.normcase(p.strip().rstrip("\\/")) for p in current_path.split(os.pathsep) if p.strip()]
    return norm not in parts


def _ensure_path_entry(bin_dir: Path):
    """Put ~/.agent-meeting/bin on the user's PATH so `codex-meeting` (and the
    other wrappers) are callable by name. Windows: idempotent user-PATH edit via
    the registry (no setx 1024-char truncation, REG_EXPAND_SZ preserved). POSIX:
    print a hint (shell-profile edits vary too much to do safely)."""
    entry = str(bin_dir)
    if not IS_WINDOWS:
        print(f"  add to your shell PATH to call codex-meeting by name: {entry}")
        return
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ) as k:
            try:
                cur, kind = winreg.QueryValueEx(k, "Path")
            except FileNotFoundError:
                cur, kind = "", winreg.REG_EXPAND_SZ
        if not _path_needs_entry(cur or "", entry):
            print(f"  {entry} already on user PATH")
            return
        new = ((cur.rstrip(os.pathsep) + os.pathsep) if cur else "") + entry
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "Path", 0, winreg.REG_EXPAND_SZ, new)
        print(f"  added {entry} to user PATH — open a NEW terminal, then `codex-meeting <name>`")
    except Exception as e:
        print(f"  (could not update user PATH automatically: {e}; add {entry} manually)")


def main():
    ap = argparse.ArgumentParser(prog="install.py",
                                 description="codex-only agent-meeting install")
    ap.add_argument("--control-url", default="",
                    help="agent-meeting control base url, e.g. http://<mac-tailnet-ip>:8765")
    ap.add_argument("--no-path", action="store_true",
                    help="do not add ~/.agent-meeting/bin to the user PATH")
    args = ap.parse_args()

    for p in (BOOTSTRAP, HOOK_INSTALLER):
        if not p.exists():
            sys.exit(f"install: required file missing: {p}")

    meeting_home = Path(os.environ.get("MEETING_HOME") or (Path.home() / ".agent-meeting"))
    codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))

    # Child processes inherit our env (incl. MEETING_HOME / CODEX_HOME /
    # CLAUDE_CONFIG_DIR) plus PLUGIN_ROOT so bootstrap wraps THIS clone's bin/.
    env = os.environ.copy()
    env["PLUGIN_ROOT"] = str(PLUGIN_ROOT)

    print(f"agent-meeting codex install")
    print(f"  clone (PLUGIN_ROOT): {PLUGIN_ROOT}")
    print(f"  runtime (MEETING_HOME): {meeting_home}")
    print(f"  codex config (CODEX_HOME): {codex_home}")

    # 1. runtime
    _run([sys.executable, str(BOOTSTRAP)], env, "bootstrap ~/.agent-meeting runtime")

    vpy = _venv_python(meeting_home)
    if not vpy.exists():
        sys.exit(f"install: venv python not found after bootstrap: {vpy}")

    # 2. codex SessionStart register hook
    _run([sys.executable, str(HOOK_INSTALLER)], env, "install codex SessionStart hook")

    # 3. codex shell fix (Windows) + meeting usage instructions for codex
    print("\n=== configure codex for outbound (shell + AGENTS.md) ===")
    _ensure_windows_sandbox(codex_home)
    _ensure_agents_md(codex_home, meeting_home, args.control_url)

    # 4. convenience: `codex-meeting <name>` callable by name, control_url remembered
    print("\n=== convenience launcher ===")
    _write_launcher_defaults(meeting_home, args.control_url)
    if args.no_path:
        print(f"  (--no-path) skipped PATH edit; add {meeting_home / 'bin'} manually to call by name")
    else:
        _ensure_path_entry(meeting_home / "bin")

    # 5. guidance
    launcher = HERE / "codex-meeting.py"
    control = args.control_url or "http://<your-mac-tailnet-ip>:8765"
    print("\n=== install complete ===")
    print(f"  runtime:       {meeting_home}")
    print(f"  meeting CLI:   {meeting_home / 'bin' / 'meeting'}")
    print(f"  codex config:  {codex_home / 'config.toml'}  (register hook installed)")
    print(f"  codex scripts: {HERE}  (run in place — do not move this clone)")
    print()
    print("Next — start a bridged live codex session (open a NEW terminal first so PATH refreshes):")
    if args.control_url:
        print("  codex-meeting <name>            # control-url is remembered; <name> optional too")
    else:
        print("  codex-meeting <name> --control-url http://<control-host>:8765")
    print("Full path form (works in this terminal, no new terminal needed):")
    print(f'  "{vpy}" "{launcher}" <name>' + (f" --control-url {control}" if not args.control_url else ""))


if __name__ == "__main__":
    main()
