# Plugins Codex 架构审计

> 归档于 2026-07-30。本文记录重构前的架构审计，所述版本、目录和迁移计划不再代表当前实现。

## 一、结论

当前问题不是简单的目录命名混乱，而是安装、更新、启动、平台适配和运行时维护由多套机制重复负责。

最终应收敛为：

1. 在仓库顶层建立 `codex/`，作为整个插件仓库的 Codex 分发层。
2. 将 `mycodex` 和本机 Codex 守护进程移出 `agent-meeting`。
3. 只保留 Codex 原生插件缓存这一套插件安装权威。
4. 将 agent-meeting 做成标准 Python 包，不再逐个复制脚本到 `~/.agent-meeting/bin`。
5. 拆分 SessionStart、运行时安装和平台服务管理。
6. 用户只需要理解两个入口：`meeting` 和 `mycodex`。
7. 历史兼容清理只执行一次，完成迁移后删除相关代码。

## 二、当前实际存在的多份代码

当前开发机上同时存在五份 agent-meeting：

| 位置 | 版本 | 用途 |
|---|---:|---|
| `/Users/tommyclaw/AIAgent/plugins/agent-meeting` | 0.14.0 | 开发源码 |
| `~/.codex/plugins-src/agent-meeting` | 0.13.6 | `mycodex --update` 使用的更新克隆 |
| `~/.codex/plugins/agent-meeting` | 0.13.8 | 旧式安装器复制出的插件 |
| `~/.codex/plugins/cache/woodor/agent-meeting/0.14.0` | 0.14.0 | Codex 原生插件缓存 |
| `~/.agent-meeting/bin` | 混合生成物 | 命令包装器和复制出的 Python 文件 |

当前实际运行的是原生插件缓存中的 0.14.0，通过
`~/.agent-meeting/.bin-plugin-root` 指向它。

问题在于其他旧版本仍然存在，而且从目录外观上看都像可以执行的正式安装。

## 三、当前安装链路

```text
install-codex-plugins.sh / install-codex-plugins.ps1
  → 克隆或更新 ~/.codex/plugins-src
  → 执行 install-codex.py
  → 把插件复制到 ~/.codex/plugins/<name>
  → 执行 agent-meeting/codex/install.py
  → 执行 agent-meeting/bin/session-bootstrap.py
  → 生成 ~/.agent-meeting/bin
  → 再通过 codex plugin add 安装同一个插件
  → 生成 Codex 原生插件缓存
  → 下次 SessionStart 从原生缓存再次执行 bootstrap
  → 重写 ~/.agent-meeting/bin
```

同一次安装同时使用了旧式目录复制和 Codex 原生插件安装。

## 四、当前更新和启动链路

### `mycodex --update`

它会执行以下操作：

```text
更新 ~/.codex/plugins-src
  → 重新运行根安装器
  → 重新复制插件
  → 重新安装原生插件
  → 重新生成运行时
```

这部分逻辑与根目录的两个远程安装脚本重复。

### `am-codexd update`

它不更新插件代码，只负责把当前运行的本机 Codex 守护进程切换到已经安装的插件版本。

两个命令都叫“更新”，实际作用却处在完全不同的层级。

### `mycodex` 启动

```text
~/.agent-meeting/bin/mycodex
  → 读取 ~/.agent-meeting/.bin-plugin-root
  → 找到当前 agent-meeting 插件目录
  → codex/codex-meeting.py
  → bin/am-codexd
  → codex/am_codexd.py
  → Codex app-server
  → Codex TUI
```

一个启动命令跨越了用户运行目录、指针文件、插件缓存、`bin/`、`codex/` 和两层守护进程。

## 五、根因

### 1. 安装权威不唯一

以下三处都在写入或重建运行时：

- 仓库根安装器；
- agent-meeting 的 Codex 安装器；
- SessionStart 自修复脚本。

### 2. 旧安装模式没有退出

`install-codex.py` 先把完整插件复制到 `~/.codex/plugins/<name>`，随后又通过 Codex 原生插件管理器安装一次。

旧副本不会自动消失，因此不同版本长期并存。

### 3. SessionStart 脚本承担了过多职责

`session-bootstrap.py` 约有 1275 行，同时负责：

