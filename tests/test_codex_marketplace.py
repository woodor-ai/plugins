import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MARKETPLACE = ROOT / ".agents" / "plugins" / "marketplace.json"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_codex_marketplace_lists_every_codex_native_plugin():
    catalog = _read_json(MARKETPLACE)
    entries = catalog["plugins"]
    by_name = {entry["name"]: entry for entry in entries}
    native_plugins = {
        path.parent.parent.name
        for path in ROOT.glob("*/.codex-plugin/plugin.json")
    }

    assert catalog["name"] == "woodor"
    assert catalog["interface"]["displayName"] == "Woodor"
    assert set(by_name) == native_plugins

    for name, entry in by_name.items():
        assert entry["source"]["source"] == "local"
        plugin_root = ROOT / entry["source"]["path"]
        manifest = _read_json(plugin_root / ".codex-plugin" / "plugin.json")
        assert plugin_root.name == name
        assert manifest["name"] == name
        assert entry["policy"] == {
            "installation": "AVAILABLE",
            "authentication": "ON_INSTALL",
        }
        assert entry["category"]


def test_imagent_is_the_only_session_management_skill_name():
    plugin_root = ROOT / "agent-meeting"
    manifest = _read_json(plugin_root / ".codex-plugin" / "plugin.json")
    skill = (plugin_root / "skills" / "imagent" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert not (plugin_root / "skills" / "meeting").exists()
    assert skill.startswith("---\nname: imagent\n")
    assert any("$imagent" in prompt for prompt in manifest["interface"]["defaultPrompt"])
    assert all("$meeting" not in prompt for prompt in manifest["interface"]["defaultPrompt"])


def test_ai_platform_manifests_declare_only_their_supported_components():
    plugin_root = ROOT / "agent-meeting"
    claude = _read_json(plugin_root / ".claude-plugin" / "plugin.json")
    codex = _read_json(plugin_root / ".codex-plugin" / "plugin.json")

    assert claude["skills"] == "./skills/"
    assert claude["hooks"] == "./claude-hooks/hooks.json"
    assert (plugin_root / claude["skills"]).is_dir()
    assert (plugin_root / claude["hooks"]).is_file()

    assert codex["skills"] == "./skills/"
    assert "hooks" not in codex
    assert (plugin_root / codex["skills"]).is_dir()
    assert not (plugin_root / "hooks" / "hooks.json").exists()
