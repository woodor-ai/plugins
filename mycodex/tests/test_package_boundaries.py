import json
from pathlib import Path
import sys

import pytest


PRODUCT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PRODUCT_ROOT.parent
@pytest.fixture(autouse=True)
def add_product_sources(monkeypatch):
    monkeypatch.syspath_prepend(str(PRODUCT_ROOT / "src"))
    monkeypatch.syspath_prepend(
        str(REPOSITORY_ROOT / "agent-meeting" / "src")
    )


def test_product_version_matches_agent_meeting_runtime():
    import mycodex
    import agent_meeting

    assert mycodex.__version__ == "0.18.35"
    assert mycodex.__version__ == agent_meeting.__version__
    manifests = (
        REPOSITORY_ROOT / "agent-meeting/.codex-plugin/plugin.json",
        REPOSITORY_ROOT / "agent-meeting/.claude-plugin/plugin.json",
    )
    assert all(
        json.loads(manifest.read_text(encoding="utf-8"))["version"]
        == mycodex.__version__
        for manifest in manifests
    )


def test_codex_plugin_does_not_load_the_claude_session_start_hook():
    plugin_root = REPOSITORY_ROOT / "agent-meeting"
    codex_manifest = json.loads(
        (plugin_root / ".codex-plugin/plugin.json").read_text(
            encoding="utf-8"
        )
    )
    claude_manifest = json.loads(
        (plugin_root / ".claude-plugin/plugin.json").read_text(
            encoding="utf-8"
        )
    )

    assert not (plugin_root / "hooks/hooks.json").exists()
    assert "hooks" not in codex_manifest
    assert claude_manifest["hooks"] == "./claude-hooks/hooks.json"
    assert (plugin_root / "claude-hooks/hooks.json").is_file()


