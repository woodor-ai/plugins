from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "installers/build-agent-meeting-release.py"


def _load_builder():
    spec = importlib.util.spec_from_file_location(
        "agent_meeting_release_builder",
        BUILDER,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_bundle(path: Path, module, *, extra: str | None = None) -> None:
    prefix = module.bundle_prefix("0.18.32")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(prefix + "LICENSE", "MIT\n")
        for required in module.REQUIRED_BUNDLE_FILES:
            content = (
                json.dumps({"version": "0.18.32"})
                if required == "agent-meeting/.codex-plugin/plugin.json"
                else "test\n"
            )
            archive.writestr(prefix + required, content)
        if extra:
            archive.writestr(prefix + extra, "unrelated\n")


def test_bundle_paths_only_include_agent_meeting_runtime_inputs():
    module = _load_builder()

    assert set(path.split("/", 1)[0] for path in module.BUNDLE_PATHS) == {
        "LICENSE",
        "agent-meeting",
        "mycodex",
        "installers",
    }
    assert not any(
        path.startswith(("handoff", "init-agents", "init-proj", "save-money", "docs"))
        for path in module.BUNDLE_PATHS
    )


def test_verify_bundle_accepts_agent_meeting_only_archive(tmp_path):
    module = _load_builder()
    bundle = tmp_path / "agent-meeting.zip"
    _write_bundle(bundle, module)

    module.verify_bundle(bundle, "0.18.32")


def test_verify_bundle_rejects_unrelated_plugin(tmp_path):
    module = _load_builder()
    bundle = tmp_path / "agent-meeting.zip"
    _write_bundle(bundle, module, extra="handoff/README.md")

    with pytest.raises(RuntimeError, match="top-level entries"):
        module.verify_bundle(bundle, "0.18.32")
