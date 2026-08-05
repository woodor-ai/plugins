from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import zipfile

import pytest


PRODUCT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PRODUCT_ROOT / "scripts" / "bootstrap_runtime.py"


def load_bootstrap():
    spec = importlib.util.spec_from_file_location("runtime_bootstrap_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def archive_bytes() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("plugins-release/installers/install.py", "pass\n")
    return output.getvalue()


class DownloadResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def test_release_archive_uses_installed_plugin_version(tmp_path):
    module = load_bootstrap()
    manifest = tmp_path / ".codex-plugin" / "plugin.json"
    manifest.parent.mkdir()
    manifest.write_text(
        json.dumps({"version": "0.18.33+codex.local-test"}),
        encoding="utf-8",
    )

    assert module.release_archive_url(tmp_path) == (
        "https://dl.omi-atlas.com/am/releases/v0.18.33/agent-meeting.zip"
    )


def test_bundled_script_resolves_plugin_root_and_version():
    module = load_bootstrap()

    assert module.plugin_root(SCRIPT) == PRODUCT_ROOT
    assert module.plugin_version(PRODUCT_ROOT) == "0.18.33"


def test_standalone_bootstrap_requires_a_plugin_version():
    module = load_bootstrap()

    with pytest.raises(RuntimeError, match="plugin version is unavailable"):
        module.release_archive_url(None)


def test_bootstrap_downloads_and_runs_shared_installer(capsys):
    module = load_bootstrap()
    calls = []

    def open_archive(request, timeout):
        calls.append((request.full_url, timeout))
        return DownloadResponse(archive_bytes())

    def run_installer(command, check):
        calls.append((command, check))

    module.install_runtime(
        target="codex",
        root=None,
        archive_url="https://example.test/agent-meeting.zip",
        opener=open_archive,
        run=run_installer,
    )

    assert calls[0] == ("https://example.test/agent-meeting.zip", 120)
    command, check = calls[1]
    assert check is True
    assert command[0] == module.sys.executable
    assert command[2:] == [
        "--target",
        "codex",
        "--source-root",
        command[-1],
    ]
    assert command[1].endswith("installers/install.py")
    assert Path(command[1]).parent.name == "installers"
    assert Path(command[-1]).name == "plugins-release"
    assert "Open a new terminal and run: amcodex <name>" in capsys.readouterr().out


def test_bootstrap_identifies_download_failure_stage():
    module = load_bootstrap()

    def fail_download(_request, timeout):
        assert timeout == 120
        raise PermissionError("sandbox blocked download")

    with pytest.raises(
        module.BootstrapStageError,
        match="download failed: sandbox blocked download",
    ):
        module.install_runtime(
            target="codex",
            root=None,
            archive_url="https://example.test/agent-meeting.zip",
            opener=fail_download,
        )


def test_bootstrap_identifies_runtime_installer_failure_stage():
    module = load_bootstrap()

    def open_archive(_request, timeout):
        assert timeout == 120
        return DownloadResponse(archive_bytes())

    def fail_installer(_command, check):
        assert check is True
        raise PermissionError("sandbox blocked user profile")

    with pytest.raises(
        module.BootstrapStageError,
        match="runtime installer failed: sandbox blocked user profile",
    ):
        module.install_runtime(
            target="codex",
            root=None,
            archive_url="https://example.test/agent-meeting.zip",
            opener=open_archive,
            run=fail_installer,
        )
