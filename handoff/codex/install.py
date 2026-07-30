#!/usr/bin/env python3
"""
handoff codex install — unified entry point for the interactive installer.

Wraps install-codex-hook.py so the handoff plugin satisfies the codex/install.py
convention required by install-codex.py.

When called from the installed copy (via install-codex.py), __file__ points to the
installed directory, so install-codex-hook.py is imported from the same installed
copy and PICKUP_SCRIPT resolves to the install dir's .codex-aware wrapper — not
the plugins-src original.
"""
import argparse
import importlib.util
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _remove_retired_codex_instruction() -> None:
    """Remove the retired autonomous handoff block from global instructions."""
    bootstrap = HERE.parent / "bin" / "handoff-bootstrap.py"
    spec = importlib.util.spec_from_file_location(f"handoff_bootstrap_{id(bootstrap)}", bootstrap)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    mod.remove_managed_block(codex_home / "AGENTS.md")


def _load_hook_installer():
    """Import install-codex-hook.py from the same directory as this script."""
    script = HERE / "install-codex-hook.py"
    spec = importlib.util.spec_from_file_location(
        f"handoff_hook_{id(script)}", script
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_install(ctx: dict) -> None:
    """Install the handoff SessionStart hook into ~/.codex/config.toml."""
    mod = _load_hook_installer()
    mod.install(None)
    _remove_retired_codex_instruction()


def main():
    ap = argparse.ArgumentParser(description="Install handoff codex hook")
    ap.add_argument("--project-path", help="Add project path trust entry")
    ap.add_argument("--uninstall", action="store_true", help="Remove the hook")
    args = ap.parse_args()

    mod = _load_hook_installer()
    if args.uninstall:
        mod.uninstall()
    else:
        project_path = args.project_path
        if project_path:
            project_path = str(Path(project_path).resolve())
        mod.install(project_path)
        _remove_retired_codex_instruction()


if __name__ == "__main__":
    main()
