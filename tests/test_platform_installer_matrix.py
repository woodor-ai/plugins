"""Static contracts for the four OS x AI-platform installer entrypoints."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_macos_installers_share_versioned_runtime_and_platform_registration():
    claude = _text("installers/claude-code/install-on-macos.sh")
    codex = _text("installers/codex/install-on-macos.sh")

    for script in (claude, codex):
        assert "install-agent-meeting-package.py" in script
        assert "migrate-agent-meeting-legacy-layout.py" in script
    assert "register-claude-marketplace.py" in claude
    assert "am-configure-codex-user-environment" in codex
    assert "register-codex-marketplace.py" in codex


def test_windows_installers_prefer_py_launcher_and_check_each_stage():
    claude = _text("installers/claude-code/install-on-windows.ps1")
    codex = _text("installers/codex/install-on-windows.ps1")

    for script in (claude, codex):
        assert "Get-Command py" in script
        assert '$PythonArguments = @("-3")' in script
        assert "Get-Command python -ErrorAction Stop" in script
        assert "Invoke-RepositoryPython" in script
        assert "install-agent-meeting-package.py" in script
        assert "migrate-agent-meeting-legacy-layout.py" in script
        assert "if ($LASTEXITCODE -ne 0)" in script
    assert "register-claude-marketplace.py" in claude
    assert "am-configure-codex-user-environment.exe" in codex
    assert "register-codex-marketplace.py" in codex


def test_platform_registration_supports_non_mutating_smoke_executable():
    claude = _text("installers/shared/register-claude-marketplace.py")
    codex = _text("installers/shared/register-codex-marketplace.py")

    assert 'os.environ.get("CLAUDE_BIN")' in claude
    assert 'os.environ.get("CODEX_BIN")' in codex


def test_common_processes_delegate_os_service_and_spawn_primitives():
    meeting_cli = _text(
        "agent-meeting/src/agent_meeting/commands/meeting_cli.py"
    )
    codex_broker = _text(
        "mycodex/src/mycodex/codex_session_broker/broker_process.py"
    )
    claude_session_start = _text(
        "agent-meeting/src/agent_meeting/ai_platforms/"
        "claude_code/session_start_bootstrap.py"
    )

    for primitive in ("launchctl", "schtasks", "taskkill"):
        assert primitive not in meeting_cli
    for primitive in ("creationflags", "start_new_session"):
        assert primitive not in codex_broker
    assert "operating_systems.macos" in claude_session_start
    assert "operating_systems.windows" in claude_session_start
    assert (
        ROOT
        / "mycodex"
        / "src"
        / "mycodex"
        / "operating_systems"
        / "macos"
        / "codex_background_process.py"
    ).is_file()
    assert (
        ROOT
        / "mycodex"
        / "src"
        / "mycodex"
        / "operating_systems"
        / "windows"
        / "codex_background_process.py"
    ).is_file()
