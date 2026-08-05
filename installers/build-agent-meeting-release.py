#!/usr/bin/env python3
"""Build the minimal versioned agent-meeting R2 release bundle."""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import zipfile
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_PATHS = (
    "LICENSE",
    "agent-meeting/.claude-plugin/plugin.json",
    "agent-meeting/.codex-plugin/plugin.json",
    "agent-meeting/README.md",
    "agent-meeting/pyproject.toml",
    "agent-meeting/scripts/bootstrap_runtime.py",
    "agent-meeting/skills",
    "agent-meeting/src",
    "mycodex/pyproject.toml",
    "mycodex/src",
    "installers/install.py",
    "installers/shared",
)
ALLOWED_TOP_LEVEL = {"LICENSE", "agent-meeting", "mycodex", "installers"}
REQUIRED_BUNDLE_FILES = (
    "agent-meeting/.claude-plugin/plugin.json",
    "agent-meeting/.codex-plugin/plugin.json",
    "agent-meeting/README.md",
    "agent-meeting/pyproject.toml",
    "agent-meeting/scripts/bootstrap_runtime.py",
    "agent-meeting/skills/imagent/SKILL.md",
    "agent-meeting/src/agent_meeting/__init__.py",
    "mycodex/pyproject.toml",
    "mycodex/src/mycodex/__init__.py",
    "installers/install.py",
    "installers/shared/install-agent-meeting-package.py",
    "installers/shared/install-claude-integration.py",
    "installers/shared/install-codex-integration.py",
)


def plugin_version(repository_root: Path) -> str:
    manifest = repository_root / "agent-meeting/.codex-plugin/plugin.json"
    return str(json.loads(manifest.read_text(encoding="utf-8"))["version"])


def bundle_prefix(version: str) -> str:
    return f"agent-meeting-v{version}/"


def verify_bundle(bundle: Path, version: str) -> None:
    prefix = bundle_prefix(version)
    with zipfile.ZipFile(bundle) as archive:
        names = set(archive.namelist())
        bad_prefixes = sorted(name for name in names if not name.startswith(prefix))
        if bad_prefixes:
            raise RuntimeError("release bundle has entries outside its version root")
        relative_names = {
            name[len(prefix) :]
            for name in names
            if name != prefix and name[len(prefix) :]
        }
        top_level = {name.split("/", 1)[0] for name in relative_names}
        if top_level != ALLOWED_TOP_LEVEL:
            raise RuntimeError(
                "release bundle top-level entries do not match the agent-meeting set"
            )
        missing = [
            path for path in REQUIRED_BUNDLE_FILES if path not in relative_names
        ]
        if missing:
            raise RuntimeError(
                "release bundle is missing required files: " + ", ".join(missing)
            )
        manifest_path = prefix + "agent-meeting/.codex-plugin/plugin.json"
        manifest_version = str(
            json.loads(archive.read(manifest_path).decode("utf-8"))["version"]
        )
        if manifest_version != version:
            raise RuntimeError(
                f"release bundle version {manifest_version} does not match {version}"
            )
        bad_crc = archive.testzip()
        if bad_crc is not None:
            raise RuntimeError(f"release bundle has a corrupt entry: {bad_crc}")


def build_bundle(
    repository_root: Path,
    *,
    ref: str,
    version: str,
    output: Path,
) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "git",
            "archive",
            "--format=zip",
            f"--prefix={bundle_prefix(version)}",
            f"--output={output}",
            ref,
            "--",
            *BUNDLE_PATHS,
        ],
        cwd=repository_root,
        check=True,
    )
    verify_bundle(output, version)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the minimal agent-meeting R2 release bundle."
    )
    parser.add_argument("--ref")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    version = plugin_version(REPOSITORY_ROOT)
    ref = args.ref or f"v{version}"
    output = args.output or (
        Path(tempfile.gettempdir()) / f"agent-meeting-v{version}.zip"
    )
    built = build_bundle(
        REPOSITORY_ROOT,
        ref=ref,
        version=version,
        output=output.resolve(),
    )
    print(built)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
