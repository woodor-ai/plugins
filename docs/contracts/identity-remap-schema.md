# 身份重映射表格式规范 v1

文档日期：2026-07-22 07:27 PDT
状态：提案，待 amb 确认
所属方案：`docs/contracts/0.10.0-composite-key-identity.md` 阶段 0.2

## 1. 这张表是什么

会话身份的权威形式是复合键 `(name, project)`。历史上 `project` 曾由 cwd
目录名推导，同一真实身份在库里因不同机器/worktree/文档误写而裂成多个复合
键（漂移）。这张表把「旧复合键」显式映射到「权威（canonical）复合键」，是
存量收敛的唯一真相来源。

两个消费方：

- **本仓迁移脚本**：按这张表改写中央库四张表 `messages` / `sessions` /
  `read_cursors` / `group_members`，把漂移键折叠进权威键。
- **AMBridge 手机端**：按这张表迁移本地存储 `state.agentsData` 的按键分
  桶数据，把旧桶键迁到新桶键。

两侧必须读**同一份文件**，否则两端归一结果不一致，会重新制造裂痕。

## 2. 硬约束

1. **映射必须是函数**——`mappings[].from` 在全表内唯一，每一项恰好对应
   一个 `to`。格式层面不存在「一对多」的表达方式。
2. **恒等映射不入表**——权威键自身不出现在任何 `from` 里；`canonical`
   段单独枚举权威键全集，不依赖从 `mappings` 反推。
3. **闭包已求值**——`to` 必须是 `canonical` 成员，不允许链式（`A→B` 且
   `B→C`）。传递闭包由生成器在产出前求好，消费方不需要（也不允许）自
   己再解一次链。
4. **一对多下沉到行级**——若某个旧键的行确实分属两个真实身份，不得在
   `mappings` 里表达（那会破坏约束 1）。只能在 `row_overrides` 按行 id
   显式枚举例外；该旧键在 `mappings` 里仍然有唯一默认 `to`，未被
   override 命中的行走默认。键级永远无歧义。
5. **人工判定必须留痕**——`basis="manual"` 的条目必须带 `note` 说明判
   定依据，不允许空 note。
6. **`deletions` 段只允许收纳消息端点计数为 0 的键**——只要该键承载过
   任何一条消息（作为 sender 或 recipient 出现过），就必须走
   `mappings`，不得出现在 `deletions`。

## 3. 文件格式

```json
{
  "schema": "agent-meeting/identity-remap@1",
  "generated_at": "2026-07-22 07:34 PDT",
  "generator": "agent-meeting/migrations/0.10.0-identity-remap.py",
  "source_db": "/Users/tommyclaw/.agent-meeting/db/rooms.db",
  "canonical": [
    { "name": "Tommy", "project": "*" },
    { "name": "atlas", "project": "Atlas" }
  ],
  "mappings": [
    {
      "from": { "name": "Tommy", "project": "AMBridge" },
      "to": { "name": "Tommy", "project": "*" },
      "basis": "manual",
      "rule": null,
      "note": "Tommy 是人类用户经 AMBridge 中继，权威身份是全局 *，AMBridge 是中继方的项目名被误写为发件人项目",
      "affected": { "messages": 11, "sessions": 0, "read_cursors": 1, "group_members": 0 }
    },
    {
      "from": { "name": "atlas", "project": "OMI" },
      "to": { "name": "atlas", "project": "Atlas" },
      "basis": "auto",
      "rule": "path-derived",
      "note": null,
      "affected": { "messages": 0, "sessions": 0, "read_cursors": 1, "group_members": 0 }
    }
  ],
  "deletions": [
    {
      "key": { "name": "codex-Tommys-Laptop", "project": "~" },
      "note": "无消息承载，会话早已离线，project 值全为路径派生且无法追认真实项目，直接删除而非归并（归并会把三个可能不同的真实项目错误合成一个）",
      "affected": { "messages": 0, "sessions": 1, "read_cursors": 1, "group_members": 0 }
    }
  ],
  "row_overrides": []
}
```

字段语义：

| 字段 | 说明 |
|---|---|
| `schema` | 固定值 `agent-meeting/identity-remap@1`。消费方**必须**校验此字段，不认识（版本不匹配或缺失）就整表拒绝消费，不得猜测降级读取。 |
| `generated_at` | 生成本文件时的本机 wall clock，格式 `YYYY-MM-DD HH:MM PDT`（或 `PST`）。 |
| `generator` | 产出本文件的脚本路径。 |
| `source_db` | 产出本文件所依据的库文件路径。 |
| `canonical` | 权威复合键全集，元素为 `{name, project}`。定义是「收敛后仍然合法存在的身份全集」，包含会话已结束但仍承载消息的历史身份——判定标准是 `project` 值本身是否合格（不是路径/用户名派生的脏值），与该会话当前是否在线无关。「在线/存活」不是筛选条件。 |
| `mappings` | 数组，元素含 `from` / `to`（均为 `{name, project}`）、`basis`（`auto` \| `manual`）、`rule`（`auto` 时给 §4 规则名，`manual` 时为 `null`）、`note`（`manual` 必填字符串，`auto` 可为 `null`）、`affected`（四张表各自的受影响行数，仅供人工核对，消费方不应依赖它做迁移判断）。 |
| `deletions` | 数组，元素含 `key`（`{name, project}`）、`note`、`affected`（结构同上）。语义：该键不承载任何消息、无迁移价值，对应的 `sessions` / `read_cursors` 行直接删除；amb 侧消费为删除对应本地桶（若存在）。 |
| `row_overrides` | 行级例外，默认空数组，见第 5 节。 |