- 创建目录和虚拟环境；
- 安装依赖；
- 生成命令包装器；
- 复制 Python 文件；
- 运行时版本选择；
- 防止旧会话降级运行时；
- macOS launchd；
- Windows 启动项、计划任务和 supervisor；
- Linux 控制节点启动；
- Claude Code 状态栏；
- SessionStart 上下文；
- 遥测；
- 历史文件清理。

插件安装器还会调用这个 SessionStart 脚本，再解析原本写给 Claude Code 的 JSON 输出。

### 4. `mycodex` 的归属倒置

`mycodex` 负责更新 Woodor Codex 插件并启动 Codex，属于插件仓库的上层入口。

但它的源码位于 `agent-meeting/codex`，安装副本也由 agent-meeting 修复。等于上层入口被下层插件拥有。

### 5. `bin/` 和 `codex/` 没有统一分类原则

当前目录同时按以下标准分类：

- 是否是命令；
- 是否是 Python 实现；
- 是否属于 Codex；
- 是否与平台有关；
- 是否用于安装；
- 是否用于 Claude Code。

例如：

- `bin/am-codexd` 是 Codex 专用命令；
- 它的实现却在 `codex/am_codexd.py`；
- `codex/install.py` 是安装器；
- `bin/session-bootstrap.py` 又包含跨平台安装逻辑。

### 6. 平台适配分散

POSIX 和 Windows 差异散落在：

- 根目录两个安装脚本；
- 三个 `mycodex` 包装文件；
- 根安装器；
- agent-meeting 安装器；
- SessionStart；
- `meeting`；
- Windows supervisor。

### 7. 历史兼容代码长期保留

当前代码仍在清理或识别：

- `codex-plugins`；
- `meeting-say`；
- `meeting-daemon`；
- 旧 Codex SessionStart hook；
- Windows 旧包装器。

这些应通过一次明确迁移清掉，而不是永远保留在主运行路径中。

## 六、目标目录

```text
plugins/
├── README.md
├── LICENSE
├── .claude-plugin/
│
├── codex/                         # 整个仓库的 Codex 分发层
│   ├── install.sh                 # POSIX 初始安装
│   ├── install.ps1               # Windows 初始安装
│   ├── install.py                 # 唯一安装协调器
│   ├── README.md
│   └── mycodex/
│       ├── launcher.py            # 启动 Codex TUI
│       ├── daemon.py              # 本机 Codex 守护进程
│       ├── daemon_cli.py          # 诊断入口
│       └── platform/
│           ├── posix.py
│           └── windows.py
│
├── agent-meeting/
│   ├── .claude-plugin/
│   ├── .codex-plugin/
│   ├── pyproject.toml
│   ├── skills/
│   ├── hooks/
│   │   ├── hooks.json
│   │   └── session_start.py       # 只输出 SessionStart 上下文
│   ├── installer/
│   │   └── runtime.py             # 只安装和升级运行时
│   ├── src/agent_meeting/
│   │   ├── cli.py                 # meeting
│   │   ├── control_server.py      # 当前 amctl
│   │   ├── monitor.py
│   │   ├── discovery.py
│   │   ├── config.py
│   │   ├── claude/
│   │   │   └── statusline.py
│   │   └── services/
│   │       ├── macos.py
│   │       ├── windows.py
│   │       └── linux.py
│   ├── migrations/
│   └── tests/
│
├── handoff/
├── init-agents/
└── save-money/
```

## 七、职责边界

### 顶层 `codex/`

负责：

- Codex 初始安装；
- Woodor Codex 插件安装和更新；
- `mycodex`；
- 本机 Codex app-server 守护进程；
- Codex 平台启动差异。

它可以依赖 agent-meeting，但 agent-meeting 不得反向生成或修复 `mycodex`。

### `agent-meeting`

负责：

- 身份和消息；
- `meeting` 命令；
- 中央控制节点；
- 网络发现与传输；
- Claude Code monitor 和状态栏；
- macOS、Windows、Linux 的控制节点常驻。

它不负责：

- Codex 插件安装；
- Codex app-server；
- 顶层 Codex 启动器。

### SessionStart

只负责：

- 检查运行时是否可用；
- 输出当前会话需要的上下文；
- 失败时不阻塞会话启动。

