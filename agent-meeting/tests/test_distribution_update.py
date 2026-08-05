import importlib.util
import io
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


def test_selected_target_collapses_every_supported_adapter():
    from agent_meeting.installation.distribution_update import (
        TARGET_CLAUDE_CODE,
        TARGET_CODEX,
        selected_target,
    )

    assert selected_target((TARGET_CLAUDE_CODE, TARGET_CODEX)) == "all"
    assert selected_target((TARGET_CODEX,)) == TARGET_CODEX


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
            or {"control_url": kwargs["explicit_control"]}
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


def test_install_latest_uses_disposable_public_installer(tmp_path):
    from agent_meeting.installation import distribution_update

    meeting_home = tmp_path / "meeting"
    legacy = distribution_update.legacy_checkout(meeting_home)
    (legacy / ".git").mkdir(parents=True)
    requested = []
    commands = []
    installer_paths = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def opener(request, timeout):
        requested.append((request.full_url, timeout))
        return Response(b"# current public installer\n")

    def run(command, **kwargs):
        commands.append((command, kwargs))
        installer = Path(command[1])
        installer_paths.append(installer)
        assert installer.read_bytes() == b"# current public installer\n"

    distribution_update.install_latest(
        meeting_home=meeting_home,
        targets=("claude-code", "codex"),
        opener=opener,
        run=run,
    )

    assert requested == [
        (
            distribution_update.PUBLIC_INSTALLER_URL,
            distribution_update.DOWNLOAD_TIMEOUT_SECONDS,
        )
    ]
    assert commands == [
        (
            [
                sys.executable,
                str(installer_paths[0]),
                "--target",
                "all",
                "--meeting-home",
                str(meeting_home),
            ],
            {"check": True},
        )
    ]
    assert not installer_paths[0].exists()
    assert not legacy.exists()


def test_install_latest_removes_legacy_checkout_after_download_failure(tmp_path):
    from agent_meeting.installation import distribution_update

    meeting_home = tmp_path / "meeting"
    legacy = distribution_update.legacy_checkout(meeting_home)
    (legacy / ".git").mkdir(parents=True)

    def fail_download(*_args, **_kwargs):
        raise OSError("network unavailable")

    with pytest.raises(OSError, match="network unavailable"):
        distribution_update.install_latest(
            meeting_home=meeting_home,
            targets=("codex",),
            opener=fail_download,
        )

    assert not legacy.exists()


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


def test_am_update_offers_no_way_to_redirect_the_installer_source():
    import inspect

    from agent_meeting.commands import am_update_cli
    from agent_meeting.installation import distribution_update

    # The updater must always fetch the released public installer, so neither
    # the command line nor the call signature may point it somewhere else.
    with pytest.raises(SystemExit):
        am_update_cli.build_parser().parse_args(
            ["--installer-url", "https://example.invalid/install.py"]
        )
    assert "installer_url" not in inspect.signature(
        distribution_update.install_latest
    ).parameters