复合键对象的 `name` / `project` 均为字符串，不做任何归一化（大小写、空
白原样保留）。`project` 可以是空串 `""`（历史脏值）或 `"*"`（全局身份）。

## 4. 自动推断规则名（`rule` 枚举）

`rule` 只在 `basis="auto"` 时出现，供审计使用，消费方不需要理解语义：

- `path-derived` — 旧 `project` 值是文件系统路径的片段或全路径（含 `/`
  或 `\`，或以 `~` 开头，或等于 cwd 末级目录名）。
- `doc-alias` — 旧 `project` 值是当时手头文档/任务名被误当项目名写入。
- `empty-value` — 旧 `project` 值为空串。
- `probe-residue` — 测试/探测残留身份。

## 5. 行级例外

当前 `row_overrides` 为空数组。格式预留：

```json
{
  "table": "messages",
  "row_id": "18422",
  "column": "sender_project",
  "from": "probeproj",
  "to": "test-project",
  "note": "该行实际来自另一独立脏身份，不走该旧键的默认映射"
}
```

`row_id` 对各表的含义：

- `messages`：`id` 列（单一整数主键）。
- `sessions` / `read_cursors` / `group_members`：主键各列以 `|` 连接，
  顺序同建表语句（如 `sessions` 为 `project|name`，`group_members` 为
  `group_project|group_name|member_project|member_name`）。

## 6. 键编码约定（给 amb）

AMBridge 本地桶键是 `project` 与 `name` 以 NUL 字符（`\0`）连接。本文件
**不预先拼好该字符串**，只给结构化 `{name, project}`，由 amb 按本地格
式自行拼接——拼接规则属 amb 存储细节，写进契约会把两边耦死。

amb 侧消费顺序：

1. 校验 `schema` 字段，不匹配（版本不对或缺失）整表拒绝。
2. 用 `mappings` 构造「旧桶键 → 新桶键」的 `Map`。
3. 逐桶迁移，**不得**用 `name → project` 单值 `Map`（同名跨项目时后
   项覆盖前项，会把两段历史合并并截断，造成串线 + 丢数据）。
4. 映射未命中的桶原样保留，不猜不丢。
5. 目标桶已存在时按消息时间戳合并去重，不做条数截断。
6. `deletions` 命中的桶直接删除。

## 7. 反迁移

映射表本身不携带反向信息，反向靠本仓行级来源日志表
`identity_remap_log`（迁移脚本写入），记录表名、行 id、列名、旧值、新
值、命中的映射。反迁移 = 按日志倒序把旧值写回。

amb 侧若需回退，用同一份映射表反向构造 `Map` 即可（映射是函数，在未发
生桶合并前提下反向唯一；发生合并的桶无法纯靠映射表拆回，需 amb 迁移前
自留桶快照——建议整体快照 `state.agentsData`，这点由 amb 侧保证）。

## 8. 交付方式

本仓产出 JSON 后通过 meeting 发给 amb，同时提交进 `docs/contracts/` 留
档。格式变更时 `schema` 版本号递增，不做兼容（内部工具，无历史包袱）。

## 9. 生命周期与消费幂等

**表是一次性快照。** 产出一次即冻结，随阶段 0.2 一并交付消费方，之后不
再重新产出。理由：身份漂移是「项目身份由 cwd 目录名推导」这一机制的产
物；阶段 0.1 关闭该机制（无权威身份时注册直接拒绝、不降级）之后，每个
会话注册时申报的 `(name, project)` 从产生那一刻就是权威值，日常不再需
要事后纠偏。存量脏键是机制关闭前的历史遗留，一次收敛干净即终结。

**消费方不需要单一真相源之外的第二套迁移。** AMBridge 侧原有的、基于
当前活跃会话名单自主推断的本地桶迁移应当删除，只认本表。除了与本表职
责重叠外，那套机制本身有缺陷：它拿当前活跃会话去判定历史身份，而历史
身份根本不在活跃名单里，会被误判为孤儿；且名单随时变化，同一批桶在不
同时刻跑出的迁移结果不一致——这是不确定性来源，不构成兜底。

**不存在「客户端未滚版仍在产生漂移」的中间态。** 阶段 0.1 之后无权威
身份的客户端注册被拒绝而非降级，它连不上，而不是连上后写脏数据。

**重复消费天然幂等，消费方无需记录消费标记。** 由第 2 节硬约束 2（恒
等映射不入表）与硬约束 3（闭包已求值）共同保证：一次消费后所有 `from`
键在消费方本地均已不存在，第二次执行是纯 no-op。

**`canonical` 的含义是「本阶段的权威归并目标」，不等于「干净」。** 例
如 `cx-test@ft` 中的 `ft` 并非真实项目名，但该键承载真实消息、本阶段不
可删，故仍作为归并目标列入 canonical，整体留待方案阶段 4 统一清理。

**阶段 0.2 完成后，上位方案的验收门槛第 1 条尚不能完全达成。** `read_
cursors` 里的纯孤儿测试残留（无消息、无会话行，如 `vfy-*` / `askdemo` /
`tc21-probe` 等）不在本阶段处理范围——本表的 canonical 定义只扫描
`sessions` 与 `messages` 两端，刻意不并入这类纯孤儿键（并入等于把脏值
追认为权威）。因此上位方案 6 条验收门槛中的「全库无旧路径派生的
project 值残留」在阶段 0.2 完成后仍有残留，需待阶段 4 清理这批纯孤儿
残留后才完全达成，不要误以为 0.2 做完即过门槛。
