#!/usr/bin/env python3
"""
agent-meeting codex install — unified entry point for the interactive installer
(install-codex.py) and direct standalone use.

Entry point for the installer: run_install(ctx)
Standalone use:                python install.py [--control-url URL]

What run_install does (in order):
  1. Run session-bootstrap (builds ~/.agent-meeting: venv + zeroconf + websockets +
     bin/ wrappers including mycodex).
  2. Discover LAN controls via `meeting controls --json`; prompt the user to confirm
     or enter the control URL.
  3. Write the control_url to launcher.json so bare `mycodex` needs no --control-url.
  4. Install the codex SessionStart register hook into ~/.codex/config.toml.
  5. Windows: force [windows] sandbox = "unelevated" in config.toml.
  6. Write the agent-meeting usage block into ~/.codex/AGENTS.md.
  7. Put ~/.agent-meeting/bin on the user PATH (Windows: idempotent winreg edit).

All paths are resolved from __file__ so when called from the installed copy every
hook, wrapper, and script path points to the install directory — not plugins-src.
Honors MEETING_HOME / CODEX_HOME for isolated testing.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent          # <install>/agent-meeting/codex/
PLUGIN_ROOT = HERE.parent                        # <install>/agent-meeting/
BOOTSTRAP = PLUGIN_ROOT / "bin" / "session-bootstrap.py"
HOOK_INSTALLER = HERE / "install-codex-hook.py"
IS_WINDOWS = sys.platform.startswith("win")


def _run(cmd, env, label):
    print(f"\n=== {label} ===", flush=True)
    print("+ " + " ".join(str(c) for c in cmd), flush=True)
    r = subprocess.run(cmd, env=env)
    if r.returncode != 0:
        sys.exit(f"install: {label} failed (rc={r.returncode})")


def _summarize_bootstrap_stdout(stdout: str) -> str:
    """session-bootstrap.py is also a SessionStart hook: its stdout contract is a
    single JSON line (hookSpecificOutput.additionalContext) meant for the agent
    runtime to consume, not a terminal. Pull a one-line human summary out of it
    for the installer; the JSON contract itself is untouched."""
    try:
        data = json.loads(stdout.strip().splitlines()[-1])
        ctx = data["hookSpecificOutput"]["additionalContext"]
        m = re.search(r"Machine: `([^`]+)` \(role: (\w+), os: (\w+)\)", ctx)
        if m:
            host, role, os_label = m.groups()
            return f"runtime ready (role={role}, machine={host}, os={os_label})"
    except Exception:
        pass
    return "runtime ready"


def _run_bootstrap(cmd, env, label):
    """Like _run, but session-bootstrap.py's stdout is the SessionStart hook JSON
    contract (not installer-facing output) — capture and summarize it instead of
    letting it leak to the terminal. stderr and the exit code pass through
    untouched: failures must stay visible."""
    print(f"\n=== {label} ===", flush=True)
    r = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, text=True)
    if r.returncode != 0:
        sys.exit(f"install: {label} failed (rc={r.returncode})")
    print(f"  {_summarize_bootstrap_stdout(r.stdout)}")


def _venv_python(meeting_home: Path) -> Path:
    if IS_WINDOWS:
        return meeting_home / "venv" / "Scripts" / "python.exe"
    return meeting_home / "venv" / "bin" / "python"


_AGENTS_BEGIN = "<!-- agent-meeting:begin (auto-managed by agent-meeting/codex/install.py) -->"
_AGENTS_END = "<!-- agent-meeting:end -->"


def _ensure_windows_sandbox(codex_home: Path):
    """Windows only: force [windows] sandbox = "unelevated" in config.toml. Idempotent."""
    if not IS_WINDOWS:
        return
    import re
    cfg = codex_home / "config.toml"
    text = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
    m = re.search(r'(?ms)^\[windows\][ \t]*\r?\n(.*?)(?=^\[|\Z)', text)
    if m and re.search(r'(?m)^[ \t]*sandbox[ \t]*=[ \t]*"unelevated"', m.group(1)):
        print('  [windows] sandbox already "unelevated" — no change')
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
    print('  set [windows] sandbox = "unelevated"')


def _ensure_agents_md(codex_home: Path, meeting_home: Path, control: str):
    """Append (or refresh) the agent-meeting usage block in ~/.codex/AGENTS.md. Idempotent."""
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
  & "{vpy}" "{say}" X '正文放在单引号里'
  ```
  Put the body in **single quotes** (PowerShell treats them literally — safe for
  Chinese prose and punctuation; a literal `'` inside must be doubled `''`). You do
  NOT need to know your own name or the control address — meeting-say fills them in.
- **Group message**: an incoming turn whose text begins with
  `[group=G peer=X msg_id=N]` means agent **X** sent to group **G**. Reply to the
  group (so all members see it) using the group name as the recipient:
  ```
  & "{vpy}" "{say}" G '正文'
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
- **Control instructions**: an incoming turn that starts with `[control:restart
  from peer=X]` or `[control:clear from peer=X]` is a structured orchestration
  command, not a regular chat message — only that exact bracketed prefix counts;
  the word "restart" or "clear" appearing anywhere else in a message body is just
  text and must NOT be treated as a command.
  - `[control:restart ...]` — write a handoff summarizing in-flight state, then
    stop accepting new tasks and wait for this session to end.
  - `[control:clear ...]` — abort whatever task is in flight, clear your working
    context, and report back that you have been cleared.
{_AGENTS_END}"""
    agents = codex_home / "AGENTS.md"
    existing = agents.read_text(encoding="utf-8") if agents.exists() else ""
    import re
    if _AGENTS_BEGIN in existing and _AGENTS_END in existing:
        # Replacement must be a callable, NOT the raw `block` string: block embeds
        # filesystem paths (Windows backslashes, e.g. C:\Users\admin\...) and re.sub
        # interprets a string replacement as a regex template, where `\U` etc. is an
        # invalid escape (re.error: bad escape \U). A callable replacement is used
        # verbatim, so this holds regardless of what block contains.
        new = re.sub(re.escape(_AGENTS_BEGIN) + r".*?" + re.escape(_AGENTS_END),
                     lambda _m: block, existing, flags=re.S)
        action = "refreshed"
    else:
        new = (existing.rstrip("\n") + "\n\n" if existing.strip() else "") + block + "\n"
        action = "appended"
    agents.parent.mkdir(parents=True, exist_ok=True)
    agents.write_text(new, encoding="utf-8")
    print(f"  {action} agent-meeting section in {agents}")


