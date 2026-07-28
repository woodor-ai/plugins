# agent-meeting / mycodex 运行时架构

版本：0.16.0

更新时间：2026-07-28

仓库级插件规范见
[`../../docs/plugin-architecture-guidelines.md`](../../docs/plugin-architecture-guidelines.md)。
本文只描述 agent-meeting 与 mycodex 的产品边界、四维安装/运行方式和兼容策略。

## 1. 架构结论

本实现使用三条正交分类轴：

- **产品域**：`agent-meeting` 与 `mycodex`；
- **AI 平台**：Claude Code 与 Codex；
- **操作系统**：macOS 与 Windows。

四个公开命令均保留：

| 命令 | 职责 |
|---|---|
| `am` | 身份、会话、消息、群组和客户端操作 |
| `am-msgd` | LAN 中央会话/消息中心的进程与运维 |
| `mycodex` | 以 agent-meeting 身份启动 Codex TUI |
| `am-codexd` | 本机 Codex session broker 的进程与运维 |

从 0.15.0 起：

- 原 `amctl` 更名为 `am-msgd`。新名称表达它拥有会话、消息、群组和投递游标，
  而不是一个单纯的 control CLI；
- 原 `/meeting` / `$meeting` skill 更名为 Claude Code 的 `/imagent` 和
  Codex 的 `$imagent`；底层 `am` CLI 不改名；
- 旧名称只保留在升级清理代码与迁移测试中。

## 2. 产品边界

### 2.1 agent-meeting

`agent-meeting` 负责：

- 复合身份、项目、会话和群组模型；
- SQLite 消息、会话、群组与游标数据；
- LAN 中央消息中心及 HTTP/WebSocket 协议；
- mDNS 发现和消息中心客户端；
- `am`、`am-msgd`；
- 共用的 `imagent`、`talkto` skills；
- Claude Code 的 SessionStart、monitor 和 status line；
- macOS/Windows 上 `am-msgd` 的常驻适配。

它不负责 Codex TUI/app-server 的生命周期。

### 2.2 mycodex

`mycodex` 是独立 Python 产品包，依赖 `woodor-agent-meeting`，负责：

- `mycodex` 与 `am-codexd`；
- 启动 `codex --remote`；
- 一个本机共享 Codex app-server；
- 每个 TUI 独立的 session lease 和 WebSocket proxy；
- Codex thread/turn 与 agent-meeting 身份的映射；
- 收件箱投递和 Codex 上下文注入；
- macOS/Windows 的后台进程、终端标题与 PATH 适配。

依赖方向是 `mycodex → agent-meeting`，禁止反向依赖。

## 3. 代码边界

```text
agent-meeting/src/agent_meeting/
├── commands/                 # meeting、am-msgd、monitor CLI
├── messaging/                # 项目与身份领域
├── message_hub/              # 中央消息中心、SQLite、WS、mDNS
├── clients/                  # 发现、HTTP、订阅和本机进程客户端
├── ai_platforms/
│   └── claude_code/          # SessionStart、monitor、status line
├── operating_systems/
│   ├── macos/                # LaunchAgent
│   └── windows/              # Startup、Task Scheduler、supervisor
└── installation/             # Python 环境、版本激活、旧布局迁移

mycodex/src/mycodex/
├── commands/                 # mycodex、am-codexd、Codex 用户配置
├── launcher/                 # 单次 TUI session
├── codex_session_broker/     # broker、lease、proxy、消息投递
├── ai_platforms/
│   └── codex/                # AGENTS 指令和 Codex 用户配置
├── operating_systems/
│   ├── macos/
│   └── windows/
└── installation/             # control endpoint 选择
```

源码树中的 `agent-meeting/bin/` 与 `agent-meeting/codex/` 是旧插件缓存和根安装器的
兼容 facade；主实现与 console entrypoint 位于两个标准包中。兼容 facade 不拥有
新的领域逻辑。

## 4. 插件元数据

| 路径 | 实际作用 |
|---|---|
| `.claude-plugin/marketplace.json` | 仓库级 Claude Code marketplace，列出多个插件 |
| `.agents/plugins/marketplace.json` | 仓库级 Codex marketplace |
| `agent-meeting/.claude-plugin/plugin.json` | agent-meeting 的 Claude Code manifest，声明 skills 与 SessionStart hook |
| `agent-meeting/.codex-plugin/plugin.json` | agent-meeting 的 Codex manifest，声明 skills 与 Codex UI 元数据 |

两份 marketplace 分别注册对应宿主；两个 manifest 分别描述同一产品在对应宿主中的
插件资产。它们不存放用户运行态，也不替代共享主机运行时。

## 5. 安装与版本激活

四个显式入口：

| AI 平台 | macOS | Windows |
|---|---|---|
| Claude Code | `installers/claude-code/install-on-macos.sh` | `installers/claude-code/install-on-windows.ps1` |
| Codex | `installers/codex/install-on-macos.sh` | `installers/codex/install-on-windows.ps1` |

