"""Installation ownership manifest used by the destructive uninstall command."""

from __future__ import annotations

import json
import os
from pathlib import Path


SCHEMA_VERSION = 1
MANIFEST_NAME = "install-manifest.json"
VALID_TARGETS = frozenset({"claude-code", "codex"})


def manifest_path(meeting_home: Path) -> Path:
    return meeting_home / MANIFEST_NAME


def read_manifest(meeting_home: Path) -> dict:
    path = manifest_path(meeting_home)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"agent-meeting install manifest is missing or invalid: {path}"
        ) from exc
    expected_home = str(meeting_home.resolve())
    if (
        payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("meeting_home") != expected_home
    ):
        raise RuntimeError(
            f"agent-meeting install manifest does not own {expected_home}"
        )
    targets = payload.get("targets")
    if (
        not isinstance(targets, list)
        or not set(targets).issubset(VALID_TARGETS)
    ):
        raise RuntimeError("agent-meeting install manifest has invalid targets")
    return payload


def record_installation(
    meeting_home: Path,
    *,
    version: str,
    targets: set[str],
) -> dict:
    invalid = targets - VALID_TARGETS
    if invalid:
        raise ValueError(f"invalid installation target(s): {sorted(invalid)}")
    path = manifest_path(meeting_home)
    previous_targets: set[str] = set()
    if path.exists():
        try:
            previous_targets.update(read_manifest(meeting_home)["targets"])
        except RuntimeError:
            previous_targets.clear()
    payload = {
        "schema_version": SCHEMA_VERSION,
        "meeting_home": str(meeting_home.resolve()),
        "version": version,
        "targets": sorted(previous_targets | targets),
        "bin_directory": str((meeting_home / "bin").resolve()),
    }
    meeting_home.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return payload
