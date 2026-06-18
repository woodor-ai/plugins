# agent-meeting × Codex TUI 桥接调研

文档日期：2026-06-17 20:52 PDT（初版）
状态：**路径 C（app-server）三命门实测坐实，真 schema + 接入坑已归档。实现待 amb-wic 桌面 APP 节奏。**

---

## 1. 背景

### 1.1 AMBridge 桌面 APP 的 codex 接入决策

AMBridge 桌面 APP 决定 codex 走**可见 TUI 会话**——iTerm2 起 codex、用户自装自更新——放弃以下三条路：

- openai_codex SDK（80MB，打包进 APP）
- 无头 app-server + WakeLoop 轮询
- 内嵌 codex 进程（自管生命周期）

### 1.2 agent-meeting 的桥接问题

agent-meeting 在 Claude 侧靠三件套桥接外部消息：

1. **meeting 插件**：Claude Code 本地插件，监听 IPC socket
2. **Monitor 工具**：Claude Code 原生工具，常驻 poll daemon
3. **SessionStart hook**：有新消息时唤醒主 agent

这套机制在 Codex 侧没有原生对等物——Codex 无 Monitor 工具、hook 在纯 idle 时不 fire，拦不到来电。

### 1.3 旧方案为何不成立

旧"另起 daemon 注入消息"方案依赖 codex SDK 在进程内注入。随 SDK 放弃（amb-wic 已删 openai-codex 80MB 依赖），这条路结构性消失，不是细节 bug。

---

## 2. 候选路径（planner 评估）

### 路径 A — hook poll + 注入（已弃）

利用 codex 用户级/项目级 `hooks.json` 的 `PostToolUse` hook：外部 daemon 注入、hook 轮询来电。

**死结**：codex 纯 idle 时不触发任何 hook event——收不到来电。结构性无解，不是调参能绕开的。

### 路径 B — tmux send-keys（兜底）

外部 daemon 对 iTerm2/tmux 里的 codex TUI 进程做 `send-keys` 注入键入。

已知坑：
- idle 判定脆——不知道 codex 是否在等输入还是执行中
- 抓屏读回复不稳——依赖屏幕字符输出格式
- 特殊键/换行可能污染 TUI 状态

留作路径 C 证伪后的兜底，**不是主推**。

### 路径 C — app-server daemon（主推，已坐实）

codex 二进制自带常驻 `app-server` daemon，JSON-RPC over WebSocket，**thread 会话模型**：

- 一个 thread = 一个持久会话，多客户端可连同一 thread 共享上下文
- 用户 TUI 连接方式：`codex --remote <ADDR>`（连上已有 app-server）
- 外部 daemon 连接方式：建 WS 连接 → `initialize` → `thread/resume` 加入目标 thread → `turn/start` 注入来电

**这是 Claude 侧 Monitor 工具的 codex 同构版**——Monitor 常驻 poll socket、有消息唤醒主 agent；app-server 是 codex 进程暴露的相同模式 socket。

关键点：放弃的只是"无头 SDK 用法"，**app-server 是用户自装 codex 自带的**，不需要重新打包任何东西。

---

## 3. 路径 C 实测结论

实测环境：codex 0.134.0，隔离 `CODEX_HOME`，rd subagent 实测，三命门均有实证。

### 3.1 C1 共享可见性 — ✅ PASS（实测）

Client-B 用 `thread/resume` 连同一 `threadId` 后发 `turn/start` 注入，Client-A 同屏实时收到：

- `turn/started`
- `item/completed`
- `turn/completed`

无延迟，无冲突。共享上下文机制有效。

### 3.2 C2 空闲唤醒 — ✅ PASS（实测）

空闲线程外部直接 `turn/start`，**成功起新 turn**，无报错（无"已有 turn 占用"、无"无活动会话"类错误）。idle 状态可接收来电。

### 3.3 C4 回复回灌 — ✅ PASS（机制实测，一处推断）

模型回复在 `item/completed` 事件的 `params.item.content[].text`，按 `threadId`/`turnId` 过滤提取。

