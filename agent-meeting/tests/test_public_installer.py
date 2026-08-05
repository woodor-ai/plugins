import importlib.util
import io
from pathlib import Path
import zipfile

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
INSTALLER = REPOSITORY_ROOT / "installers/public/agent-meeting-install.py"


@pytest.fixture
def public_installer():
    spec = importlib.util.spec_from_file_location(
        "agent_meeting_public_installer",
        INSTALLER,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _archive() -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as bundle:
        bundle.writestr("plugins-release/installers/install.py", "# installer")
    return stream.getvalue()


def test_detect_target_uses_every_available_client(public_installer, monkeypatch):
    monkeypatch.setattr(
        public_installer.shutil,
        "which",
        lambda command: f"/bin/{command}",
    )
    assert public_installer.detect_target() == "all"


def test_detect_target_requires_a_supported_client(public_installer, monkeypatch):
    monkeypatch.setattr(public_installer.shutil, "which", lambda _command: None)
    with pytest.raises(RuntimeError, match="neither claude nor codex"):
        public_installer.detect_target()


def test_install_downloads_pinned_release_and_runs_repository_installer(
    public_installer,
    tmp_path,
    monkeypatch,
):
    requested = []
    commands = []

    class Response:
        def read(self):
            return _archive()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def open_request(request, timeout):
        requested.append((request.full_url, timeout))
        return Response()

    monkeypatch.setattr(public_installer.urllib.request, "urlopen", open_request)
    monkeypatch.setattr(
        public_installer.subprocess,
        "run",
        lambda command, check: commands.append(command),
    )

    public_installer.install(
        target="codex",
        meeting_home=tmp_path / "meeting",
    )

    assert requested == [(public_installer.ARCHIVE_URL, 60)]
    assert public_installer.ARCHIVE_URL.endswith("/tags/v0.18.22")
    assert commands[0][2:4] == ["--target", "codex"]
    assert commands[0][-2:] == [
        "--meeting-home",
        str((tmp_path / "meeting").resolve()),
    ]
