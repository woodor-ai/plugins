import importlib.util
import json
import subprocess
import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
MIGRATION = PLUGIN_ROOT / "bin" / "handoff-codex-migrate.py"
RUNNER = PLUGIN_ROOT / "bin" / "handoff-python-hook"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


migration = _load("handoff_codex_migrate", MIGRATION)


def _state(key: str, digest: str) -> str:
    return (
        f"[hooks.state.{json.dumps(key)}]\n"
        "enabled = true\n"
        f'trusted_hash = "{digest}"\n\n'
    )


def _hook(matcher: str, command: str) -> str:
    return (
        "[[hooks.SessionStart]]\n"
        f'matcher = "{matcher}"\n\n'
        "[[hooks.SessionStart.hooks]]\n"
        'type = "command"\n'
        f"command = {json.dumps(command)}\n\n"
    )


def test_codex_migration_removes_only_legacy_handoff_and_reindexes_state(tmp_path):
    config = tmp_path / "config.toml"
    source = str(config)
    native_key = "handoff@woodor:hooks/hooks.json:session_start:0:0"
    content = (
        _state(f"{source}:session_start:0:0", "sha256:keep-zero")
        + _state(f"{source}:session_start:1:0", "sha256:remove")
        + _state(f"{source}:session_start:2:0", "sha256:keep-two")
        + _state(native_key, "sha256:native")
        + _hook("startup", "python3 /tmp/other-start.py")
        + _hook(
            "resume",
            "python3 /tmp/woodor/handoff/0.6.0/codex/codex-handoff-pickup.py",
        )
        + _hook("compact", "python3 /tmp/other-compact.py")
        + "[mcp_servers.example]\ncommand = \"example\"\n"
    )
    config.write_text(content, encoding="utf-8")

    assert migration.migrate_codex_config(config) == 1
    migrated = config.read_text(encoding="utf-8")

    assert "codex-handoff-pickup.py" not in migrated
    assert "sha256:remove" not in migrated
    assert f"{source}:session_start:0:0" in migrated
    assert f"{source}:session_start:1:0" in migrated
    assert f"{source}:session_start:2:0" not in migrated
    assert "sha256:keep-zero" in migrated
    assert "sha256:keep-two" in migrated
    assert native_key in migrated
    assert "sha256:native" in migrated
    assert "python3 /tmp/other-start.py" in migrated
    assert "python3 /tmp/other-compact.py" in migrated
    assert migration.migrate_codex_config(config) == 0


def test_codex_migration_recognizes_pre_wrapper_shared_pickup(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        _hook("startup", "python3 /tmp/handoff/bin/handoff-pickup.py"),
        encoding="utf-8",
    )

    assert migration.migrate_codex_config(config) == 1
    assert "SessionStart" not in config.read_text(encoding="utf-8")


def test_codex_migration_loads_without_tomllib(monkeypatch):
    monkeypatch.setitem(sys.modules, "tomllib", None)

    legacy_python = _load("handoff_codex_migrate_legacy_python", MIGRATION)

    content = _hook("startup", "python3 /tmp/handoff/codex-handoff-pickup.py")
    migrated, removed = legacy_python.migrate_config_text(
        content,
        Path("/tmp/config.toml"),
    )
    assert removed == 1
    assert "SessionStart" not in migrated


def test_posix_hook_runner_preserves_script_exit_status(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python3"
    fake_python.write_text("#!/bin/sh\nexit 23\n", encoding="utf-8")
    fake_python.chmod(0o755)

    result = subprocess.run(
        [str(RUNNER), "/tmp/hook.py"],
        env={"PATH": str(fake_bin)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 23


def test_posix_hook_runner_reports_missing_python(tmp_path):
    result = subprocess.run(
        [str(RUNNER), "/tmp/hook.py"],
        env={"PATH": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 127
    assert "Python 3 is required" in result.stderr
