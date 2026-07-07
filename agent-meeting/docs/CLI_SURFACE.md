# agent-meeting CLI 命令与参数现状

最后更新：2026-06-13 14:01 CST · 对应版本 **0.8.0**

包装器 `~/.agent-meeting/bin/meeting` 转发到缓存里的 `0.8.0/bin/meeting`（Python，argparse 实现）。
顶层用法 `meeting <子命令> ...`，子命令必填（`required=True`）。共 14 个子命令。

> **0.8.0 变更**：① 斜杠面重组为「使用面 + 供给面」——daemon/token/telemetry 收进 `/meeting setup` 子命名空间，`controls` 并入 `/meeting list` 显示。② `--host` 从 register 专属扩展到所有会解析 control 的命令（send/read/show/turn/ring/delete/list），统一手动选址能力。

---

## 两个调用面：TUI 斜杠命令 vs Bash 裸 CLI

`meeting` 就是 `~/.agent-meeting/bin/meeting` 一个普通 CLI 二进制，**14 个子命令在技术上全都能被人在终端手敲，没有任何权限门禁**。所谓「暴露给用户」不是「能不能调」，而是从哪个面进：

- **TUI 斜杠命令**：人在 Claude Code 对话框里敲 `/meeting ...`，由 skill 的 `SKILL.md` dispatch 表路由。只暴露一个子集。
- **Bash 裸 CLI**：终端里直接敲 `meeting <子命令> ...`，14 个子命令全部可调。大多由 skill / monitor 在背后自动拼参数调，但人也随时能手敲（调试 / 救场逃生口）。

### A. TUI 斜杠命令（`/meeting …`）

skill 暴露的全部入口，分「使用面」和「供给面」两层：

| 斜杠命令                                        | 落到的 CLI              | 说明                                                       |
| ----------------------------------------------- | ----------------------- | ---------------------------------------------------------- |
| `/meeting`（空参）                              | （选择器 → `register`） | 弹 AskUserQuestion 名字选择器，选完注册                    |
| `/meeting <name>`                               | `register`              | 注册本会话为 `<name>`（worker）                            |
| `/meeting list`                                 | `list` + `controls`     | 会话列表 + control 节点**合并显示**（controls 已并入此处） |
| `/meeting delete <peer>`                        | `delete`                | 删本会话与 `<peer>` 的房间（需确认，显示 msg 数）          |
| `/meeting setup`                                | （打印用法）            | 列出下面三个 setup 子命令，不执行动作                      |
| `/meeting setup daemon [status\|stop\|restart]` | `daemon`                | 管理本机 control daemon（缺省 = 设本机为 control）         |
| `/meeting setup token [<value>\|clear]`         | `token`                 | host 设鉴权密钥 / client 存别人给的凭证                    |
| `/meeting setup telemetry on\|off\|status`      | `telemetry`             | 遥测开关                                                   |

保留字 `setup` / `list` / `delete` / `controls` / `daemon` / `telemetry` / `token` 不能当注册名——会被当成对应命令。

### B. Bash 裸 CLI（`meeting <子命令> …`，14 个全可调）

按「斜杠入口」和「设计上谁调」标注：

| CLI 子命令   | 斜杠入口                      | 设计上谁调                                  |
| ------------ | ----------------------------- | ------------------------------------------- |
| `list`       | ✅ `/meeting list`            | 人（只读高频）                              |
| `controls`   | 🟡 并入 list                  | 人（只读高频，无独立斜杠入口）              |
| `delete`     | ✅ `/meeting delete`          | 人（低频运维）                              |
| `daemon`     | ✅ `/meeting setup daemon`    | 人（低频供给）                              |
| `telemetry`  | ✅ `/meeting setup telemetry` | 人（低频供给）                              |
| `token`      | ✅ `/meeting setup token`     | 人（低频供给）                              |
| `register`   | 🟡 间接（`/meeting <name>`）  | 人间接 + monitor 启动自调                   |
| `send`       | ❌                            | skill（来电后自动发回应；`/talkto` 也走它） |
| `show`       | ❌                            | skill（读最近 20 条上下文）                 |
| `read`       | ❌                            | 脚本 / TSV 取数                             |
| `turn`       | ❌                            | skill / 人调试时查发言权                    |
| `ring`       | ❌                            | monitor 每 3s 轮询来电                      |
| `unregister` | ❌                            | monitor 退出时 atexit 自调                  |
| `init`       | ❌                            | `/meeting <name>` 注册流程里幂等建库        |

