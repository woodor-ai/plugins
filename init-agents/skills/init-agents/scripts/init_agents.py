#!/usr/bin/env python3
"""Install init-agents templates into a project without silent overwrites."""

from __future__ import annotations

import argparse
import difflib
import os
import subprocess
import sys
import tempfile
from pathlib import Path


PROFILE_NAMES = ("explore", "rd", "planner")
HOST_LAYOUT = {
    "codex": (Path(".codex") / "agents", ".toml"),
    "claude": (Path(".claude") / "agents", ".md"),
}
PROJECT_MARKERS = (
    ".git",
    "AGENTS.md",
    "CLAUDE.md",
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "go.mod",
    "Gemfile",
)


def _git_root(cwd: Path) -> Path | None:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


def _looks_like_project(path: Path) -> bool:
    return any((path / marker).exists() for marker in PROJECT_MARKERS)


def resolve_project_root(
    requested: str | None, allow_unrecognized: bool, cwd: Path | None = None
) -> Path:
    current = (cwd or Path.cwd()).resolve()
    root = Path(requested).expanduser().resolve() if requested else _git_root(current)
    root = root or current

    if not root.is_dir():
        raise ValueError(f"project root is not a directory: {root}")
    if not allow_unrecognized and not _looks_like_project(root):
        raise ValueError(
            f"{root} does not look like a project root; "
            "confirm the target and pass --allow-unrecognized-root"
        )
    return root


def asset_root() -> Path:
    return Path(__file__).resolve().parents[1] / "assets"


def planned_files(host: str, project_root: Path) -> list[tuple[Path, Path]]:
    target_dir, suffix = HOST_LAYOUT[host]
    return [
        (
            asset_root() / host / f"{profile}{suffix}",
            project_root / target_dir / f"{profile}{suffix}",
        )
        for profile in PROFILE_NAMES
    ]


def classify(source: Path, target: Path) -> str:
    if not target.exists():
        return "missing"
    if not target.is_file():
        return "different"
    return "identical" if source.read_bytes() == target.read_bytes() else "different"


def unified_diff(source: Path, target: Path) -> str:
    old = target.read_text(encoding="utf-8").splitlines(keepends=True)
    new = source.read_text(encoding="utf-8").splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            old,
            new,
            fromfile=str(target),
            tofile=f"{target} (init-agents template)",
        )
    )


def atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    content = source.read_bytes()
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def ensure_target_is_inside_project(target: Path, root: Path) -> None:
    resolved_root = root.resolve()
    resolved_target = target.resolve(strict=False)
    if not resolved_target.is_relative_to(resolved_root):
        raise ValueError(
            f"refusing to write through a path outside the project: {target}"
        )


def inspect(host: str, root: Path) -> list[tuple[Path, Path, str]]:
    results = []
    for source, target in planned_files(host, root):
        if not source.is_file():
            raise ValueError(f"template is missing: {source}")
        results.append((source, target, classify(source, target)))
    return results


def run_check(host: str, root: Path) -> int:
    for source, target, status in inspect(host, root):
        print(f"{status:9} {target.relative_to(root)}")
        if status == "different" and target.is_file():
            print(unified_diff(source, target), end="")
    return 0


def run_apply(host: str, root: Path, conflict: str) -> int:
    results = inspect(host, root)
    for _source, target, status in results:
        ensure_target_is_inside_project(target, root)
        if status == "different" and target.exists() and not target.is_file():
            raise ValueError(f"profile target is not a regular file: {target}")

    conflicts = [
        target for _source, target, status in results if status == "different"
    ]
    if conflicts and conflict == "error":
        for target in conflicts:
            print(f"conflict  {target.relative_to(root)}", file=sys.stderr)
        print(
            "No files changed. Run --mode check, then choose "
            "--conflict skip or --conflict overwrite.",
            file=sys.stderr,
        )
        return 2

    for source, target, status in results:
        relative = target.relative_to(root)
        if status == "identical":
            print(f"unchanged   {relative}")
        elif status == "different" and conflict == "skip":
            print(f"skipped     {relative}")
        else:
            atomic_copy(source, target)
            action = "created" if status == "missing" else "overwritten"
            print(f"{action:11} {relative}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install project-local explore, rd, and planner profiles."
    )
    parser.add_argument("--host", required=True, choices=sorted(HOST_LAYOUT))
    parser.add_argument("--mode", required=True, choices=("check", "apply"))
    parser.add_argument("--root", help="Project root; defaults to the Git top level.")
    parser.add_argument(
        "--conflict",
        choices=("error", "skip", "overwrite"),
        default="error",
        help="How apply handles existing files that differ (default: error).",
    )
    parser.add_argument(
        "--allow-unrecognized-root",
        action="store_true",
        help="Allow a confirmed non-Git directory without a known project marker.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = resolve_project_root(args.root, args.allow_unrecognized_root)
        if args.mode == "check":
            return run_check(args.host, root)
        return run_apply(args.host, root, args.conflict)
    except (OSError, ValueError) as error:
        print(f"init-agents: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
