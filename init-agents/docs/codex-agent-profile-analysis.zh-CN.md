# init-agents：Agent Profile 与 Codex 适配分析

> 分析日期：2026-07-28
> 本地核对版本：codex-cli 0.145.0

## 一、结论摘要

`explore`、`rd`、`planner` 三类 profile 的基础分工是合理的：

- `explore` 负责低成本、只读的信息收集；
- `rd` 负责边界明确的编码和验证；
- `planner` 负责高影响决策、跨系统分析和方案评审。

原 0.1.7 实现存在以下关键问题：

1. 调度规则只区分任务类型，没有说明何时根本不应派 subagent。
2. `rd` 和 `planner` 含有不适用于任意项目的高风险假设，例如默认项目没有外部用户、可以直接破坏旧 API。
3. `planner` 没有明确覆盖独立代码审查、风险评估等常见只读高推理任务。
4. Codex 部分关于“不能按名调度自定义 agent”的说明已经过时。
5. `init-agents` 缺少 Codex 插件清单和 marketplace 注册，不能作为 Codex 原生插件正常安装。
6. Codex 模型和 Windows sandbox 说明需要根据当前文档更新。

0.2.0 方案保留三档体系，重新收紧职责边界、把独立审查纳入 `planner`，并完成 Codex 原生插件包装。本文后续同时记录判断依据和已采用的实现。

## 二、三个 Agent Profile 的使用场景

### 1. explore

#### 适合使用

- 定位类问题：某个函数、配置、字段或命令在哪里定义；
- 追踪代码执行链、依赖关系和数据流；
- 搜索符合条件的文件、符号和历史变更；
- 阅读日志、测试输出和公开文档；
- 为主 agent、`rd` 或 `planner` 收集可引用的事实。

#### 不适合使用

- 修改代码、配置或文档；
- 决定架构方向；
- 输出未经验证的修复方案；
- 承担需要持续实现和测试的任务。

#### 建议修改

原模板禁止所有“dev commands”过于严格。新模板允许必要的非修改性诊断命令，例如查看测试列表、读取构建配置或运行不会改变项目状态的检查。

遇到歧义时，subagent 不应直接等待用户回答。更合适的行为是向主 agent 返回：

- 缺少什么信息；
- 已经确认了什么；
- 在哪些假设下可以继续。

另外，`sandbox_mode = "read-only"` 是重要护栏，但不应在文案中承诺绝对无法写入。Codex 会继承并重新应用父会话的实时权限和 sandbox override。

### 2. rd

#### 适合使用

- 实现已经确定范围的功能；
- 修复边界明确的 bug；
- 增加或更新测试；
- 完成局部重构；
- 运行与改动相关的测试、类型检查和 lint；
- 根据 planner 或主 agent 已确定的方向落地。

#### 不适合使用

- 决定跨模块架构；
- 未经授权扩展任务范围；
- 多个 agent 同时修改相同文件；
- 擅自改变公共 API、数据模型或兼容性策略；
- 顺手重构与任务无关的代码。

#### 建议修改

“设计必须完全决定”过于严格。实现过程中允许做局部、可逆、低影响的技术选择；以下情况才应停止并升级：

- 公共 API 或持久化数据格式变化；
- 跨子系统职责调整；
- 多个合理方向具有明显产品或维护成本差异；
- 需要扩大用户授权的操作范围；
- 实际任务明显超出原始边界。

应删除或改写以下通用规则：

- “内部工具没有外部用户”；
- “直接破坏旧字段、CLI 参数和 API”；
- “信任内部代码，不增加防御检查”。

这些规则只能由具体项目的 `AGENTS.md`、兼容性政策或用户指令决定，不能作为所有项目的默认值。

验证规则也应由“运行项目所有 test/typecheck/lint”改为“运行与改动风险相称的相关验证，并遵守项目说明”。无需强制创建日志文件；报告执行命令、结果和关键失败信息即可。

### 3. planner

#### 适合使用

- 非平凡功能的实施规划；
- 架构方向和技术取舍；
- 跨子系统复杂 bug 的根因分析；
- 大型重构的范围划分；
- 方案可行性、迁移成本和风险评估；
- 独立代码审查、安全风险和测试缺口分析。

