from pathlib import Path
import subprocess
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

    assert mycodex.__version__ == "0.15.4"
    assert mycodex.__version__ == agent_meeting.__version__


def test_codex_app_server_environment_marks_mycodex_runtime(monkeypatch):
    from mycodex.codex_session_broker import broker_process

    monkeypatch.delenv("AGENT_MEETING_CODEX_RUNTIME", raising=False)
    environment = broker_process.codex_app_server_environment({"KEEP": "yes"})

    assert environment == {
        "KEEP": "yes",
        "AGENT_MEETING_CODEX_RUNTIME": "1",
    }


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
        "1 mycodex session(s) are active\n"
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
    from mycodex.codex_session_broker.meeting_inbox_delivery import (
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
    assert "peer@tools [via woodor:agent-meeting]: please review" in text
    assert "Message ID: 7" in text
    assert "private full body" not in text


def test_mycodex_update_is_deprecated(capsys):
    from mycodex.commands import mycodex_cli

    assert mycodex_cli.main(["update"]) == 2
    assert "has moved to am-update" in capsys.readouterr().err


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


def test_codex_instructions_upgrade_legacy_managed_block(tmp_path):
    from mycodex.ai_platforms.codex import agent_meeting_instructions

    codex_home = tmp_path / "codex"
    agents_path = codex_home / "AGENTS.md"
    agents_path.parent.mkdir(parents=True)
    agents_path.write_text(
        "keep before\n"
        f"{agent_meeting_instructions.LEGACY_AGENTS_BEGIN}\n"
        "stale content\n"
        f"{agent_meeting_instructions.AGENTS_END}\n"
        "keep after\n",
        encoding="utf-8",
    )
    meeting = tmp_path / "meeting.exe"

    first_install = (
        agent_meeting_instructions.install_agent_meeting_instructions(
            codex_home=codex_home,
            meeting_command=meeting,
            control_url="http://10.0.0.1:8765",
            is_windows=True,
        )
    )

    text = agents_path.read_text(encoding="utf-8")
    assert first_install is False
    assert "keep before" in text and "keep after" in text
    assert "stale content" not in text
    assert agent_meeting_instructions.AGENTS_BEGIN in text
    assert f'& "{meeting}" message NAME@PROJECT N' in text
    assert "MEETING_SELF" not in text
    assert "MEETING_HOST" not in text


def test_control_selection_prefers_explicit_then_discovered_then_saved(
    tmp_path,
):
    from mycodex.installation import control_endpoint_selection

    meeting_home = tmp_path / "meeting"
    control_endpoint_selection.write_launcher_default(
        meeting_home,
        "http://saved:8765",
    )
    prompt = lambda *_args: "http://prompt:8765"

    assert control_endpoint_selection.select_control(
        meeting_home=meeting_home,
        discovered="http://discovered:8765",
        explicit="http://explicit:8765",
        prompt=prompt,
        health_check=lambda _url: True,
    ) == "http://explicit:8765"
    assert control_endpoint_selection.select_control(
        meeting_home=meeting_home,
        discovered="http://discovered:8765",
        explicit="",
        prompt=prompt,
        health_check=lambda _url: True,
    ) == "http://discovered:8765"
    assert control_endpoint_selection.select_control(
        meeting_home=meeting_home,
        discovered="",
        explicit="",
        prompt=prompt,
        health_check=lambda _url: True,
    ) == "http://saved:8765"


def test_codex_user_configuration_preserves_unrelated_sections(tmp_path):
    from mycodex.ai_platforms.codex import user_configuration

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