**推断标注**：assistantMessage 确切 item 格式**未 100% 实测**——测试环境模型报 `model_not_found`，没生成真实回复。机制本身已确认，assistant 文本字段格式推断与 userMessage 结构一致。→ **真接入时需复验此推断**。

---

## 4. 真实 JSON-RPC Schema

来源：codex `app-server generate-json-schema` 产出（实测），非文档推断。文档与实际有出入，以下为真实方法名。

| 用途 | 真实方法名 | 必填参数 |
|---|---|---|
| 建新线程 | `thread/start` | 无必填（可选 `cwd`/`model`/`approvalPolicy`/`sandbox`） |
| 恢复已有线程 | `thread/resume` | `threadId` |
| 开一轮 | `turn/start` | `threadId`、`input`（数组，元素 `{type:"text",text:"..."}`) |
| 打断当前轮 | `turn/steer` | `threadId`、`expectedTurnId`（须匹配活跃 turn）、`input` |
| 建连 | `initialize` | `clientInfo`（驼峰）、`capabilities`（可 null） |

响应结构：`result.thread.id` / `result.turn.id`（**不是** `result.threadId` / `result.turnId`，字段名不同）。

有效子命令（实测确认）：

- `app-server [daemon|proxy|generate-json-schema|generate-ts]`
- `remote-control [start|stop]`
- 主命令：`codex --remote <ADDR>`

---

## 5. 两个接入坑（文档未写，实测踩出）

### 坑 1 — 传输层是 WebSocket，不是裸 TCP

`app-server --listen unix://SOCK` 起的是 **WebSocket 服务端**，需要 HTTP/1.1 Upgrade 握手。直接 read/write unix socket 永远 0 字节——连接建立了但协议层没走通。

接入方需用 WS 客户端库（Python `websockets`、Node `ws` 等），不能用裸 socket 读写。

### 坑 2 — 外部连接必须 `thread/resume` 才收得到推送

光完成 `initialize` 握手**不够**——外部客户端在 `initialize` 后不会自动订阅任何 thread。必须显式 `thread/resume` 加入目标 thread，之后该 thread 上的事件推送才会流到这个连接。

---

## 6. 待验 / 风险

| 项目 | 状态 | 说明 |
|---|---|---|
| assistantMessage item 格式 | **推断，待验** | §3.3 C4 推断点，真接入时复验 `item/completed` 的 assistant 文本字段 |
| 跨版本稳定性 | 风险 | `app-server`/`--remote` 标 experimental；用户自更新 codex 可能引入 schema 漂移 |
| Windows 支持 | **完全未测** | unix socket（`unix://SOCK`）在 Windows codex 上是否支持未知；Windows 可能只剩 `ws://` TCP 地址形式 |
| 多会话寻址 | **待验** | 一台机起多个 codex 会话时，外部 daemon 怎么把"agent-meeting 这个房间"映射到正确 `threadId`；目前没有自动发现机制 |

---

## 7. 现状与优先级

- **amb-wic 已降优先级**：AMBridge 桌面 APP 先用 Claude 出货，codex 入口先 stub 暂不支持（已删 openai-codex 80MB 依赖）。
- **agent-meeting codex 桥接实现等 amb-wic 桌面 APP 节奏**，不提前开工。
- 本文档是路径 C 坐实后的调研归档，供日后实现时直接参照，免重复踩坑。

---

## 8. 接手第一步（日后实现时）

1. 读 §5 两个坑，先用 `websockets`（Python）或 `ws`（Node）写连接验证脚本，确认 WS 握手通。
2. 跑 `thread/resume` + `turn/start` 注入一条真实 user 消息，观察 `item/completed` 回来的 assistant 文本字段，复验 §3.3 C4 推断。
3. 确认 `result.thread.id` 字段名（§4 响应结构），不要用文档里的 `result.threadId`。
4. Windows 接入需单独测 `unix://` 是否可用，备选 `ws://127.0.0.1:PORT` TCP 形式。
