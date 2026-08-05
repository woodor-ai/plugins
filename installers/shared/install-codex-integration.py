#!/usr/bin/env python3
"""Install agent-meeting Codex skills without a marketplace source."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SKILL_NAMES = ("imagent", "talkto")
OWNER_FILE = ".agent-meeting-owner.json"
PLUGIN_ID = "agent-meeting@woodor"


def _command_detail(result: subprocess.CompletedProcess) -> str:
    return (
        result.stderr
        or result.stdout
        or "no diagnostic output"
    ).strip()


def _plugin_version() -> str:
    manifest = REPOSITORY_ROOT / "agent-meeting/.codex-plugin/plugin.json"
    try:
        return str(json.loads(manifest.read_text(encoding="utf-8"))["version"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise RuntimeError(
            f"bundled Codex plugin manifest is invalid: {error}"
        ) from error


def _is_owned_skill(directory: Path) -> bool:
    marker = directory / OWNER_FILE
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return payload.get("product") == "agent-meeting"


def _install_skill(
    source: Path,
    destination: Path,
    version: str,
    *,
    bootstrap_script: Path | None = None,
) -> None:
    if destination.exists() and not _is_owned_skill(destination):
        raise RuntimeError(
            f"refusing to replace unowned Codex skill: {destination}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.agent-meeting.tmp.{os.getpid()}"
    )
    backup = destination.with_name(
        f".{destination.name}.agent-meeting.backup.{os.getpid()}"
    )
    shutil.rmtree(temporary, ignore_errors=True)
    shutil.rmtree(backup, ignore_errors=True)
    shutil.copytree(source, temporary)
    if bootstrap_script is not None:
        scripts = temporary / "scripts"
        scripts.mkdir()
        shutil.copy2(bootstrap_script, scripts / bootstrap_script.name)
    (temporary / OWNER_FILE).write_text(
        json.dumps(
            {
                "product": "agent-meeting",
                "schema_version": 1,
                "version": version,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    if destination.exists():
        os.replace(destination, backup)
    try:
        os.replace(temporary, destination)
    except Exception:
        if backup.exists() and not destination.exists():
            os.replace(backup, destination)
        raise
    shutil.rmtree(backup, ignore_errors=True)


def _legacy_marketplace_source(codex_home: Path) -> str | None:
    config = codex_home / "config.toml"
    try:
        payload = tomllib.loads(config.read_text(encoding="utf-8"))
        marketplace = payload["marketplaces"]["woodor"]
    except (OSError, tomllib.TOMLDecodeError, KeyError, TypeError):
        return None
    if marketplace.get("source_type") != "local":
        return None
    source = marketplace.get("source")
    return str(source) if source else None


def _is_disposable_installer_source(source: str) -> bool:
    normalized = source.replace("\\", "/").lower().rstrip("/")
    return (
        "/agent-meeting-install-" in normalized
        or normalized.endswith("/.agent-meeting/updates/plugins")
    )


def _remove_legacy_plugin(codex: str) -> None:
    removed = subprocess.run(
        [codex, "plugin", "remove", PLUGIN_ID],
        capture_output=True,
        text=True,
    )
    if removed.returncode == 0:
        print("Removed legacy Codex marketplace plugin registration.")
        return
    detail = _command_detail(removed)
    normalized = detail.lower()
    if (
        "not installed" in normalized
        or "not configured" in normalized
        or "not found" in normalized
    ):
        return
    raise RuntimeError(
        "could not remove the legacy Codex marketplace plugin "
        f"(exit {removed.returncode}): {detail}"
    )


def _remove_disposable_marketplace(codex: str, codex_home: Path) -> None:
    source = _legacy_marketplace_source(codex_home)
    if source is None or not _is_disposable_installer_source(source):
        return
    removed = subprocess.run(
        [codex, "plugin", "marketplace", "remove", "woodor"],
        capture_output=True,
        text=True,
    )
    if removed.returncode != 0:
        raise RuntimeError(
            "could not remove the obsolete disposable Codex marketplace "
            f"(exit {removed.returncode}): {_command_detail(removed)}"
        )
    print(f"Removed obsolete disposable Codex marketplace source: {source}")


def install(codex_home: Path) -> None:
    version = _plugin_version()
    source_skills = REPOSITORY_ROOT / "agent-meeting" / "skills"
    bootstrap_script = (
        REPOSITORY_ROOT / "agent-meeting/scripts/bootstrap_runtime.py"
    )
    destination_skills = codex_home / "skills"
    for skill_name in SKILL_NAMES:
        _install_skill(
            source_skills / skill_name,
            destination_skills / skill_name,
            version,
            bootstrap_script=(
                bootstrap_script if skill_name == "imagent" else None
            ),
        )

    codex = os.environ.get("CODEX_BIN") or shutil.which("codex")
    if codex:
        _remove_legacy_plugin(codex)
        _remove_disposable_marketplace(codex, codex_home)

    print(
        f"Installed agent-meeting Codex skills {version} at {destination_skills}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")),
    )
    args = parser.parse_args(argv)
    try:
        install(args.codex_home.resolve())
    except (OSError, RuntimeError) as error:
        print(f"ERROR: Codex integration installation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
