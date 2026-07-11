#!/usr/bin/env python3
"""
Interactive installer for codex-compatible woodor-ai plugins.

Discovers plugins in this repo that expose codex/install.py with a run_install(ctx)
interface, copies each selected plugin to the install directory, then runs the
plugin's own install logic against the installed copy.

Plugin convention: plugin dir must contain codex/install.py with run_install(ctx).
ctx keys passed to each plugin:
    install_dir     : Path  — where this plugin was copied
    plugins_src_dir : Path  — root of this plugins repo
    prompt          : Callable[[str, str], str]  — prompt(msg, default) -> str
    is_windows      : bool
"""
import importlib.util
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
IS_WINDOWS = sys.platform.startswith("win")

_EXCL_DIRS = {"tests", "__pycache__", ".claude-plugin", ".claude", "worktrees"}
_EXCL_SUFFIXES = {".pyc"}


def _default_codex_home() -> Path:
    if IS_WINDOWS:
        base = os.environ.get("USERPROFILE") or str(Path.home())
        return Path(base) / ".codex"
    return Path.home() / ".codex"


def _default_install_dir(codex_home: Path, plugin_name: str) -> Path:
    return codex_home / "plugins" / plugin_name


def _default_meeting_home() -> Path:
    if IS_WINDOWS:
        base = os.environ.get("USERPROFILE") or str(Path.home())
        return Path(base) / ".agent-meeting"
    return Path.home() / ".agent-meeting"


def _prompt(msg: str, default: str = "") -> str:
    display = f"{msg} [{default}]: " if default else f"{msg}: "
    try:
        val = input(display).strip()
    except EOFError:
        return default
    return val if val else default


def _discover_plugins(src: Path) -> list:
    """Return sorted list of plugin Paths that expose codex/install.py."""
    found = []
    if not src.is_dir():
        return found
    for item in sorted(src.iterdir()):
        if not item.is_dir():
            continue
        if (item / "codex" / "install.py").exists():
            found.append(item)
    return found


def _copy_dir(src: Path, dest: Path) -> None:
    for item in src.iterdir():
        if item.name in _EXCL_DIRS:
            continue
        if item.suffix in _EXCL_SUFFIXES:
            continue
        d = dest / item.name
        if item.is_dir():
            d.mkdir(exist_ok=True)
            _copy_dir(item, d)
        else:
            shutil.copy2(str(item), str(d))


def _copy_plugin(src: Path, dest: Path) -> None:
    """Copy plugin src to dest, clearing dest first.

    Safety guard: an existing non-empty dest is only cleared when it is
    recognizable as a previous plugin install (contains the codex/install.py
    sentinel). Anything else — the current directory, a repo checkout, a
    random user directory — is refused with an error instead of being wiped.
    """
    dest = dest.resolve()
    if dest.exists():
        if any(dest.iterdir()) and not (dest / "codex" / "install.py").exists():
            sys.exit(
                f"install: refusing to clear {dest} — it is not empty and does not "
                f"look like a previous plugin install (no codex/install.py). "
                f"Choose a different install directory."
            )
        shutil.rmtree(str(dest))
    dest.mkdir(parents=True)
    _copy_dir(src, dest)