#### 不适合使用

- 纯文件定位；
- 已经确定方案后的机械实现；
- 仅需单文件、低风险判断的小任务；
- 在缺少关键业务约束时假装能够确定唯一方案。

#### 建议修改

“必须明确选一个方向”应改为“在证据充分时给出明确建议”。如果结果依赖未提供的业务、兼容性或成本约束，应明确指出决定条件，而不是制造虚假确定性。

同样需要删除“默认没有外部用户或历史负担”的假设。

本项目确定只保留三个 profile，因此独立审查正式纳入 `planner`。未来只有在审查隔离成为高频、强约束需求时，才考虑单独增加只读 `reviewer`，当前不增加第四档。

## 三、建议的调度规则

### 第一步：判断是否值得派 subagent

以下任务通常由主 agent 直接完成：

- 一次搜索即可回答的问题；
- 单文件、低风险的小修改；
- 无法与主任务并行的短步骤；
- 派发和汇总成本大于任务本身的工作。

只有满足以下一项或多项时才考虑派发：

- 可以与主任务或其他任务并行；
- 任务边界清晰且上下文可以独立；
- 结果可以压缩大量阅读上下文；
- 独立视角能够降低判断偏差；
- 使用低成本模型有明显收益。

### 第二步：选择 profile

1. 只需要事实、位置和证据：`explore`。
2. 需要修改文件，范围和目标已经明确：`rd`。
3. 需要决定方向、处理跨系统复杂性或独立审查：`planner`。
4. 同一问题既需要调查又需要实现时，先 `explore`，主 agent 确认范围后再派 `rd`。
5. 不要让多个 `rd` 同时修改相同文件；最终集成和冲突处理由主 agent 负责。

### 第三步：禁止递归派发

如果要求三个 profile 都不能继续创建 subagent，Codex 模板不能只依赖提示词。建议在每个 agent TOML 中加入：

```toml
[agents]
enabled = false
```

项目级并发限制可以放在 `.codex/config.toml`：

```toml
[agents]
max_concurrent_threads_per_session = 4
```

## 四、Skill 本身需要调整的部分

### 1. 收紧触发条件

当前 skill 的 description 包含“starting work in a new project”，而 Codex 可以根据 description 隐式选择 skill。这可能导致普通新项目任务意外创建配置文件。

建议改成仅在以下情况触发：

- 用户明确要求初始化 agent profiles；
- 用户明确要求安装、重建或升级 `init-agents`；
- 用户显式调用 `$init-agents`。

### 2. 改进项目根目录判断

不应只通过 `.git`、`package.json`、`Cargo.toml` 或 `pyproject.toml` 判断根目录。优先使用：

```bash
git rev-parse --show-toplevel
```

非 Git 项目再回退到标记文件，并让用户确认目标目录。

### 3. 不提供未定义的“合并”

对已有 TOML 或 Markdown profile 执行“合并”缺少明确语义，容易产生重复字段、失效配置或混合版本。

建议：

- 先统一展示 diff；
- 提供保留或替换；
- 如果必须支持合并，使用确定性的字段级脚本并增加测试；
- 在生成文件中加入模板版本标记，支持后续升级。

### 4. 拆分模板和执行逻辑

原来六份模板全部内嵌在 `SKILL.md`，每次调用都要加载大量内容。

0.2.0 采用的结构：

```text
init-agents/
├── .codex-plugin/
│   └── plugin.json
├── skills/
│   └── init-agents/
│       ├── SKILL.md
│       ├── agents/
│       │   └── openai.yaml
│       ├── scripts/
│       │   └── init_agents.py
│       └── assets/
│           ├── claude/
│           │   ├── explore.md
│           │   ├── rd.md
│           │   └── planner.md
│           └── codex/
│               ├── explore.toml
│               ├── rd.toml
│               └── planner.toml
├── tests/
│   └── test_init_agents.py
└── docs/
```

脚本负责根目录识别、模板校验、diff、覆盖保护和原子写入；skill 只保留触发条件与调用流程。已有文件只提供保留或替换，不做语义不明确的自动合并。

## 五、三个 Profile 在 Codex 中的使用

### 1. Codex 原生能力