def _write_launcher_defaults(meeting_home: Path, control_url: str):
    """Persist control_url so bare `mycodex` needs no --control-url."""
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
    norm = os.path.normcase(entry.rstrip("\\/"))
    parts = [os.path.normcase(p.strip().rstrip("\\/")) for p in current_path.split(os.pathsep) if p.strip()]
    return norm not in parts


def _ensure_path_entry(bin_dir: Path):
    """Put ~/.agent-meeting/bin on the user PATH so `mycodex` is callable by name."""
    entry = str(bin_dir)
    if not IS_WINDOWS:
        print(f"  add to your shell PATH to call mycodex by name: {entry}")
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
        print(f"  added {entry} to user PATH — open a NEW terminal, then `mycodex <name>`")
        _broadcast_environment_change()
    except Exception as e:
        print(f"  (could not update user PATH automatically: {e}; add {entry} manually)")


def _broadcast_environment_change() -> None:
    """Tell already-running processes (Explorer, etc.) that HKCU\\Environment changed.

    winreg writes the registry directly and — unlike `setx` — does not broadcast
    WM_SETTINGCHANGE, so Explorer keeps handing out the stale PATH to any window
    opened via the Start menu / Win+R until this fires (or the user logs off).
    """
    try:
        import ctypes
        result = ctypes.c_size_t()
        ok = ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF,  # HWND_BROADCAST
            0x001A,  # WM_SETTINGCHANGE
            0,
            "Environment",
            0x0002,  # SMTO_ABORTIFHUNG
            5000,
            ctypes.byref(result),
        )
        if not ok:
            print("  (could not notify running windows of the PATH change; a reboot or new login will pick it up)")
        else:
            print("  windows were notified — a new window (any app) will already see the updated PATH")
    except Exception:
        print("  (could not notify running windows of the PATH change; open a NEW window, or reboot, to pick it up)")


def _parse_controls(json_str: str) -> str:
    """Parse `meeting controls --json` output. Returns best URL or ''.

    Prefers the entry with is_current=True; falls back to the first entry.
    """
    try:
        controls = json.loads(json_str)
        if not controls:
            return ""
        c = next((x for x in controls if x.get("is_current")), controls[0])
        ip, port = c.get("ip", ""), c.get("port", "")
        return f"http://{ip}:{port}" if ip and port else ""
    except Exception:
        return ""