共同步骤：

1. 读取 `agent-meeting` 与 `mycodex` 的包版本并要求一致；
2. 在 `~/.agent-meeting/runtimes/<version>/venv` 安装两个包；
3. 校验所有公共及内部 entrypoint；
4. 原子激活到 `~/.agent-meeting/bin`；
5. 幂等执行旧布局迁移；
6. 注册对应 AI 平台 marketplace；
7. Codex 安装额外配置用户环境。

稳定入口在 POSIX 上是指向版本 venv 的符号链接，在 Windows 上是 pip 生成并原子
复制的 `.exe` launcher。Windows 安装器优先 `py -3`，找不到时回退到 `python`。

共享运行时布局：

```text
~/.agent-meeting/
├── runtimes/<version>/venv/
├── active-runtime.json
├── bin/
├── config.json
├── db/rooms.db
├── logs/
└── codex/
```

已激活版本不会被 SessionStart 原地重写。Claude Code 的 SessionStart 仍包含一个
面向 0.15.0 以前安装的自愈兼容路径：只有找不到有效激活运行时时，才维护旧 venv 和
wrapper；存在有效 `active-runtime.json` 时使用版本化运行时。

旧根入口 `install-codex.py` 与 `install-codex-plugins.{sh,ps1}` 暂作兼容安装链。
当选择 agent-meeting 时，共享运行时安装委托给同一版本化安装器；插件源码复制逻辑
仅用于兼容旧 Codex 安装方式。

## 6. 四维安装、使用和运行态

| OS × AI 平台 | 安装 | 用户入口 | 会话运行态 | 常驻进程 |
|---|---|---|---|---|
| macOS × Claude Code | Claude macOS 安装器 | `/imagent`、`/talkto` | SessionStart + `am-session-monitor` | host 上 launchd 管理 `am-msgd` |
| Windows × Claude Code | Claude Windows 安装器 | `/imagent`、`/talkto` | SessionStart + `.exe` monitor | host 上 Startup + MINUTE task 管理 supervisor/`am-msgd` |
| macOS × Codex | Codex macOS 安装器 | `mycodex`、`$imagent`、`$talkto` | TUI → session proxy → app-server | `am-codexd`；host 可另运行 launchd `am-msgd` |
| Windows × Codex | Codex Windows 安装器 | `mycodex.exe`、`$imagent`、`$talkto` | 同上，使用 Windows process adapter | `am-codexd.exe`；host 可另运行 Windows `am-msgd` supervisor |

### 6.1 Claude Code

SessionStart 的包内入口为 `am-claude-session-start`。插件 hook 中的
`bin/session-bootstrap.py` 只负责找到并委托包内实现。它：

- 读取/补齐用户配置；
- 检查激活运行时版本，禁止旧插件会话降级运行时；
- 配置 status line；
- 对 `is_host=true` 的机器协调 OS 持久化；
- 输出当前会话的 additional context；
- 捕获错误，避免阻塞 Claude Code 会话。

`/imagent <name>` 启动会话级 monitor。monitor 通过 WebSocket 订阅 `am-msgd`，
其生命周期属于当前 Claude 会话，不是 OS 服务。

### 6.2 Codex

`mycodex` 的启动链：

```text
mycodex
  → 确保 am-codexd 可用
  → 获取当前 meeting 身份的 session lease
  → 创建本次 TUI 独立的 loopback WebSocket proxy
  → codex --remote ws://127.0.0.1:<port>
  → broker 观察 thread/turn 并注入 agent-meeting 上下文
  → TUI 退出，仅释放本次 lease
```

`am-codexd` 绑定 `127.0.0.1`，拥有一个共享 app-server。存在活动 lease 时，
会中断会话的 stop/restart/update 继续被拒绝。

## 6.1 统一更新入口

`am-update` 是唯一的发布更新入口，属于 `agent-meeting` 核心 runtime，而非
`mycodex`。它从公开仓库获取纯语义版本的 release，安装并原子切换新的共享 runtime，
再分别刷新已安装的 Claude Code 和 Codex adapter。Claude Code 只在下一个会话读取
新的 hook；Codex 的 daemon 切换仍会在存在活动 lease 时拒绝执行。`mycodex` 只负责
启动 Codex 会话，`mycodex --update` 不再承担更新职责。

## 7. OS 运行态

### 7.1 macOS

`am-msgd` host 使用 LaunchAgent：

```text
label: com.tommy.agent-meeting.am-msgd
command: ~/.agent-meeting/bin/am-msgd
```

安装和自愈由 `operating_systems/macos/message_hub_launch_agent.py` 管理。公共领域模块
不直接调用 `launchctl`。

### 7.2 Windows

无管理员默认策略使用两层：