当前 Codex 官方支持：

- 用户级自定义 agent：`~/.codex/agents/*.toml`；
- 项目级自定义 agent：`.codex/agents/*.toml`；
- 通过 `name` 字段识别 agent；
- 在 agent 文件中设置 `model`、`model_reasoning_effort`、`sandbox_mode`、MCP 和 skill 配置。

每个文件至少需要：

```toml
name = "agent_name"
description = "When Codex should use this agent."
developer_instructions = """
Agent behavior.
"""
```

因此当前三份 Codex TOML 的基本格式成立。项目级配置应在可信项目中使用，生成或更新 profile 后建议开启新会话进行验证。

官方资料：

- [Codex Subagents](https://developers.openai.com/codex/multi-agent)
- [Codex Configuration Reference](https://developers.openai.com/codex/config-reference)

### 2. Codex 插件安装

0.2.0 增加 Codex 要求的：

```text
.codex-plugin/plugin.json
```

同时在仓库的以下目录注册：

```text
.agents/plugins/marketplace.json
```

Codex manifest 声明 `skills` 和 Codex 安装界面元数据，例如：

```json
{
  "name": "init-agents",
  "version": "0.2.0",
  "description": "Initialize project-level subagent profiles.",
  "skills": "./skills/"
}
```

安装方式：

```bash
codex plugin marketplace add woodor-ai/plugins
codex plugin add init-agents@woodor
```

然后在 Codex CLI 或 IDE 中显式调用：

```text
$init-agents
```

Codex skill 通常通过 `$skill-name` 或 `/skills` 选择；README 中的 `/init-agents` 是 Claude Code 风格，需要分别说明。

官方资料：

- [Codex Skills](https://developers.openai.com/codex/skills)
- [Codex Plugin Packaging](https://developers.openai.com/codex/plugins/build)

### 3. Agent 的实际调用

初始化完成并开启新会话后，可以直接要求 Codex 使用对应 agent：

```text
让 explore agent 定位认证流程，不修改文件，并返回路径和行号。
```

```text
让 rd agent 实现已经确定的缓存修复，只修改相关模块并运行对应测试。
```

```text
让 planner agent 评估这次数据模型调整，给出推荐方案、关键假设和风险。
```

主 agent 负责选择、派发、跟进和汇总，不应把最终集成责任交给 subagent。

### 4. 工具托管环境的限制

某些 ChatGPT 或工具托管的 Codex 会话只暴露通用 `spawn_agent`，没有暴露自定义 `agent_type`。这种情况下，`.codex/agents/*.toml` 可能无法按名选中。

临时方案是：

- 派发通用 subagent；
- 在任务 prompt 中内联对应 profile 的职责和限制；
- 使用工具接口实际支持的模型和 reasoning override。

这只是 profile 行为模拟，不等于加载了 `.codex/agents/*.toml`。

## 六、需要删除的过时 Codex 说明

原来三个 Codex 模板都写着：

> Current Codex versions cannot dispatch custom agents by name via spawn_agent.

该说法应删除。当前官方文档已经明确支持 standalone custom agents。

GitHub issue [openai/codex#14039](https://github.com/openai/codex/issues/14039) 讨论的是更细粒度的每次派发 model、provider 或 profile 覆盖能力，不等于自定义 agent 完全不能按名使用。

更准确的兼容性说明是：

> 原生 Codex CLI/IDE 支持项目级和用户级自定义 agent。部分工具托管表面可能不暴露自定义 agent selector；此时只能使用通用 subagent 加提示词覆盖。

## 七、模型建议

原 Codex 模型配置：

```text
explore: gpt-5.4-mini / low
rd:      gpt-5.4 / high
planner: gpt-5.5 / high
```

最终采用以下明确的默认矩阵：

```text
Codex
explore: gpt-5.6-terra / medium
rd:      gpt-5.6-terra / high
planner: gpt-5.6-sol   / high

Claude Code
explore: claude-sonnet-5 / medium
rd:      claude-sonnet-5 / high
planner: claude-opus-5   / high
```

Claude Code profile 使用官方字段 `effort`，不再使用旧模板里的 `reasoningEffort`。

`planner` 默认不设为 xhigh。xhigh 只用于极少数 high 明显不足、长链条且高影响的任务；如以后需要，可额外建立显式 deep 变体，而不是提高所有 planner 调用的成本和延迟。

模型是否可用仍取决于账号、工作区政策和具体宿主表面。

官方资料：

- [OpenAI Model Guidance](https://developers.openai.com/api/docs/guides/latest-model)

## 八、Windows Sandbox 说明

当前文档将 codex-cli 0.140.0 的实测结论写成了普遍规则，并固定推荐 `unelevated`。这容易随版本过时。

当前官方说明是：

- 优先使用 `elevated`；
- 当管理员配置、企业策略或环境兼容性阻止 elevated 时，再使用 `unelevated`；
- Windows sandbox 问题应作为版本和环境相关的 troubleshooting，而不是 `rd` profile 的固定前提。

建议保留 0.140.0 的实测记录，但明确标注为历史版本，并将当前操作指引链接到官方文档：

- [Codex Windows Sandbox](https://developers.openai.com/codex/windows)

## 九、实施结果

### 已落实

1. 添加 `.codex-plugin/plugin.json` 和 Codex marketplace 条目；
2. README 增加 Codex 安装、`$init-agents` 调用和双宿主源码边界；
3. 删除三份 TOML 中错误的 issue #14039 调度说明；
4. 删除“无外部用户”“默认可破坏兼容性”等危险假设；
5. 增加“不值得派 subagent”的判断；
6. 允许 `rd` 做局部、可逆的实现决策；
7. 将独立审查纳入 `planner`；
8. 使用 `[agents] enabled = false` 禁止三个 profile 递归派发；
9. 将模板移动到 assets，并增加确定性初始化脚本；
10. 增加 TOML、模型矩阵、冲突保护和项目根目录测试。

### 保留为版本相关说明

Windows sandbox 的操作方式可能随 Codex 版本变化。模板只声明期望的 `sandbox_mode`；具体 Windows 故障排查以当前官方文档和实际运行环境为准，不把旧版本 workaround 写成 profile 的固定前提。

## 十、主 Agent 模型建议

### Codex

主 agent 默认使用 `gpt-5.6-terra / high`，需要高影响方向判断时派 `gpt-5.6-sol / high` 的 `planner`。

理由：

- 主 agent 承担日常编排、上下文整合和大量中等复杂度实现，Terra 更适合持续运行；
- 真正需要 Sol 的通常只是少数规划、复杂根因和独立审查步骤；
- planner 的结果回到主 agent 后，再由 Terra 主 agent 负责落地、验证和集成；
- 如果整个任务从开始到结束都是高风险架构或极难推理，可以直接把主 agent 提升到 Sol，而不是机械坚持 Terra。

因此推荐默认策略是“Terra 主 agent + 按需 Sol planner”，而不是所有会话固定使用 Sol。

### Claude Code

对应策略是 `claude-sonnet-5 / high` 作为主 agent，需要高影响规划时派 `claude-opus-5 / high` 的 `planner`。

## 十一、仓库级插件规范适配

本方案依据仓库文档 `agent-meeting/docs/plugins-codex-architecture-audit.md` 区分四层：

1. Claude marketplace：根 `.claude-plugin/marketplace.json`；
2. Codex marketplace：根 `.agents/plugins/marketplace.json`；
3. 两个宿主各自的版本化插件缓存；
4. 只有需要长期进程、共享命令或 Python 包的产品才有共享主机运行时。

`init-agents` 只包含 manifest、skill、profile 模板和初始化脚本。脚本从只读插件资产向用户明确指定的项目写入配置，但它没有：

- 后台进程或公开系统命令；
- 共享 Python 包；
- PID、日志、数据库或其他用户运行态；
- SessionStart 安装职责。

因此：

- 必须同时有 `.claude-plugin/plugin.json` 和 `.codex-plugin/plugin.json`；
- 必须分别进入两份 marketplace；
- 不需要 `codex/install.py`；
- 不应被旧的 `install-codex.py` 复制到 `~/.codex/plugins/<name>`；
- Codex 原生插件管理器安装其只读版本资产；
- profile 仅在用户显式调用 skill 时生成到具体项目。
