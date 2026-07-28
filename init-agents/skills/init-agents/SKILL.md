---
name: init-agents
description: Initialize, rebuild, or update the project-local explore, rd, and planner subagent profiles for Codex or Claude Code. Use only when the user explicitly asks to set up these agent profiles or explicitly invokes this skill; do not run merely because work starts in a new project.
---

# Init Agents

为当前项目安装三类固定 subagent profile：

- `explore`：只读调查、定位和证据收集；
- `rd`：边界明确的实现、修复和验证；
- `planner`：架构取舍、复杂根因分析、独立审查和风险评估。

简单搜索、单文件低风险修改等短任务由主 agent 直接完成；只有当任务边界清晰、可以并行、能显著压缩上下文，或需要独立视角时才派 subagent。

## 执行

1. 判断当前宿主：
   - Codex：`--host codex`
   - Claude Code：`--host claude`
2. 从当前 `SKILL.md` 的绝对位置解析同目录下的 `scripts/init_agents.py`。命令的 cwd 保持在目标项目中；不要相对目标项目查找脚本。
3. 先运行只读检查：

   ```bash
   python3 "/absolute/skill/path/scripts/init_agents.py" --host <host> --mode check
   ```

4. 如果目标文件均为 `missing` 或 `identical`，执行：

   ```bash
   python3 "/absolute/skill/path/scripts/init_agents.py" --host <host> --mode apply
   ```

5. 如果存在 `different`：
   - 向用户展示脚本输出的统一 diff；
   - 一次性询问全部冲突采用“保留”还是“替换”；
   - 保留：`--conflict skip`
   - 替换：`--conflict overwrite`
   - 不提供含义不明确的自动“合并”。
6. 报告 created、unchanged、skipped、overwritten 的文件。

脚本默认优先使用 Git 顶层目录；非 Git 项目需存在常见项目标记。用户明确确认其他目录后，才可加 `--allow-unrecognized-root`。

## 安全边界

- 只写项目内 `.codex/agents/` 或 `.claude/agents/`，不修改用户全局配置。
- 不静默覆盖已有 profile。
- 生成后提示用户开启新会话，使宿主重新发现 profile。
- 模板在 `assets/codex/` 和 `assets/claude/`；不要把六份模板重新内嵌到本文件。

## 调度原则

- 只需事实、位置和证据：`explore`。
- 需要改文件且目标、范围已明确：`rd`。
- 需要决定方向、跨系统推理或独立审查：`planner`。
- 混合任务通常先调查、再由主 agent 收敛范围、最后实现。
- 不让多个 `rd` 同时修改相同文件。
- 三个 profile 都不得继续创建 subagent；升级和最终集成回到主 agent。
