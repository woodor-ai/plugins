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
    """Copy plugin src to dest, clearing dest first."""
    if dest.exists():
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
        install_str = pf(f"  Install directory", default_dest)
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


if __name__ == "__main__":
    main()
