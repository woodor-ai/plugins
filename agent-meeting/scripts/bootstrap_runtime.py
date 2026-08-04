#!/usr/bin/env python3
"""Install the agent-meeting host runtime from a matching public release."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable


REPOSITORY_ARCHIVE = (
    "https://codeload.github.com/woodor-ai/plugins/zip/refs/{ref}"
)
TARGETS = ("claude-code", "codex", "all")


def plugin_root(script_path: Path) -> Path | None:
    candidate = script_path.resolve().parents[1]
    if (candidate / ".codex-plugin" / "plugin.json").is_file():
        return candidate
    if (candidate / ".claude-plugin" / "plugin.json").is_file():
        return candidate
    return None


def plugin_version(root: Path | None) -> str | None:
    if root is None:
        return None
    for manifest in (
        root / ".codex-plugin" / "plugin.json",
        root / ".claude-plugin" / "plugin.json",
    ):
        try:
            value = json.loads(manifest.read_text(encoding="utf-8"))["version"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        version = str(value).split("+", 1)[0]
        if version:
            return version
    return None


def release_archive_url(root: Path | None) -> str:
    version = plugin_version(root)
    reference = f"tags/v{version}" if version else "heads/main"
    return REPOSITORY_ARCHIVE.format(ref=reference)


def extracted_source_root(directory: Path) -> Path:
    candidates = [
        path.parents[1]
        for path in directory.glob("*/installers/install.py")
        if path.is_file()
    ]
    if len(candidates) != 1:
        raise RuntimeError("downloaded archive does not contain one plugins checkout")
    return candidates[0]


def install_runtime(
    *,
    target: str,
    root: Path | None,
    archive_url: str | None = None,
    opener: Callable[..., object] = urllib.request.urlopen,
    run: Callable[..., object] = subprocess.run,
) -> None:
    url = archive_url or release_archive_url(root)
    with tempfile.TemporaryDirectory(prefix="agent-meeting-bootstrap-") as temp:
        temp_dir = Path(temp)
        archive = temp_dir / "plugins.zip"
        print(f"Downloading agent-meeting runtime from {url}", flush=True)
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "agent-meeting-bootstrap"},
        )
        with opener(request, timeout=120) as response:
            with archive.open("wb") as output:
                shutil.copyfileobj(response, output)
        with zipfile.ZipFile(archive) as package:
            package.extractall(temp_dir / "source")
        source_root = extracted_source_root(temp_dir / "source")
        run(
            [
                sys.executable,
                str(source_root / "installers" / "install.py"),
                "--target",
                target,
                "--source-root",
                str(source_root),
            ],
            check=True,
        )
    print("agent-meeting runtime installation complete", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=TARGETS, required=True)
    parser.add_argument("--archive-url", default=None)
    args = parser.parse_args(argv)
    try:
        install_runtime(
            target=args.target,
            root=plugin_root(Path(__file__)),
            archive_url=args.archive_url,
        )
    except Exception as error:
        print(
            f"ERROR: agent-meeting runtime installation failed: {error}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
