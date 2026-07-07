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


def main():
    ap = argparse.ArgumentParser(prog="install.py",
                                 description="codex-only agent-meeting install")
    ap.add_argument("--control-url", default="",
                    help="agent-meeting control base url, e.g. http://<mac-tailnet-ip>:8765")
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

    # 3. guidance
    launcher = HERE / "codex-meeting.py"
    control = args.control_url or "http://<your-mac-tailnet-ip>:8765"
    print("\n=== install complete ===")
    print(f"  runtime:       {meeting_home}")
    print(f"  meeting CLI:   {meeting_home / 'bin' / 'meeting'}")
    print(f"  codex config:  {codex_home / 'config.toml'}  (register hook installed)")
    print(f"  codex scripts: {HERE}  (run in place — do not move this clone)")
    print()
    print("Next — start a bridged live codex session (foreground = your live TUI):")
    print(f'  "{vpy}" "{launcher}" <name> --control-url {control}')
    if not args.control_url:
        print("  (replace <your-mac-tailnet-ip> with the host running agent-meeting-control)")


if __name__ == "__main__":
    main()
