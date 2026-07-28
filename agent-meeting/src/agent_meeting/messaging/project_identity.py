"""Stable project identities and their explicit per-repository declarations."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import urllib.parse


def _project_root(cwd: str) -> str:
    """Return the main repository root, converging linked git worktrees."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            common_dir = result.stdout.strip()
            if common_dir:
                return os.path.dirname(os.path.normpath(common_dir))
    except Exception:
        pass
    return os.path.normpath(cwd)


def _cache_dir(meeting_home: str) -> str:
    return os.path.join(meeting_home, "projcache")


def proj_cache_path(root: str, *, meeting_home: str) -> str:
    key = hashlib.sha1(os.path.normpath(root).encode("utf-8", "replace")).hexdigest()[:16]
    return os.path.join(_cache_dir(meeting_home), key)


def proj_cache_get(root: str, *, meeting_home: str):
    try:
        with open(proj_cache_path(root, meeting_home=meeting_home)) as cache_file:
            value = cache_file.readline().strip()
            return value or None
    except Exception:
        return None


def proj_cache_set(root: str, project: str, *, meeting_home: str) -> None:
    try:
        path = proj_cache_path(root, meeting_home=meeting_home)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as cache_file:
            cache_file.write(f"{project}\n{root}\n")
    except Exception:
        pass


def proj_cache_entries(*, meeting_home: str) -> list[dict]:
    cache_dir = _cache_dir(meeting_home)
    try:
        keys = os.listdir(cache_dir)
    except FileNotFoundError:
        keys = []

    entries = []
    for key in keys:
        try:
            with open(os.path.join(cache_dir, key)) as cache_file:
                lines = cache_file.read().splitlines()
        except Exception:
            continue
        project = lines[0].strip() if lines else ""
        root = lines[1].strip() if len(lines) > 1 else None
        entries.append({"key": key, "proj": project, "root": root or None})

    entries.sort(key=lambda entry: (
        entry["root"] is None,
        entry["root"] or "",
        entry["key"],
    ))
    return entries


def proj_cache_clear(root: str, *, meeting_home: str) -> bool:
    try:
        os.remove(proj_cache_path(root, meeting_home=meeting_home))
        return True
    except FileNotFoundError:
        return False


def proj_cache_clear_all(*, meeting_home: str) -> int:
    cache_dir = _cache_dir(meeting_home)
    try:
        keys = os.listdir(cache_dir)
    except FileNotFoundError:
        return 0

    removed = 0
    for key in keys:
        try:
            os.remove(os.path.join(cache_dir, key))
            removed += 1
        except Exception:
            pass
    return removed


def validate_project(project: str) -> str:
    stripped = project.strip()
    if not stripped:
        raise ValueError("--proj must not be empty")
    if any(char.isspace() or ord(char) < 0x20 for char in stripped):
        raise ValueError(
            f"--proj {project!r} must not contain whitespace or control characters"
        )
    return stripped


def resolve_authoritative_project(
    cwd: str,
    explicit_project: str | None,
    *,
    meeting_home: str,
):
    root = _project_root(cwd)
    if explicit_project is not None:
        proj_cache_set(root, explicit_project, meeting_home=meeting_home)
        return explicit_project
    return proj_cache_get(root, meeting_home=meeting_home)


def derive_project(cwd: str, *, meeting_home: str) -> str:
    root = _project_root(cwd)
    cached = proj_cache_get(root, meeting_home=meeting_home)
    if cached:
        return cached

    if sys.platform.startswith("win"):
        name = root
    else:
        home = os.path.expanduser("~")
        if root == home:
            name = "~"
        elif root.startswith(home + os.sep):
            name = "~" + root[len(home):]
        else:
            name = root
    return "_" if name == "*" else name


def monitor_pidfile_stem(name: str, project: str) -> str:
    token = "global" if project == "*" else urllib.parse.quote(project, safe="")
    return f"{name}@{token}"
