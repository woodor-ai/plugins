#!/usr/bin/env python3
"""Remove retired auto-handoff instructions and install the Codex pickup hook.

Older handoff releases injected a ``woodor-handoff`` managed block into the
user's global Claude and Codex instruction files. Autonomous session shutdown
is no longer part of the handoff contract, so startup now removes every
version of that block instead of replacing it.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path


_TAG = "woodor-handoff"
_PATTERN = re.compile(
    r"(?:\r?\n)?<!-- BEGIN "
    + re.escape(_TAG)
    + r" v(\d+) -->.*?<!-- END "
    + re.escape(_TAG)
    + r" v\1 -->(?:\r?\n)?",
    re.DOTALL,
)


def remove_managed_block(doc_path: Path) -> bool:
    """Remove all retired auto-handoff blocks from *doc_path*.

    Returns ``True`` when the file changed. User-authored content is preserved.
    """

    if not doc_path.exists():
        return False
    existing = doc_path.read_text(encoding="utf-8")
    updated = _PATTERN.sub("\n", existing).strip()
    if updated:
        updated += "\n"
    if updated == existing:
        return False
    doc_path.write_text(updated, encoding="utf-8")
    return True


def install_codex_pickup_hook(codex_home: Path) -> None:
    if not codex_home.exists():
        return
    install_script = (
        Path(__file__).resolve().parent.parent
        / "codex"
        / "install-codex-hook.py"
    )
    if install_script.exists():
        subprocess.run(
            [sys.executable, str(install_script)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def main() -> None:
    home = Path.home()
    remove_managed_block(home / ".claude" / "CLAUDE.md")

    codex_home = Path(os.environ.get("CODEX_HOME", str(home / ".codex")))
    remove_managed_block(codex_home / "AGENTS.md")
    install_codex_pickup_hook(codex_home)


if __name__ == "__main__":
    main()
