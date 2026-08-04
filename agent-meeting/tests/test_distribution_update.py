import importlib.util
import sys
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PLUGIN_ROOT / "src"
INSTALLER = (
    PLUGIN_ROOT.parent
    / "installers"
    / "shared"
    / "install-agent-meeting-package.py"
)


@pytest.fixture(autouse=True)
def add_source_root(monkeypatch):
    monkeypatch.syspath_prepend(str(SRC_ROOT))


def _load_package_installer():
    spec = importlib.util.spec_from_file_location(
        "agent_meeting_package_installer_test",
        INSTALLER,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_detect_targets_uses_client_directories(tmp_path, monkeypatch):
    from agent_meeting.installation.distribution_update import (
        TARGET_CLAUDE_CODE,
        TARGET_CODEX,
        detect_targets,
    )

    monkeypatch.setattr("shutil.which", lambda _command: None)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".codex").mkdir()

    assert detect_targets(home=tmp_path) == (TARGET_CLAUDE_CODE, TARGET_CODEX)


def test_install_release_runs_shared_runtime_once_and_selected_adapters(tmp_path):
    from agent_meeting.installation.distribution_update import (
        TARGET_CLAUDE_CODE,
        TARGET_CODEX,
        install_release,
    )

    commands = []
    source_root = tmp_path / "plugins"
    for project in ("agent-meeting", "mycodex"):
        manifest = source_root / project / "pyproject.toml"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(
            "[project]\nversion = \"0.15.3\"\n",
            encoding="utf-8",
        )

    def fake_run(command, **_kwargs):
        commands.append(command)

    install_release(
        source_root=source_root,
        meeting_home=tmp_path / "meeting",
        targets=(TARGET_CLAUDE_CODE, TARGET_CODEX),
        run=fake_run,
    )

    assert commands == [
        [
            sys.executable,
            str(source_root / "installers/install.py"),
            "--target",
            "all",
            "--source-root",
            str(source_root),
            "--meeting-home",
            str(tmp_path / "meeting"),
        ]
    ]


def test_package_installer_applies_service_to_explicit_isolated_home(
    tmp_path,
    monkeypatch,
):
    installer = _load_package_installer()
    source_root = tmp_path / "source"
    meeting_home = tmp_path / "isolated-home"
    hub_calls = []
    lifecycle_calls = []

    monkeypatch.setattr(installer.sys, "platform", "darwin")
    monkeypatch.setattr(
        installer,
        "install_runtime",
        lambda **kwargs: {
            "version": "0.16.0",
            "runtime": str(meeting_home / "runtimes" / "0.16.0"),
        },
    )
    monkeypatch.setattr(
        installer,
        "ensure_local_message_hub_service",
        hub_calls.append,
    )
    monkeypatch.setattr(
        installer,
        "ensure_lifecycle_control_service",
        lifecycle_calls.append,
    )

    assert installer.main(
        [
            "--source-root",
            str(source_root),
            "--meeting-home",
            str(meeting_home),
        ]
    ) == 0
    assert hub_calls == [meeting_home.resolve()]
    assert lifecycle_calls == [meeting_home.resolve()]


def test_package_installer_applies_codex_configuration_directly(
    tmp_path,
    monkeypatch,
):
    installer = _load_package_installer()
    source_root = tmp_path / "source"
    meeting_home = tmp_path / "meeting"
    codex_home = tmp_path / "codex"
    configuration_calls = []

    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(installer.sys, "platform", "darwin")
    monkeypatch.setattr(
        installer,
        "install_runtime",
        lambda **_kwargs: {
            "version": "0.17.4",
            "runtime": str(meeting_home / "runtimes" / "0.17.4"),
        },
    )
    monkeypatch.setattr(
        installer,
        "ensure_local_message_hub_service",
        lambda _meeting_home: None,
    )
    monkeypatch.setattr(
        installer,
        "ensure_lifecycle_control_service",
        lambda _meeting_home: None,
    )
    monkeypatch.setattr(
        installer,
        "configure_codex_user_environment",
        lambda **kwargs: (
            configuration_calls.append(kwargs)
            or {"first_install": False, "control_url": kwargs["explicit_control"]}
        ),
    )

    assert installer.main(
        [
            "--source-root",
            str(source_root),
            "--meeting-home",
            str(meeting_home),
            "--configure-codex",
            "--control-url",
            "http://10.0.0.9:8765",
            "--enable-full-automation",
        ]
    ) == 0
    assert configuration_calls == [
        {
            "meeting_home": meeting_home.resolve(),
            "codex_home": codex_home,
            "explicit_control": "http://10.0.0.9:8765",
            "enable_full_automation": True,
            "prompt": installer._prompt,
        }
    ]


