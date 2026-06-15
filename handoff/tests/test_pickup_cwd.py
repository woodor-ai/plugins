"""
验证 handoff-pickup.py 的 cwd 锚点优先级：
  1. stdin 的 cwd 字段压过 CLAUDE_PROJECT_DIR
  2. stdin 无 cwd 时退回 CLAUDE_PROJECT_DIR
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "bin" / "handoff-pickup.py"


def _write_card(base: Path, content: str) -> None:
    dot_claude = base / ".claude"
    dot_claude.mkdir(parents=True, exist_ok=True)
    (dot_claude / "handoff-pending.md").write_text(content, encoding="utf-8")


def _run(stdin_payload: dict, env_project_dir: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = env_project_dir
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(stdin_payload),
        capture_output=True,
        text=True,
        env=env,
    )


def test_stdin_cwd_wins_over_env():
    """stdin 的 cwd 字段应压过 CLAUDE_PROJECT_DIR。"""
    with tempfile.TemporaryDirectory() as tmp:
        dir_a = Path(tmp) / "A"
        dir_b = Path(tmp) / "B"
        dir_a.mkdir()
        dir_b.mkdir()

        _write_card(dir_a, "card-A")
        _write_card(dir_b, "card-B")

        result = _run(
            {"cwd": str(dir_b), "hook_event_name": "SessionStart", "source": "startup"},
            env_project_dir=str(dir_a),
        )

        assert result.returncode == 0, f"脚本非零退出: {result.stderr}"
        out = json.loads(result.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "card-B" in ctx, f"期望拾取 card-B，实际输出: {ctx}"

        # B 的 pending 应已归档（文件被 rename 走）
        assert not (dir_b / ".claude" / "handoff-pending.md").exists(), "B 的 pending 应已被归档"
        archive_dir = dir_b / "docs" / "handoff" / "archive"
        archived = list(archive_dir.glob("handoff-*.md"))
        assert len(archived) == 1, f"期望 1 个归档文件，实际: {archived}"
        assert "card-B" in archived[0].read_text(encoding="utf-8")

        # A 的卡应原封不动
        assert (dir_a / ".claude" / "handoff-pending.md").exists(), "A 的 pending 不应被动"


def test_fallback_to_env_when_no_stdin_cwd():
    """stdin 无 cwd 时应退回 CLAUDE_PROJECT_DIR。"""
    with tempfile.TemporaryDirectory() as tmp:
        dir_a = Path(tmp) / "A"
        dir_b = Path(tmp) / "B"
        dir_a.mkdir()
        dir_b.mkdir()

        _write_card(dir_a, "card-A-fallback")
        _write_card(dir_b, "card-B-fallback")

        # 喂空 payload（无 cwd 字段）
        result = _run({}, env_project_dir=str(dir_a))

        assert result.returncode == 0, f"脚本非零退出: {result.stderr}"
        out = json.loads(result.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "card-A-fallback" in ctx, f"期望拾取 card-A-fallback，实际输出: {ctx}"

        # A 归档，B 原封不动
        assert not (dir_a / ".claude" / "handoff-pending.md").exists()
        assert (dir_b / ".claude" / "handoff-pending.md").exists()


if __name__ == "__main__":
    test_stdin_cwd_wins_over_env()
    print("PASS: stdin_cwd 压过 CLAUDE_PROJECT_DIR")
    test_fallback_to_env_when_no_stdin_cwd()
    print("PASS: 无 cwd 时退回 CLAUDE_PROJECT_DIR")
