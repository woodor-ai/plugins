import json
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SKILL = PLUGIN_ROOT / "skills" / "handoff" / "SKILL.md"
PICKUP = PLUGIN_ROOT / "bin" / "handoff-pickup.py"


def test_skill_keeps_handoff_fast_and_delta_focused():
    skill = SKILL.read_text(encoding="utf-8")

    assert "≤30 行，只允许以下 3 段" in skill
    assert "默认以当前对话为事实来源" in skill
    assert "禁止为了“完整”做仓库巡检" in skill
    assert "第 5 段" not in skill
    assert "本轮新增文档 / roadmap / 进展" not in skill
    assert "必读文档" not in skill

    assert skill.count("## 1. 当前断点") == 1
    assert skill.count("## 2. Pending 用户决定") == 1
    assert skill.count("## 3. 下一步与遗留待办") == 1


def test_pickup_imports_actions_from_section_three():
    pickup = PICKUP.read_text(encoding="utf-8")

    assert "第 3 段：下一步与遗留待办" in pickup
    assert "第 5 段" not in pickup


def test_host_manifests_publish_the_same_version():
    claude = json.loads(
        (PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    codex = json.loads(
        (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )

    assert claude["version"] == codex["version"] == "0.6.2"