def test_refresh_checkout_fast_forwards_existing_public_checkout(tmp_path):
    from agent_meeting.installation.distribution_update import (
        CHECKOUT_REFRESH_TIMEOUT_SECONDS,
        refresh_checkout,
    )

    checkout = tmp_path / "plugins"
    (checkout / ".git").mkdir(parents=True)
    commands = []

    def fake_run(command, **kwargs):
        commands.append((command, kwargs))

    assert refresh_checkout(
        checkout=checkout,
        repository="https://example.test/plugins.git",
        run=fake_run,
    ) == checkout
    assert commands == [
        (
            [
                "git",
                "-C",
                str(checkout),
                "fetch",
                "--prune",
                "origin",
                "main",
            ],
            {"check": True, "timeout": CHECKOUT_REFRESH_TIMEOUT_SECONDS},
        ),
        (
            [
                "git",
                "-C",
                str(checkout),
                "reset",
                "--hard",
                "FETCH_HEAD",
            ],
            {"check": True, "timeout": CHECKOUT_REFRESH_TIMEOUT_SECONDS},
        ),
    ]


def test_refresh_checkout_kills_the_complete_git_process_group_on_timeout(
    tmp_path, monkeypatch
):
    import subprocess

    from agent_meeting.installation import distribution_update

    class StalledGit:
        pid = 1234
        returncode = None

        def __init__(self):
            self.communicate_calls = 0

        def communicate(self, *, timeout=None):
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                raise subprocess.TimeoutExpired(["git"], timeout)
            return "", ""

    killed = []
    monkeypatch.setattr(
        distribution_update.subprocess,
        "Popen",
        lambda *args, **kwargs: StalledGit(),
    )
    monkeypatch.setattr(distribution_update.os, "killpg", lambda pid, signal: killed.append((pid, signal)))

    with pytest.raises(RuntimeError, match="timed out after"):
        distribution_update.refresh_checkout(
            checkout=tmp_path / "plugins",
            repository="https://example.test/plugins.git",
            sleep=lambda _seconds: None,
        )

    assert killed == [
        (1234, distribution_update.signal.SIGKILL),
        (1234, distribution_update.signal.SIGKILL),
        (1234, distribution_update.signal.SIGKILL),
        (1234, distribution_update.signal.SIGKILL),
    ]


def test_refresh_checkout_reports_complete_git_error(monkeypatch):
    from agent_meeting.installation import distribution_update

    class FailedGit:
        pid = 1234
        returncode = 1

        @staticmethod
        def communicate(*, timeout=None):
            return "", (
                "error: local changes would be overwritten\n"
                "Please commit or stash them before you merge.\n"
                "Aborting"
            )

    monkeypatch.setattr(
        distribution_update.subprocess,
        "Popen",
        lambda *args, **kwargs: FailedGit(),
    )

    with pytest.raises(RuntimeError) as error:
        distribution_update._refresh_checkout_process(["git"])

    assert str(error.value) == (
        "could not refresh agent-meeting checkout: "
        "error: local changes would be overwritten\n"
        "Please commit or stash them before you merge.\n"
        "Aborting"
    )


def test_refresh_checkout_reports_each_retry_attempt(tmp_path, capsys):
    from agent_meeting.installation import distribution_update

    attempts = 0
    delays = []

    def flaky_run(_command, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts < 4:
            raise RuntimeError("temporary network failure")

    assert distribution_update.refresh_checkout(
        checkout=tmp_path / "plugins",
        repository="https://example.test/plugins.git",
        run=flaky_run,
        sleep=delays.append,
    ) == tmp_path / "plugins"
    assert capsys.readouterr().out.splitlines() == [
        "Retrying agent-meeting checkout refresh (1/3) in 1s after: temporary network failure",
        "Retrying agent-meeting checkout refresh (2/3) in 2s after: temporary network failure",
        "Retrying agent-meeting checkout refresh (3/3) in 4s after: temporary network failure",
    ]
    assert delays == [1, 2, 4]


def test_release_version_rejects_local_cachebuster_suffix(tmp_path):
    from agent_meeting.installation.distribution_update import release_version

    for project, version in (
        ("agent-meeting", "0.15.3+codex.local"),
        ("mycodex", "0.15.3+codex.local"),
    ):
        manifest = tmp_path / project / "pyproject.toml"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(f"[project]\nversion = \"{version}\"\n", encoding="utf-8")

    import pytest

    with pytest.raises(ValueError, match="must not use local cachebuster"):
        release_version(tmp_path)


def test_am_update_check_reports_runtime_and_detected_targets(
    tmp_path, monkeypatch, capsys
):
    from agent_meeting.commands import am_update_cli

    meeting_home = tmp_path / "meeting"
    meeting_home.mkdir()
    (meeting_home / "active-runtime.json").write_text(
        '{"version": "0.15.3"}', encoding="utf-8"
    )
    monkeypatch.setattr(
        am_update_cli.distribution_update,
        "default_meeting_home",
        lambda: meeting_home,
    )
    monkeypatch.setattr(
        am_update_cli.distribution_update,
        "detect_targets",
        lambda: ("claude-code",),
    )

    assert am_update_cli.main(["--check"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "active runtime: 0.15.3",
        "installed targets: claude-code",
    ]
