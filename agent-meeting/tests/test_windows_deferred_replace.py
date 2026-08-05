from __future__ import annotations

import json
from pathlib import Path


def test_apply_replacements_retries_locked_files(tmp_path):
    from agent_meeting.installation.windows_deferred_replace import (
        apply_replacements,
    )

    source = tmp_path / "new.exe"
    destination = tmp_path / "stable.exe"
    manifest = tmp_path / "pending.json"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")
    manifest.write_text(
        json.dumps(
            {
                "replacements": [
                    {
                        "source": str(source),
                        "destination": str(destination),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    attempts = 0

    def replace(staged, stable):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("still running")
        Path(stable).write_bytes(Path(staged).read_bytes())
        Path(staged).unlink()

    clock = iter((0.0, 0.0, 0.1))

    assert apply_replacements(
        manifest,
        replace=replace,
        sleep=lambda _seconds: None,
        monotonic=lambda: next(clock),
        timeout=1,
    ) is True
    assert attempts == 2
    assert destination.read_bytes() == b"new"
    assert not manifest.exists()


def test_apply_replacements_preserves_manifest_after_timeout(tmp_path):
    from agent_meeting.installation.windows_deferred_replace import (
        apply_replacements,
    )

    source = tmp_path / "new.exe"
    destination = tmp_path / "stable.exe"
    manifest = tmp_path / "pending.json"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")
    manifest.write_text(
        json.dumps(
            {
                "replacements": [
                    {
                        "source": str(source),
                        "destination": str(destination),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    clock = iter((0.0, 1.0))

    assert apply_replacements(
        manifest,
        replace=lambda *_args: (_ for _ in ()).throw(
            PermissionError("locked")
        ),
        sleep=lambda _seconds: None,
        monotonic=lambda: next(clock),
        timeout=1,
    ) is False
    assert manifest.exists()
    assert destination.read_bytes() == b"old"
