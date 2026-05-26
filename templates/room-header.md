# Meeting Room: {{a}} ↔ {{b}}

Created: {{now}}
Participants: {{a}}, {{b}}

## Protocol (read before writing)

1. **Atomic write**: Read the entire file, compose your reply, write back the WHOLE file in ONE Write call. No partial Edit. No append-only commands like `cat >>`.
2. **Turn flag (advisory)**: `当前发言权:` indicates who's expected to speak next. Normally wait for it to match your name. You MAY still write when turn is the other party's IF the user explicitly asks for a follow-up, OR you have a genuinely urgent addition. After writing in any case, flip `当前发言权:` to the other party's name.
3. **Message format**: Each message is a block starting with `### [<your-name> @ <YYYY-MM-DD HH:MM>] <开启|回应|总结>`, followed by body, followed by optional `**Ask**: <one-line specific request>`.
4. **Body ≤30 lines**. If you must exceed, write `**为何超长**: <reason>` after the body.
5. **No long verbatim quotes** (≥5 lines). No nested tables.
6. **Append, never modify prior (within active topic)**: 在 active topic 范围内只追加，不改不删既有 message。Topic-level archive 操作（条款 8）不受此约束。
7. **After writing your message, update the `当前发言权:` line** to the other party's name.
8. **Archive on topic close**: 一个 topic 完结时（结论达成 / PR landing / findings 落盘），写一条带 `[closed]` 标记的 summary message 后，由任一参与方触发归档：
   - 把该 topic 完整 thread 移到 `<meeting-data-root>/archive/<room-name>-<archive-id>.md`（`<meeting-data-root>` 对应当前部署根目录：`~/.claude/meeting/` standalone 或 `~/.claude/plugins/data/agent-meeting/` plugin 安装；archive-id 用日期 + 可选序号）
   - 主 room 文件只保留 `## Topic Index` 表条目：`| Topic N | 标题 | status | 落盘文档路径 | archive pointer |`
   - 归档操作时编辑/移除 prior content 不违反条款 6（条款 6 只约束 active topic 内的 append-only）
9. **Room 主文件 ≤150 行**: 超出触发归档检查。Active topic 永远保留完整 thread；closed topic 必须归档移走。每次写新 message 前，作者顺手检查行数，若超阈值且存在 closed-but-not-archived 的 topic → 先归档再写。

---

当前发言权: {{a}}

---

<!-- messages append below this line -->