def _load_plugin_installer(install_dir: Path):
    """Import codex/install.py from the installed copy."""
    script = install_dir / "codex" / "install.py"
    spec = importlib.util.spec_from_file_location(
        f"codex_install_{install_dir.name}_{id(install_dir)}", script
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_interactive(plugins_src: Path, codex_home: Path, prompt_fn=None) -> dict:
    """Run the interactive install loop.

    Returns dict with keys 'installed' (list of (name, Path)) and 'skipped' (list of str).
    """
    pf = prompt_fn or _prompt
    plugins = _discover_plugins(plugins_src)
    installed = []
    skipped = []

    for plugin_dir in plugins:
        name = plugin_dir.name
        yn = pf(f"Install {name}?", "Y")
        if yn.lower() not in ("y", "yes", ""):
            skipped.append(name)
            continue

        default_dest = str(_default_install_dir(codex_home, name))
        # Empty answer always falls back to the default — enforced here, not in
        # the prompt function, so no prompt implementation can yield Path("")
        # (which is Path(".") and once caused the cwd to be wiped).
        install_str = (pf(f"  Install directory", default_dest) or "").strip() or default_dest
        install_dir = Path(install_str)

        print(f"  Copying {name} -> {install_dir} ...")
        _copy_plugin(plugin_dir, install_dir)

        print(f"  Running {name} installer ...")
        mod = _load_plugin_installer(install_dir)
        ctx = {
            "install_dir": install_dir,
            "plugins_src_dir": plugins_src,
            "prompt": pf,
            "is_windows": IS_WINDOWS,
        }
        mod.run_install(ctx)
        installed.append((name, install_dir))

    return {"installed": installed, "skipped": skipped}


def _generate_codex_plugins_command(plugins_src: Path, bin_dir: Path) -> None:
    """Install a local `codex-plugins` command into bin_dir (same dir + generation
    mechanism as `mycodex`): re-running it later pulls (or clones) the canonical
    ~/.codex/plugins-src checkout and reruns this installer, args passed through.

    Copies the existing bootstrap scripts (install-codex-plugins.sh/.ps1) verbatim
    instead of reimplementing clone/pull — those scripts already are "clone-or-pull
    + run the installer", so this stays a single source of truth.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    if IS_WINDOWS:
        dest_ps1 = bin_dir / "codex-plugins.ps1"
        shutil.copy2(str(plugins_src / "install-codex-plugins.ps1"), str(dest_ps1))
        dest_cmd = bin_dir / "codex-plugins.cmd"
        dest_cmd.write_text(
            f'@echo off\r\npowershell -NoProfile -ExecutionPolicy Bypass -File "{dest_ps1}" %*\r\n'
        )
    else:
        dest_sh = bin_dir / "codex-plugins"
        shutil.copy2(str(plugins_src / "install-codex-plugins.sh"), str(dest_sh))
        dest_sh.chmod(0o755)


def _ensure_bin_on_path(bin_dir: Path) -> None:
    """Put bin_dir on PATH so `codex-plugins` is callable by name, even when the
    agent-meeting plugin itself was not selected for install.

    Reuses agent-meeting's own winreg PATH helper (Windows: idempotent user-PATH
    edit; POSIX: prints a hint) by importing it straight from plugins_src — no
    second copy of that logic, and it only ever touches PATH, never agent-meeting's
    runtime.
    """
    am_install = HERE / "agent-meeting" / "codex" / "install.py"
    if not am_install.exists():
        return
    spec = importlib.util.spec_from_file_location("agent_meeting_codex_install_pathhelper", am_install)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._ensure_path_entry(bin_dir)


def main():
    codex_home = Path(os.environ.get("CODEX_HOME") or str(_default_codex_home()))

    print("=== codex plugin installer ===")
    print(f"plugins-src : {HERE}")
    print(f"codex home  : {codex_home}")
    print()

    result = run_interactive(HERE, codex_home)

    print()
    print("=== summary ===")
    for name, d in result["installed"]:
        print(f"  installed : {name} -> {d}")
    for name in result["skipped"]:
        print(f"  skipped   : {name}")
    if result["installed"]:
        print()
        print("Open a NEW terminal and run: mycodex")
        print("Or:                          mycodex <session-name>")

        bin_dir = Path(os.environ.get("MEETING_HOME") or str(_default_meeting_home())) / "bin"
        _generate_codex_plugins_command(HERE, bin_dir)
        _ensure_bin_on_path(bin_dir)
        print()
        print(f"codex-plugins command installed -> {bin_dir}")
        print("Next time you want to install/update plugins, just run: codex-plugins")


if __name__ == "__main__":
    main()