def test_amcodex_descriptor_does_not_persist_launch_arguments(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("MEETING_HOME", str(tmp_path))
    from mycodex.launcher.codex_tui_session import Launcher

    launcher = Launcher("worker", "tools", "http://127.0.0.1:8765")
    launcher.session = {
        "identity": "worker@tools",
        "proxy_url": "ws://127.0.0.1:9999",
    }
    descriptor = launcher.descriptor()

    assert descriptor["wrapper"] == "amcodex"
    assert descriptor["identity"] == "worker@tools"
    assert descriptor["launch_recipe"]["args_persisted"] is False
    assert "ws://127.0.0.1:9999" not in json.dumps(descriptor)


def test_amcodex_launch_log_shows_am_msgd_instead_of_local_proxy(monkeypatch):
    from mycodex.launcher import codex_tui_session

    messages = []

    class Process:
        pid = 1234

        def wait(self):
            return 0

    launcher = codex_tui_session.Launcher(
        "worker",
        "tools",
        "http://10.0.0.8:8765",
    )
    launcher.session = {
        "identity": "worker@tools",
        "proxy_url": "ws://127.0.0.1:9999",
    }
    monkeypatch.setattr(codex_tui_session, "log", messages.append)
    monkeypatch.setattr(
        codex_tui_session,
        "set_terminal_title",
        lambda _title: None,
    )
    monkeypatch.setattr(launcher, "start_control_server", lambda: None)
    monkeypatch.setattr(launcher, "publish_descriptor", lambda: None)
    monkeypatch.setattr(
        codex_tui_session.subprocess,
        "Popen",
        lambda *_args, **_kwargs: Process(),
    )

    launcher.run_codex()

    assert messages == [
        "launching Codex foreground; am-msgd=http://10.0.0.8:8765"
    ]
    assert "127.0.0.1" not in messages[0]


def test_codex_app_server_environment_marks_mycodex_runtime(monkeypatch):
    from mycodex.codex_session_broker import broker_process

    monkeypatch.delenv("AGENT_MEETING_CODEX_RUNTIME", raising=False)
    environment = broker_process.codex_app_server_environment({"KEEP": "yes"})

    assert environment == {
        "KEEP": "yes",
        "AGENT_MEETING_CODEX_RUNTIME": "1",
    }


def test_windows_codex_app_server_uses_npm_native_executable(tmp_path):
    from mycodex.operating_systems import codex_cli_command

    npm_root = tmp_path / "npm"
    batch_file = npm_root / "codex.cmd"
    native_executable = (
        npm_root
        / "node_modules/@openai/codex/node_modules/@openai"
        / "codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe"
    )
    native_executable.parent.mkdir(parents=True)
    native_executable.touch()
    batch_file.touch()
    locations = {
        "codex.exe": None,
        "codex.cmd": str(batch_file),
    }
    effort = 'model_reasoning_effort="high"'
    command = codex_cli_command.resolve(
        ["app-server", "--config", effort],
        platform_name="win32",
        which=locations.get,
    )

    assert command == [
        str(native_executable),
        "app-server",
        "--config",
        effort,
    ]


def test_windows_codex_app_server_rejects_incomplete_npm_install(tmp_path):
    from mycodex.operating_systems import codex_cli_command

    batch_file = tmp_path / "npm" / "codex.cmd"
    batch_file.parent.mkdir()
    batch_file.touch()

    with pytest.raises(FileNotFoundError, match="reinstall @openai/codex"):
        codex_cli_command.resolve(
            ["app-server"],
            platform_name="win32",
            which=lambda name: str(batch_file) if name == "codex.cmd" else None,
        )


def test_windows_codex_app_server_prefers_native_executable():
    from mycodex.operating_systems import codex_cli_command

    command = codex_cli_command.resolve(
        ["app-server", "--listen", "ws://127.0.0.1:8792"],
        platform_name="win32",
        which=lambda name: r"C:\Tools\codex.exe"
        if name == "codex.exe"
        else None,
    )

    assert command == [
        r"C:\Tools\codex.exe",
        "app-server",
        "--listen",
        "ws://127.0.0.1:8792",
    ]


def test_daemon_update_defers_when_a_session_is_active(monkeypatch, capsys):
    from mycodex.commands import am_codexd_cli

    monkeypatch.setattr(
        am_codexd_cli,
        "status_info",
        lambda: {"version": "0.15.2", "sessions": 1, "ok": True},
    )
    monkeypatch.setattr(am_codexd_cli, "installed_version", lambda: "0.15.1")

    assert am_codexd_cli.main(["update", "--defer-if-active"]) == 0
    assert capsys.readouterr().out == (
        "deferring am-codexd update from 0.15.2 to 0.15.1 while "
            "1 amcodex session(s) are active\n"
    )


def test_central_hub_transports_bypass_system_proxies(monkeypatch):
    import urllib.request

    from mycodex.codex_session_broker import broker_process

    options = broker_process.central_websocket_options({"X-Test": "yes"})
    assert options["proxy"] is None
    assert options["additional_headers"] == {"X-Test": "yes"}

    assert isinstance(
        broker_process.CENTRAL_HTTP_PROXY_HANDLER,
        urllib.request.ProxyHandler,
    )
    assert broker_process.CENTRAL_HTTP_PROXY_HANDLER.proxies == {}

    captured = {}

    class Response:
        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_open(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(broker_process.CENTRAL_HTTP_OPENER, "open", fake_open)

    assert broker_process.http_json(
        "GET", "http://100.82.70.77:8765", "/health", timeout=3
    ) == {"ok": True}
    assert captured == {
        "url": "http://100.82.70.77:8765/health",
        "timeout": 3,
    }


def test_session_lease_has_canonical_identity_and_proxy_url():
    from mycodex.codex_session_broker.session_lease_registry import (
        SessionLease,
    )

    session = SessionLease(
        launch_id="launch-1",
        name="plugins",
        project="tools",
        cwd="/repo",
        control_url="http://10.0.0.1:8765/",
        thread_id=None,
        cursor=3,
        proxy_host="127.0.0.1",
    )
    session.proxy_port = 9000

    assert session.identity == "plugins@tools"
    assert session.control_url == "http://10.0.0.1:8765"
    assert session.proxy_url == "ws://127.0.0.1:9000"


def test_tui_scope_adds_runtime_context_without_overwriting_existing():
    from mycodex.codex_session_broker.session_lease_registry import (
        SessionLease,
    )
    from mycodex.codex_session_broker.tui_websocket_proxy import (
        scope_client_request,
    )

    session = SessionLease(
        launch_id="launch-1",
        name="plugins",
        project="tools",
        cwd="/repo",
        control_url="http://10.0.0.1:8765",
        thread_id=None,
        cursor=0,
    )
    scoped = scope_client_request(
        session,
        {
            "method": "thread/start",
            "params": {"developerInstructions": "existing"},
        },
    )

    assert scoped["params"]["cwd"] == "/repo"
    instructions = scoped["params"]["developerInstructions"]
    assert instructions.startswith("existing\n\n")
    assert "plugins@tools" in instructions
    assert "--host http://10.0.0.1:8765" in instructions


def test_meeting_inbox_renderer_uses_provenance_without_message_body():
    from mycodex.codex_session_broker.hub_inbox_delivery import (
        build_injection,
    )
    from mycodex.codex_session_broker.session_lease_registry import (
        SessionLease,
    )

    session = SessionLease(
        launch_id="launch-1",
        name="plugins",
        project="tools",
        cwd="/repo",
        control_url="http://10.0.0.1:8765",
        thread_id="thread-1",
        cursor=0,
    )
    session.pending[7] = {
        "id": 7,
        "sender": "peer",
        "sender_project": "tools",
        "kind": "回应",
        "ask": "please review",
        "body": "private full body",
        "created_at": 100,
        "deliver": True,
    }

    selected, text = build_injection(
        session,
        control_stale_seconds=600,
        now=100,
    )

    assert selected == [7]
    assert text == (
        "📬 New Message from peer@tools to plugins@tools "
        "[via woodor:agent-meeting] Message ID: 7"
    )
    assert "please review" not in text
    assert "private full body" not in text


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("localhost", "http://localhost:8765"),
        ("127.0.0.1", "http://127.0.0.1:8765"),
        ("127.0.0.1:9000", "http://127.0.0.1:9000"),
        ("10.0.0.8:9876", "http://10.0.0.8:9876"),
        ("::1", "http://[::1]:8765"),
        ("[::1]:9000", "http://[::1]:9000"),
        ("http://localhost:8765/", "http://localhost:8765"),
    ],
)
def test_mycodex_normalizes_am_msgd_endpoint(endpoint, expected):
    from mycodex.launcher import codex_tui_session

    assert codex_tui_session.normalize_am_msgd(endpoint) == expected