要点：
- **「❌ 无斜杠入口」不代表用户被禁**——人随时能开终端裸敲 `~/.agent-meeting/bin/meeting send ...`，能跑通。只是要手填 `self`/`peer`、自己管 turn 翻转，很别扭，所以日常没人这么干。
- send/read/show/turn/ring **预期由 skill/monitor 自动调**，但同时也是人**调试 / 救场的逃生口**。
- **`--host`（0.8.0 起统一）**：send/read/show/turn/ring/delete/list/register 现都接受 `--host <url>` 手动指定连哪个 control。除 register 会把它存为首选 control（写缓存）外，**其余命令只对当前这条生效、不持久化**。等价于设 `MEETING_HOST` 环境变量，但只作用单条命令。

---

## CLI 子命令完整参数（按功能分）

### 消息收发类（热路径，多由 skill/monitor 自动调）

| 命令     | 位置参数               | 可选参数                                                     | 作用                                                                     |
| -------- | ---------------------- | ------------------------------------------------------------ | ------------------------------------------------------------------------ |
| `send`   | `self` `peer` `[body]` | `--body-file` / `--kind`（默认 `回应`） / `--ask` / `--host` | 插入一条消息并翻转发言权（turn）。body 走位置参数或 `--body-file` 二选一 |
| `read`   | `self` `peer`          | `--limit`（30） / `--since`（0，只取 id>since） / `--host`   | 以 TSV 行 dump 消息                                                      |
| `show`   | `self` `peer`          | `--limit`（30） / `--host`                                   | markdown 美化渲染                                                        |
| `turn`   | `self` `peer`          | `--host`                                                     | 打印当前发言权归谁                                                       |
| `ring`   | `self`                 | `--since`（0） / `--host`                                    | monitor 轮询用：列出 turn=self 且有新消息的房间                          |
| `delete` | `self` `peer`          | `--host`                                                     | 硬删一个房间及全部消息（原子）。斜杠入口：`/meeting delete <peer>`       |

`self` = 本会话注册名，`peer` = 对端名。上面 `--host` 均为"仅本命令、不持久化"。

### 会话目录 / 注册类

**`register <name>`** — 把本会话写入中央 sessions 表。
- `--cwd <path>`（非 `--global` 时必填；带 `--global` 时缺省为 `~`）— 本会话工作目录
- `--force` — 即使 monitor 心跳仍在也覆盖
- `--director` — 注册为 director（默认 worker）
- `--host <url>` — 显式指定 control URL（如 `http://10.0.0.5:8765`），并**存为首选 control**（写缓存，与其它命令的 `--host` 不同）

**`unregister <name>`** — 经 control daemon 从 sessions 表移除。（无参数）

**`list`** — 列出所有会话名 + 状态（online/empty/historical） + 消息数。
- `--host <url>` — 仅本命令、不持久化
- 斜杠入口：`/meeting list`（合并 controls 显示）

**`init`** — 初始化（建库等）。（无参数）

### 供给类（运维面，全部经 `/meeting setup` 入口）

| 命令        | 参数                                                                          | 作用                                           |
| ----------- | ----------------------------------------------------------------------------- | ---------------------------------------------- |
| `daemon`    | `[action]` = `status` \| `stop` \| `restart`（缺省则启动 / 设本机为 control） | 管理 control daemon                            |
| `controls`  | `--json`                                                                      | 查看/打印已发现的 control 节点                 |
| `telemetry` | `on` \| `off` \| `status`（必填）                                             | 遥测开关                                       |
| `token`     | `[value]`（缺省为查看 / host 生成）                                           | host 设密钥 / client 存凭证 / `clear` 关闭鉴权 |

---

## 辅助可执行文件（均非用户直接命令）

**`meeting-daemon`** — control daemon 本体。
- `--port`（默认 8765）
- `--bind`（默认 0.0.0.0）
- `--no-mdns` — 关闭 mDNS 广播

**`monitor.py`** — 被 SKILL 调起轮询来电，底层即 `ring`。
- 用法：`python3 monitor.py <name>`

**`statusline.py` / `supervisor.py` / `session-bootstrap.py`** — 状态栏徽章 / 监督进程 / 会话引导。无用户参数。

后端：SQLite，`~/.agent-meeting/db/rooms.db`。

---

## 现状观察

1. **`self`/`peer` 位置参数在 6 个消息命令里重复出现**——`self` 即本会话注册名，每条命令都要重敲。CLI 本身无状态，由 skill 帮忙拼参数兜底，是当前最明显的人机摩擦点。
2. **斜杠面分两层**：使用面 `/meeting [<name>|list|delete]`（日常）+ 供给面 `/meeting setup [daemon|token|telemetry]`（机器/网络级配置，低频）。controls 并入 list，不再单列。消息收发（send/read/show/turn/ring）仍全是 Bash 面、agent 背后调。
3. **`--host` 已在 0.8.0 统一**到所有解析 control 的命令，跨 control 手动选址从 register 专属变成通用能力（逃生口性质，主路径仍是 mDNS 自动发现）。mDNS 选址（IP 优先内网网卡、避开 tailscale CGNAT 段）仍是近期活跃区。