def _discover_control(meeting_home: Path, vpy: Path) -> str:
    """Query the LAN for agent-meeting controls. Returns the best URL or ''."""
    cli = meeting_home / "bin" / "meeting"
    if not cli.exists():
        return ""
    kw = {"creationflags": 0x08000000} if IS_WINDOWS else {}
    try:
        r = subprocess.run(
            [str(vpy), str(cli), "controls", "--json"],
            capture_output=True, text=True, timeout=10, **kw,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return ""
        return _parse_controls(r.stdout)
    except Exception:
        return ""


def run_install(ctx: dict) -> None:
    """Unified install entry point called by install-codex.py (and usable standalone).

    ctx keys: install_dir, plugins_src_dir, prompt (callable), is_windows.
    Paths are derived from __file__ so they always point to the installed copy.
    """
    prompt = ctx.get("prompt") or (
        lambda msg, default="": (input(f"{msg} [{default}]: " if default else f"{msg}: ").strip() or default)
    )

    for p in (BOOTSTRAP, HOOK_INSTALLER):
        if not p.exists():
            sys.exit(f"install: required file missing: {p}")

    meeting_home = Path(os.environ.get("MEETING_HOME") or (Path.home() / ".agent-meeting"))
    codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))

    env = os.environ.copy()
    env["PLUGIN_ROOT"] = str(PLUGIN_ROOT)

    print(f"agent-meeting install")
    print(f"  plugin root : {PLUGIN_ROOT}")
    print(f"  runtime     : {meeting_home}")
    print(f"  codex config: {codex_home}")

    # 1. bootstrap runtime (venv + zeroconf + websockets + bin/ wrappers incl. mycodex)
    _run_bootstrap([sys.executable, str(BOOTSTRAP)], env, "bootstrap ~/.agent-meeting runtime")

    vpy = _venv_python(meeting_home)
    if not vpy.exists():
        sys.exit(f"install: venv python not found after bootstrap: {vpy}")

    # 2. discover control URL via LAN broadcast
    print("\n=== discover control ===")
    discovered = _discover_control(meeting_home, vpy)
    if discovered:
        print(f"  found: {discovered}")
    else:
        print("  no control found on LAN (zeroconf scan)")

    control_url = prompt("  control URL (http://x.x.x.x:8765)", discovered)
    if not control_url:
        print("  WARNING: no control URL set; re-run install or use --control-url with mycodex")

    # 3. write launcher defaults
    _write_launcher_defaults(meeting_home, control_url)

    # 4. codex SessionStart register hook
    _run([sys.executable, str(HOOK_INSTALLER)], env, "install codex SessionStart hook")

    # 5. Windows sandbox fix + AGENTS.md
    print("\n=== configure codex outbound ===")
    _ensure_windows_sandbox(codex_home)
    _ensure_agents_md(codex_home, meeting_home, control_url)

    # 6. PATH
    print("\n=== PATH ===")
    _ensure_path_entry(meeting_home / "bin")

    print("\n=== agent-meeting install complete ===")
    print(f"  runtime : {meeting_home}")
    print(f"  mycodex : {meeting_home / 'bin' / 'mycodex'}")
    print()
    print("Next: open a NEW terminal and run `mycodex` or `mycodex <name>`")
    if control_url:
        print("  (control URL is remembered — no flag needed)")
    else:
        print("  mycodex <name> --control-url http://<control-host>:8765")


def main():
    ap = argparse.ArgumentParser(prog="install.py",
                                 description="agent-meeting codex install (standalone)")
    ap.add_argument("--control-url", default="",
                    help="pre-fill the control URL prompt")
    args = ap.parse_args()

    prefill = args.control_url

    def _cli_prompt(msg, default=""):
        actual_default = prefill if ("control" in msg.lower() and prefill) else default
        try:
            val = input(f"{msg} [{actual_default}]: " if actual_default else f"{msg}: ").strip()
        except EOFError:
            return actual_default
        return val or actual_default

    ctx = {
        "install_dir": PLUGIN_ROOT,
        "plugins_src_dir": PLUGIN_ROOT.parent.parent,
        "prompt": _cli_prompt,
        "is_windows": IS_WINDOWS,
    }
    run_install(ctx)


if __name__ == "__main__":
    main()
