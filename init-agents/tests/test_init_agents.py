import subprocess
import sys
import tomllib
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN_ROOT / "skills" / "init-agents" / "scripts" / "init_agents.py"


def run_script(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--root",
            str(root),
            *arguments,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def make_project(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'fixture'\n")
    return tmp_path


def test_check_is_read_only_and_reports_missing(tmp_path):
    root = make_project(tmp_path)

    result = run_script(root, "--host", "codex", "--mode", "check")

    assert result.returncode == 0
    assert result.stdout.count("missing") == 3
    assert not (root / ".codex").exists()


def test_apply_creates_valid_codex_profiles(tmp_path):
    root = make_project(tmp_path)

    result = run_script(root, "--host", "codex", "--mode", "apply")

    assert result.returncode == 0
    assert result.stdout.count("created") == 3
    expected = {
        "explore": ("gpt-5.6-terra", "high", "read-only"),
        "rd": ("gpt-5.6-terra", "high", "workspace-write"),
        "planner": ("gpt-5.6-sol", "high", "read-only"),
    }
    for name, (model, effort, sandbox) in expected.items():
        data = tomllib.loads(
            (root / ".codex" / "agents" / f"{name}.toml").read_text()
        )
        assert data["name"] == name
        assert data["model"] == model
        assert data["model_reasoning_effort"] == effort
        assert data["sandbox_mode"] == sandbox
        assert data["agents"]["enabled"] is False


def test_apply_creates_claude_profiles_with_effort_field(tmp_path):
    root = make_project(tmp_path)

    result = run_script(root, "--host", "claude", "--mode", "apply")

    assert result.returncode == 0
    expected = {
        "explore": ("claude-sonnet-5", "medium"),
        "rd": ("claude-sonnet-5", "high"),
        "planner": ("claude-opus-5", "high"),
    }
    for name, (model, effort) in expected.items():
        text = (root / ".claude" / "agents" / f"{name}.md").read_text()
        assert f"model: {model}\n" in text
        assert f"effort: {effort}\n" in text
        assert "reasoningEffort:" not in text


def test_conflict_error_preserves_all_files(tmp_path):
    root = make_project(tmp_path)
    first = run_script(root, "--host", "codex", "--mode", "apply")
    assert first.returncode == 0
    explore = root / ".codex" / "agents" / "explore.toml"
    explore.write_text("user content\n")

    result = run_script(root, "--host", "codex", "--mode", "apply")

    assert result.returncode == 2
    assert "No files changed" in result.stderr
    assert explore.read_text() == "user content\n"


def test_check_shows_diff_and_overwrite_replaces_conflict(tmp_path):
    root = make_project(tmp_path)
    target = root / ".claude" / "agents" / "planner.md"
    target.parent.mkdir(parents=True)
    target.write_text("custom planner\n")

    check = run_script(root, "--host", "claude", "--mode", "check")
    overwrite = run_script(
        root,
        "--host",
        "claude",
        "--mode",
        "apply",
        "--conflict",
        "overwrite",
    )

    assert check.returncode == 0
    assert "different .claude/agents/planner.md" in check.stdout
    assert "-custom planner" in check.stdout
    assert overwrite.returncode == 0
    assert "overwritten .claude/agents/planner.md" in overwrite.stdout
    assert "model: claude-opus-5" in target.read_text()


def test_skip_preserves_conflict_and_creates_missing_profiles(tmp_path):
    root = make_project(tmp_path)
    target = root / ".codex" / "agents" / "rd.toml"
    target.parent.mkdir(parents=True)
    target.write_text("custom rd\n")

    result = run_script(
        root,
        "--host",
        "codex",
        "--mode",
        "apply",
        "--conflict",
        "skip",
    )

    assert result.returncode == 0
    assert "skipped     .codex/agents/rd.toml" in result.stdout
    assert target.read_text() == "custom rd\n"
    assert (target.parent / "explore.toml").is_file()
    assert (target.parent / "planner.toml").is_file()


def test_unrecognized_root_requires_explicit_confirmation(tmp_path):
    result = run_script(tmp_path, "--host", "codex", "--mode", "check")

    assert result.returncode == 2
    assert "--allow-unrecognized-root" in result.stderr

    allowed = run_script(
        tmp_path,
        "--host",
        "codex",
        "--mode",
        "apply",
        "--allow-unrecognized-root",
    )
    assert allowed.returncode == 0


def test_apply_refuses_target_directory_symlink_outside_project(tmp_path):
    root = make_project(tmp_path / "project")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / ".codex").mkdir()
    try:
        (root / ".codex" / "agents").symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")

    result = run_script(root, "--host", "codex", "--mode", "apply")

    assert result.returncode == 2
    assert "outside the project" in result.stderr
    assert list(outside.iterdir()) == []
