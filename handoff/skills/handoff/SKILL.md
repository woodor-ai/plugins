---
name: handoff
description: Write a session-end handoff card so the next session auto-picks it up via the plugin's SessionStart hook. Use `.codex/handoff-pending.md` in Codex and `.claude/handoff-pending.md` in Claude Code.
allowed-tools:
  - Read
  - Write
  - Bash(mkdir *)
  - Bash(pwd *)
  - Bash(test *)
  - Bash(wc *)
  - Bash(date *)
---

# /handoff — Session 结束交接（auto-pickup）

写一份只记录本 session 增量的交接卡。**Codex** 使用 `.codex/handoff-pending.md`；**Claude Code** 使用 `.claude/handoff-pending.md`。下个 session 启动时，本 plugin 的 SessionStart hook 会自动读取、归档并注入卡片，用户不用重新说明上下文。

交接不是项目盘点。默认以当前对话为事实来源；只有当前对话无法确定准确断点或下一步时，才读取能补齐该事实的最少项目内容。不要例行扫描 README、状态文档、Git 历史、diff、roadmap 或架构文档。

## 配套架构

- **写入**：本 skill（session 结束时主 agent 调）
- **读取 + 归档**：`hooks/hooks.json` 注册的 SessionStart hook（startup / resume / clear / compact）调用 `bin/handoff-pickup.py`；脚本用 atomic rename 领取卡片并作为 `additionalContext` 注入
- **位置**：`<cwd>/.codex/handoff-pending.md`（Codex）或 `<cwd>/.claude/handoff-pending.md`（Claude Code）
- **归档**：`<cwd>/docs/handoff/archive/handoff-<YYYY-MM-DD-HHMMSS>.md`
- **接手要求**：新会话把第 3 段的每条动作加入本 session 待办；写“无”时不创建待办

## 内容硬约束

**≤30 行，只允许以下 3 段**：

```markdown
# Handoff <YYYY-MM-DD HH:MM PDT/PST>

## 1. 当前断点
<尚未完成的工作 1-3 条；优先写当前文件、命令、commit 或状态文档指针，不复制 diff 或项目背景；无写“无”>

## 2. Pending 用户决定
<只写必须由用户选择或外部事件解除的阻塞；无写“无”>

## 3. 下一步与遗留待办
- <第一条必须是新会话立即执行的具体动作>
- <其余未完成动作，每条一行；不重复第 2 段的决策项；无动作写“无”>
```

## 执行步骤

1. 开始整理前，用尽量一次工具调用取得当前 `pwd` 和 `date "+%Y-%m-%d %H:%M %Z"`，并检查当前宿主的 pending 文件是否存在。
2. 如 pending 文件已存在，先 Read 并展示给用户，确认覆盖后再继续；不得静默覆盖。
3. 直接从当前对话整理三段。上下文不足时，只读取能回答缺失事实的最小范围；禁止为了“完整”做仓库巡检。
4. 写到当前 shell 实际工作目录：Codex 为 `.codex/handoff-pending.md`，Claude Code 为 `.claude/handoff-pending.md`。需要时先创建宿主目录；不要改写为 Git 仓根目录或 `CLAUDE_PROJECT_DIR`。
5. 用 `wc -l` 验证不超过 30 行；超出时立即压缩后重写，不要把压缩决定抛给用户。
6. 只报一行确认：文件路径、行数、三个段落标题。

## 禁止

- 超过 30 行或增加第四段
- 默认读取项目状态文档、README、roadmap、架构文档、Git 历史或 diff
- 重写项目背景、长 root cause、changed-files 全表、commands dump 或历史对话
- 把同一事项同时写进多个段落
- 用“等下一步指示”“自己看 git log”之类空话
- 写完交接卡后向 AMBridge 或 agent-meeting 发送 restart/clear 控制标记

## 调用边界

本 skill 只在用户显式调用，或受信任的本机会话生命周期控制器明确请求时执行。不得根据会话时长、对话轮数、token 利用率或任务边界自行触发。