def test_mycodex_help_exposes_am_msgd_not_control_url(capsys):
    from mycodex.launcher import codex_tui_session

    with pytest.raises(SystemExit) as error:
        codex_tui_session.main(["--help"])

    assert error.value.code == 0
    output = capsys.readouterr().out
    assert "--am-msgd HOST[:PORT]" in output
    assert "--model {sol,terra}" in output
    assert "--effort {xhigh,high,medium}" in output
    assert "--control-url" not in output


def test_windows_background_process_policy_uses_detached_flags(tmp_path):
    from mycodex.operating_systems.windows import codex_background_process

    log_file = object()
    options = codex_background_process.detached_popen_options(log_file)

    assert options["stdout"] is log_file
    assert options["creationflags"] == 0x08000000 | 0x00000200
    assert "start_new_session" not in options
    assert (
        codex_background_process.legacy_runtime_python(tmp_path)
        == str(Path(sys.executable))
    )


def test_macos_background_process_policy_starts_new_session():
    from mycodex.operating_systems.macos import codex_background_process

    log_file = object()
    options = codex_background_process.detached_popen_options(log_file)

    assert options["stdout"] is log_file
    assert options["start_new_session"] is True
    assert "creationflags" not in options


def test_amcodex_default_control_uses_public_am_discovery(monkeypatch):
    from mycodex.launcher import codex_tui_session

    calls = []

    class Result:
        returncode = 0
        stdout = (
            '[{"ip":"10.0.0.8","port":9876,'
            '"is_current":true}]'
        )

    def fake_run(cli, *args, **kwargs):
        calls.append((cli, args, kwargs))
        return Result()

    monkeypatch.setattr(codex_tui_session, "run_am_cli", fake_run)

    assert codex_tui_session.default_control_url() == "http://10.0.0.8:9876"
    assert calls == [
        (
            codex_tui_session.AM_COMMAND,
            ("msgd", "--json"),
            {"timeout": 10},
        )
    ]


