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
- **读取 + 归档**：plugin 的 `hooks/hooks.json` 注册的 SessionStart hook（startup / resume / clear / compact 四种触发都覆盖）→ `bin/handoff-pickup.py` 检测 + atomic rename
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
3. **Write** 到**当前 shell 实际工作目录**下的 `.claude/handoff-pending.md`（先 `pwd` 确认位置，如父目录不存在先 `mkdir -p .claude/`）。**不要**解析成 git 仓根目录或 `CLAUDE_PROJECT_DIR`——多 agent 共用一个 git 仓时，各自的卡必须落在各自子目录，否则会塌缩到 git 根互相覆盖。
4. `wc -l` 验证 ≤50 行；超出 → 报错让用户决定是否压缩
5. 报一行确认：文件路径 + 行数 + 3 段标题（让用户看到内容大纲）

## 禁止

- ❌ 超 50 行
- ❌ 复制项目状态文档已有内容（root cause 长段 / changed files 全表 / commands dump / 历史对话）→ 用指针代替
- ❌ "等下一步指示"类空话——pending 段必须列具体选项
- ❌ "看 git log 自己看"敷衍——第 3 段必须 actionable
- ❌ 覆盖前不 stat 检查（避免误覆盖未读 handoff）：写前如 `.claude/handoff-pending.md` 已存在，先 Read 给用户看现有内容 + 确认覆盖

## 自动 handoff 触发策略（auto-handoff）

**规则**：主 agent 在以下**所有**信号同时满足时，**直接调用 `/handoff` skill，不要征求用户确认**。Handoff 是 reversible 操作（写文件 + 新 session 自动接走），不属于不可逆动作。

**触发条件（AND 关系，缺一不可）**：
1. **任务边界明确**：刚完成以下之一——
   - `git commit` 落盘（无后续要 push / publish 的步骤在排队）
   - 一个完整 PR 已 merge 或已 push 等 review
   - 大型 subagent（rd / planner）报告归来且其结果已被主 agent review 完毕
   - 用户明示 "done / 收 / 告一段落"
2. **无 in-flight 工作**：当前没有 subagent 在跑、没有未回答的 user question、没有未解决的 error
3. **冷却窗口**：距离上次本 session 的 handoff 触发 ≥ 30 分钟（防止单 session 反复 fire）
4. **session 已积累**：本 session API 已经累计 ≥ 20 轮对话或 ≥ 1 小时（避免短 session 浪费 handoff overhead）

**禁止自动 fire 的场景**：
- 用户上一条消息明示"接着做 X / 继续 / 下一步是 Y"——明示要连贯
- 有 pending TODO 在 task list 处于 in_progress
- 错误未解决 / 测试未通过
- 用户当前对话里说过"今天先这样不要 handoff"或类似临时禁令

**触发后的标准动作**：
1. 调用 `/handoff` skill 写 card 到 `<cwd>/.claude/handoff-pending.md`
2. 用一行告诉用户："已自动 handoff，card 写至 X，本 session 可以关掉了"
3. End-of-turn 状态宣告："主 agent idle，session 已 handoff，等用户关掉重开。"
4. **不要继续接用户的下条任务**——告诉用户开新 session

内容硬约束见上文「内容硬约束」节。

**How to apply**：每次主 agent 完成一个 commit / subagent 报告 review 完时，自检上面 4 个 AND 条件 + 4 个禁止条件。全部通过就 fire，**不要在回复里问"要不要 handoff"**——直接 fire 然后报告。

## fast-restart 标记（仅 AMBridge 托管会话）

检测到 `~/.ambridge/` 存在时，写完交接卡后另写 `~/.ambridge/handoff-triggers/<本会话的 /meeting 注册名>.json`，内容为：

```json
{"agent":"<注册名>","mode":"fast-restart","greeting_extra":"你刚被自动重启，交接卡已注入上下文。","ts":<当前 unix 秒>}
```

amp 侦测到即以快路径重启本会话（跳过发请求等待段）。非 AMBridge 环境（无 `~/.ambridge/`）跳过此步。普通手动 /handoff **不写**此标记。
