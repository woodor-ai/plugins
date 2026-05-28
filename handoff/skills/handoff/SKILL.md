---
name: handoff
description: Write a session-end handoff card to `.claude/handoff-pending.md` so the next session auto-picks it up via the plugin's SessionStart hook. Use at end of a working session when there is in-flight state / pending decisions / a defined next step. The next session reads + archives the file automatically — the user does not need to re-explain context. Use when the user signals end-of-session ("handoff", "wrap up", "session over") or when you want to leave a session-boundary cue card for the next agent.
user-invocable: true
allowed-tools:
  - Read
  - Write
  - Bash(mkdir *)
  - Bash(ls *)
  - Bash(test *)
  - Bash(wc *)
  - Bash(date *)
---

# /handoff — Session 结束交接（auto-pickup）

写一份 session-boundary cue card 到固定路径 `.claude/handoff-pending.md`。下个 session 启动时，本 plugin 的 SessionStart hook 会自动读取 + 归档到 `docs/handoff/archive/handoff-<timestamp>.md`，**用户不用手动指路**。

## 配套架构（plugin 自带，无需用户配置）

- **写入**：本 skill（session 结束时主 agent 调）
- **读取 + 归档**：plugin 的 `hooks/hooks.json` 注册的 SessionStart hook（startup / resume / clear / compact 四种触发都覆盖）→ `bin/handoff-pickup.sh` 检测 + atomic mv
- **位置**：`<project>/.claude/handoff-pending.md`（单文件，last-write-wins，无并发预期）
- **归档**：`<project>/docs/handoff/archive/handoff-<YYYY-MM-DD-HHMMSS>.md`

## 内容硬约束

**≤50 行**，**只允许 3 段**，**禁止 restate 项目状态文档已有内容**：

```markdown
# Handoff <YYYY-MM-DD HH:MM>

## 1. 当前阶段 in-flight
<本 session 没干完的事 1-3 条；用 "见 <state-doc> §X.Y" 指针引现状，不重写细节；用 "见 commit <hash>" 指代码，不复制 diff>

## 2. Pending 用户决定
<具体 N 选 M 的待决方向，每条 ≤2 行——例 "(a) X / (b) Y / (c) Z"；无 pending 写 "无"，禁止留空>

## 3. 新会话接手第一步
<actionable 级别：具体命令 / 具体读哪段 / 具体派 subagent 干什么；不许"自己看 git log"敷衍>
```

## 执行步骤

1. **Read** 当前 cwd 的项目状态 doc（如 `docs/current-state.md` / `README.md` / `CLAUDE.md` 等，按项目惯例）的前两节确认 in-flight context
2. 从对话上下文整理 3 段内容（每段都必填，无内容写"无"，**禁止超 50 行**——超就重写更精炼）
3. **Write** 到 `<cwd>/.claude/handoff-pending.md`（如父目录不存在 `mkdir -p .claude/`）
4. `wc -l` 验证 ≤50 行；超出 → 报错让用户决定是否压缩
5. 报一行确认：文件路径 + 行数 + 3 段标题（让用户看到内容大纲）

## 禁止

- ❌ 超 50 行
- ❌ 复制项目状态文档已有内容（root cause 长段 / changed files 全表 / commands dump / 历史对话）→ 用指针代替
- ❌ "等下一步指示"类空话——pending 段必须列具体选项
- ❌ "看 git log 自己看"敷衍——第 3 段必须 actionable
- ❌ 覆盖前不 stat 检查（避免误覆盖未读 handoff）：写前如 `.claude/handoff-pending.md` 已存在，先 Read 给用户看现有内容 + 确认覆盖