def test_codex_configuration_pins_explicit_host_through_am(monkeypatch):
    from mycodex.installation import codex_user_environment

    calls = []

    class Result:
        returncode = 0
        stdout = "control_host pinned"
        stderr = ""

    def fake_run(cli, *args, **kwargs):
        calls.append((cli, args, kwargs))
        return Result()

    monkeypatch.setattr(
        codex_user_environment,
        "run_am_cli",
        fake_run,
    )
    am_command = Path("/runtime/am")

    codex_user_environment._pin_control(
        am_command,
        "http://10.0.0.9:8765",
    )

    assert calls == [
        (
            am_command,
            ("host", "http://10.0.0.9:8765"),
            {"timeout": 10},
        )
    ]


def test_codex_configuration_runs_as_installation_logic(
    tmp_path,
    monkeypatch,
):
    from mycodex.installation import codex_user_environment

    meeting_home = tmp_path / "meeting"
    codex_home = tmp_path / "codex"
    agents_path = codex_home / "AGENTS.md"
    (meeting_home / "bin").mkdir(parents=True)
    agents_path.parent.mkdir(parents=True)
    agents_path.write_text("user-owned instructions\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setattr(
        codex_user_environment,
        "_discover_control",
        lambda _am_command: "http://10.0.0.8:8765",
    )

    result = codex_user_environment.configure_codex_user_environment(
        meeting_home=meeting_home,
        codex_home=codex_home,
        is_windows=False,
    )

    assert result == {"control_url": "http://10.0.0.8:8765"}
    assert agents_path.read_text(encoding="utf-8") == (
        "user-owned instructions\n"
    )
    assert str(meeting_home / "bin") in (
        tmp_path / "user" / ".zshrc"
    ).read_text(encoding="utf-8")


def test_codex_configuration_is_not_a_packaged_runtime_command():
    manifest = (PRODUCT_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "am-configure-codex-user-environment" not in manifest


def test_codex_user_configuration_preserves_unrelated_sections(tmp_path):
    from agent_meeting.ai_platforms.codex import user_configuration

    codex_home = tmp_path / "codex"
    config = codex_home / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        'default_permissions = "ask"\n\n'
        "[features]\n"
        "example = true\n",
        encoding="utf-8",
    )

    user_configuration.enable_full_automation(codex_home)
    user_configuration.ensure_windows_unelevated_sandbox(codex_home)

    text = config.read_text(encoding="utf-8")
    assert 'approval_policy = "never"' in text
    assert 'sandbox_mode = "danger-full-access"' in text
    assert "default_permissions" not in text
    assert "[features]\nexample = true" in text
    assert '[windows]\nsandbox = "unelevated"' in text


def test_packaged_am_codexd_restarts_with_its_own_runtime_python(
    monkeypatch,
):
    """A legacy shared venv may coexist with the active immutable runtime."""
    from mycodex.commands import am_codexd_cli

    monkeypatch.setattr(
        am_codexd_cli.sys,
        "executable",
        "/active-runtime/venv/bin/python",
    )

    assert am_codexd_cli.venv_python() == "/active-runtime/venv/bin/python"
