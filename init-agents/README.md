# init-agents

为 Codex 和 Claude Code 项目安装三个固定的 project-local subagent profile，让主 agent 按任务性质选择调查、实现或规划模型。

## 模型矩阵

| Profile | Codex | Claude Code | 用途 |
|---|---|---|---|
| `explore` | `gpt-5.6-terra` / high | `claude-sonnet-5` / medium | 只读调查、定位、证据收集 |
| `rd` | `gpt-5.6-terra` / high | `claude-sonnet-5` / high | 边界明确的实现、修复、测试 |
| `planner` | `gpt-5.6-sol` / high | `claude-opus-5` / high | 架构取舍、复杂分析、独立审查 |

`planner` 默认使用 high。只有极少数真正困难、长链条且 high 仍不足的任务，才建议手动建立 xhigh 变体；不把 xhigh 作为日常默认。

## 安装

### Codex

```bash
codex plugin marketplace add woodor-ai/plugins
codex plugin add init-agents@woodor
```

在 Codex 中调用：

```text
$init-agents
```

### Claude Code

```text
/plugin marketplace add woodor-ai/plugins
/plugin install init-agents@woodor
```

在 Claude Code 中调用：

```text
/init-agents
```

## 生成结果

Codex：

```text
<project>/.codex/agents/
├── explore.toml
├── rd.toml
└── planner.toml
```

Claude Code：

```text
<project>/.claude/agents/
├── explore.md
├── rd.md
└── planner.md
```

profile 是项目级配置，不会修改 `~/.codex/agents/` 或 `~/.claude/agents/`。生成或更新后建议开启新会话，让宿主重新发现配置。

## 调度原则

- 简单搜索或单文件低风险修改由主 agent 直接做，避免派发成本高于任务本身。
- 只需要事实、位置和证据时使用 `explore`。
- 需要改文件，且目标和范围已经明确时使用 `rd`。
- 需要决定方向、跨系统推理、迁移设计或独立审查时使用 `planner`。
- 混合任务通常先调查，再由主 agent 收敛范围，最后实现。
- 不让多个 `rd` 同时修改同一文件；最终集成归主 agent。

建议主 agent 默认使用：

- Codex：`gpt-5.6-terra` / high，需要高影响规划时派 `gpt-5.6-sol` planner；
- Claude Code：`claude-sonnet-5` / high，需要高影响规划时派 `claude-opus-5` planner。

这样能把 Sol/Opus 用在真正需要深推理的步骤，而不是让整个会话持续承担最高成本和延迟。

## 冲突处理

在插件源码根目录手动运行时，初始化器先以只读模式检查目标：

```bash
python3 skills/init-agents/scripts/init_agents.py \
  --host codex \
  --mode check
```

目标文件分为 `missing`、`identical` 和 `different`。`different` 会输出统一 diff；apply 默认遇到冲突即停止，不会静默覆盖：

```bash
# 无冲突时创建缺失文件
python3 skills/init-agents/scripts/init_agents.py \
  --host codex \
  --mode apply

# 保留已有冲突文件，同时创建缺失文件
python3 skills/init-agents/scripts/init_agents.py \
  --host codex \
  --mode apply \
  --conflict skip

# 用户确认后替换冲突文件
python3 skills/init-agents/scripts/init_agents.py \
  --host codex \
  --mode apply \
  --conflict overwrite
```

不提供自动“合并”，因为 TOML 和 Markdown frontmatter 的字段级合并语义不明确，容易生成重复或失效配置。

## 插件与源码设计

本插件遵循仓库的双宿主边界：

```text
init-agents/
├── .claude-plugin/plugin.json
├── .codex-plugin/plugin.json
├── skills/init-agents/
│   ├── SKILL.md
│   ├── agents/openai.yaml
│   ├── scripts/init_agents.py
│   └── assets/
│       ├── claude/*.md
│       └── codex/*.toml
└── tests/test_init_agents.py
```

- 仓库根 `.claude-plugin/marketplace.json` 是 Claude Code 的插件目录；
- 仓库根 `.agents/plugins/marketplace.json` 是 Codex 的插件目录；
- 两个 plugin manifest 分别描述各自宿主要加载的版本化资产；
- `assets/` 是 profile 唯一模板来源，`SKILL.md` 不再重复内嵌六份模板；
- `init_agents.py` 只向用户项目写入配置，采用原子替换并保护冲突；
- 本插件没有后台进程、共享 Python 包或用户数据，不需要 `codex/install.py`，也不进入 agent-meeting/mycodex 的共享主机运行时安装链。

历史决策背景见
[Codex Agent Profile 适配分析](../docs/archive/designs/init-agents-codex-agent-profile-analysis.zh-CN.md)。

## License

MIT — see [LICENSE](../LICENSE).