它不负责安装依赖、生成包装器、维护系统服务或清理历史文件。

### 运行时安装器

只负责：

- 创建或升级 agent-meeting 运行环境；
- 安装 agent-meeting Python 包；
- 安装平台服务定义；
- 记录唯一的运行时版本。

## 八、用户命令

PATH 上只保留：

```text
meeting
mycodex
```

建议命令面：

```text
meeting control start|status|stop|restart
mycodex [会话参数]
mycodex update
mycodex doctor
mycodex debug daemon status|restart
```

`amctl` 和 `am-codexd` 改为内部进程模块，不再作为独立的顶层用户命令。

## 九、目标安装模型

```text
远程平台安装脚本
  → 临时下载仓库
  → 执行 codex/install.py
  → 通过 Codex 原生插件管理器安装插件
  → 安装独立的 mycodex 运行时
  → 删除临时下载
```

结果：

- 不再保留 `~/.codex/plugins-src`；
- 不再生成 `~/.codex/plugins/<name>`；
- Codex 原生插件缓存是唯一插件安装权威；
- 删除 `.bin-plugin-root`；
- 不再逐个复制 Python 文件；
- 不再手工维护多套 POSIX/Windows 包装器；
- 每个运行时只有一个写入方；
- 每个运行时只有一个明确版本。

运行目录建议：

```text
~/.codex/woodor/        # mycodex 运行时和状态
~/.agent-meeting/       # agent-meeting 运行时、数据、日志和服务状态
```

`mycodex` 不再安装到 `~/.agent-meeting/bin`。

## 十、当前文件如何处理

| 当前内容 | 目标处理 |
|---|---|
| 根目录 `install-codex-plugins.sh` | 移到 `codex/install.sh` |
| 根目录 `install-codex-plugins.ps1` | 移到 `codex/install.ps1` |
| 根目录 `install-codex.py` | 移到 `codex/install.py` |
| `agent-meeting/codex/mycodex-*` | 移到顶层 Codex 分发层 |
| `agent-meeting/codex/codex-meeting.py` | 移到 `codex/mycodex/launcher.py` |
| `agent-meeting/codex/am_codexd.py` | 移到 `codex/mycodex/daemon.py` |
| `agent-meeting/bin/am-codexd` | 删除公开入口 |
| `agent-meeting/bin/meeting` | 改为 Python 包中的 CLI 模块 |
| `agent-meeting/bin/amctl` | 改为中央控制服务模块 |
| `agent-meeting/bin/meeting_common.py` | 按职责拆成包内模块 |
| `agent-meeting/bin/monitor.py` | 归入 agent-meeting 运行时 |
| `agent-meeting/bin/statusline.py` | 归入 Claude Code 集成 |
| `agent-meeting/bin/supervisor.py` | 归入 Windows 服务实现 |
| `agent-meeting/bin/session-bootstrap.py` | 拆成 hook、运行时安装和服务管理 |
| `remove-legacy-codex-hook.py` | 迁移时执行一次，然后删除 |

## 十一、迁移顺序

这应当作为一个完整的破坏性重构版本完成。开发时可以分多个提交，但中间状态不发布。

1. 建立顶层 Codex 分发目录。
2. 将 `mycodex`、启动器和守护进程移出 agent-meeting。
3. 将 agent-meeting 改成标准 Python 包。
4. 拆分 SessionStart、运行时安装和平台服务。
5. 切换到唯一的 Codex 原生插件安装链路。
6. 删除更新克隆和运行时指针。
7. 删除旧安装器、包装器生成和永久兼容代码。
8. 一次性清理 Mac 和 Windows 的旧安装目录及服务项。
9. 验证：
   - POSIX 全新安装；
   - Windows 全新安装；
   - 从当前版本升级；
   - Claude Code 注册和消息；
   - Codex 启动和消息注入；
   - 跨机器控制节点发现；
   - 守护进程停止、升级和重启。

## 十二、最终判断

`mycodex` 不是 agent-meeting 的子组件，也不是 `~/AIAgent/tools` 下的通用工具。

它是本插件仓库面向 Codex 的分发和启动层，因此源码应位于仓库顶层 `codex/`。

agent-meeting 继续作为被该层使用的消息插件和运行服务。