1. Startup 文件夹启动 `am-message-hub-supervisor.exe`；
2. 用户级 `schtasks /SC MINUTE` 周期拉起 supervisor。

任务名：

```text
agent-meeting-am-msgd
```

supervisor 直接执行 `~/.agent-meeting/bin/am-msgd.exe`，不使用
`python.exe am-msgd.exe`，也不经 `%*` 转发用户消息。显式 stop 写入 sentinel，
阻止 supervisor 把人工停止误判为崩溃。

这套无管理员策略只保证用户登录后的持久化；登录前 Windows Service 不在默认能力内。

## 8. 消息中心与数据权威

`am-msgd` 是 LAN 中唯一的消息持久化权威：

- SQLite：`~/.agent-meeting/db/rooms.db`；
- HTTP：身份、会话、消息、群组和管理操作；
- WebSocket：通知、补发、heartbeat 和 delivery cursor；
- mDNS：`_agent-meeting._tcp.local.`；
- 可选 Bearer token：`config.json` 的 `auth_token`。

客户端，包括 localhost 客户端，都通过 HTTP/WebSocket 访问，不直接写数据库。

主要存储模块：

| 模块 | 职责 |
|---|---|
| `sqlite_message_database.py` | schema、连接与幂等列升级 |
| `sqlite_session_repository.py` | session、身份、在线状态和游标 |
| `sqlite_group_repository.py` | group、成员、charter 与入群水位 |
| `sqlite_conversation_repository.py` | 直接会话与消息查询 |
| `websocket_subscriptions.py` | subscriber 状态和 RFC6455 帧 |

注册身份写入进程 `instance_id`。已注册身份发起写操作时必须携带
`X-Meeting-Instance`，避免旧进程或错误 session 复用身份。

新加入群组的成员只接收加入水位之后的群消息；`joined_after_message_id` 由 schema
兼容升级幂等添加。

`am-codexd` 只保存本机 lease、临时 pending queue 和 identity/thread 映射，不建立
第二个消息持久游标权威。

## 9. 公开命令与内部 entrypoint

公共：

```text
am
am-msgd
mycodex
am-codexd
```

安装器和宿主内部使用：

```text
am-session-monitor
am-statusline
am-message-hub-supervisor
am-claude-session-start
am-configure-codex-user-environment
```

`am` 是 agent identity、消息、群组和 control 客户端。原 `meeting` launcher
已直接删除，不保留兼容别名。`am-msgd` 自己提供
`status|start|stop|restart|agent-list`、动态 listener 管理和内部 `serve`
入口；原 `meeting am-msgd ...` 已删除。`am-codexd` 同理保留独立公共运维入口，
不降级为不可见的包内实现。

## 10. 旧布局迁移

`installers/shared/migrate-agent-meeting-legacy-layout.py` 是长期兼容入口，不是本次
重构的临时脚本。包内实现是
`agent_meeting/installation/legacy_layout_migration.py`。

它按旧产物是否存在来幂等清理：

- `amctl` wrapper、PID 和 stop sentinel；
- `com.tommy.agent-meeting.amctl` LaunchAgent；
- `agent-meeting-amctl` Task Scheduler 任务；
- `agent-meeting-amctl.cmd` Startup 文件；
- pre-0.15 的服务名、插件 wrapper 和旧 Codex hook。

迁移器只有在明确结束旧布局支持窗口后才能从正常安装路径移除。

## 11. 验证状态

本轮已完成：

- `agent-meeting` 全量 pytest：180 passed，1 skipped；
- `mycodex` 全量 pytest：15 passed；
- Windows 相关 adapter、版本激活、旧布局迁移和命令构造模拟测试；
- WebSocket、identity、prune、monitor 和动态 listener 集成测试；
- POSIX shell 语法校验；
- 隔离 venv 的两个 Python 包安装，以及 `am`/`am-msgd` entrypoint smoke；
- `.codex-plugin` validator；
- `imagent`、`talkto` skill validator；
- Windows `.exe` 激活和命令构造的本地模拟测试。

按本轮约定暂不执行 Windows 真机安装和进程生命周期测试。因此 Windows 真机的
Startup、Task Scheduler、`am-msgd.exe`、
`am-codexd.exe` 与 Codex `--remote` 联调状态是 **延期/未验证**，不能解释为已经
通过。

后续 Windows release smoke 应至少覆盖：

1. 两个 PowerShell 安装入口和 `py -3` 回退；
2. 四个公共 `.exe` 的 `--help`/版本；
3. 旧 `amctl` 服务与文件清理；
4. Startup + MINUTE task 的启动、崩溃恢复、stop/restart；
5. `X-Meeting-Instance` HTTP/WS 协议；
6. `mycodex` lease 和 `am-codexd` 保护；
7. Claude Code `/imagent` 与 SessionStart；
8. Unicode、空格、引号和 shell 特殊字符 argv。
