# Plugins 仓库跨宿主架构规范

状态：生效

更新时间：2026-07-28

## 1. 适用范围

本文是 `plugins` 仓库的架构与命名规范，适用于同时面向 Claude Code、
Codex 或其他 AI 宿主的插件。单个插件的产品协议、进程拓扑和数据模型必须写在
该插件自己的 `docs/` 下，不得写回本规范。

文中的“必须”“禁止”是合并门槛；“建议”允许在设计文档中说明理由后偏离。

## 2. 三条正交分类轴

目录和模块设计必须同时区分：

1. **产品域**：插件解决什么问题、拥有哪些命令和数据；
2. **AI 平台**：Claude Code、Codex 等宿主怎样发现插件、注入上下文和管理会话；
3. **操作系统**：macOS、Windows 等系统怎样启动进程、维护 PATH 和持久化服务。

AI 平台和操作系统不是同一个 `platform` 概念，禁止把二者放进一个
`platform.py` 或一组交叉条件中。推荐结构：

```text
<plugin>/
├── src/<package>/
│   ├── commands/
│   ├── <product-domain>/
│   ├── ai_platforms/
│   │   ├── claude_code/
│   │   └── codex/
│   ├── operating_systems/
│   │   ├── macos/
│   │   └── windows/
│   └── installation/
└── tests/
```

只有真正需要对应适配的插件才创建这些目录；纯 skill 插件不应为了形式补空目录。

## 3. Marketplace、manifest、缓存和运行时

这四层必须分开：

| 层 | 内容 | 权威写入方 |
|---|---|---|
| Marketplace | 仓库中可安装插件的目录 | 对应 AI 平台的 marketplace 管理器 |
| 插件 manifest | 单个插件面向某一宿主的身份与资产声明 | 插件源码与发布流程 |
| AI 插件缓存 | skills、hooks、manifest 等版本化只读资产 | Claude Code 或 Codex |
| 共享主机运行时 | 公共命令、包、后台进程及其激活版本 | 产品自己的主机安装器 |

插件缓存不是用户数据目录，也不是长期后台进程的可变工作目录。数据库、日志、
PID、lease、配置和动态依赖禁止写入插件缓存。

### 3.1 仓库根 `.claude-plugin/`

```text
plugins/.claude-plugin/marketplace.json
```

这是 **Claude Code marketplace 目录**。它列出仓库中的多个 Claude Code
插件以及各自的 `source`，供 `claude plugin marketplace ...` 使用。

它不是任何单个插件的 manifest，不存放插件运行态，也不被产品运行时代码读取。

### 3.2 `<plugin>/.claude-plugin/`

```text
<plugin>/.claude-plugin/plugin.json
```

这是 **单个插件面向 Claude Code 的 manifest**。它声明 Claude Code 侧的插件
身份、版本、skills 和 hooks 等资产路径，并建立 `${CLAUDE_PLUGIN_ROOT}` 所在的
插件边界。

根 marketplace 可以列多个插件；这里仅描述当前插件。二者不可合并。

### 3.3 仓库根 `.agents/plugins/`

```text
plugins/.agents/plugins/marketplace.json
```

这是 **Codex marketplace 目录**。它按 Codex schema 声明插件来源、安装策略、
认证策略和分类，供 `codex plugin marketplace ...` 使用。

它与 Claude Code marketplace 服务相似，但 schema 和宿主不同，必须作为独立文件
维护和校验。

### 3.4 `<plugin>/.codex-plugin/`

```text
<plugin>/.codex-plugin/plugin.json
```

这是 **单个插件面向 Codex 的 manifest**。它声明 Codex 侧的插件身份、版本、
skills 和界面元数据，以及存在时的 MCP/app 资产。

Codex 代码不得从 `.claude-plugin/plugin.json` 推断自己的版本或资产；Claude Code
代码也不得把 `.codex-plugin/plugin.json` 当作安装来源。跨宿主共用的产品版本应从
同一发布源生成或在发布校验中比较。

## 4. 产品边界与依赖方向

每个顶层产品目录必须用产品名命名，而不是用宿主名或技术形态命名。只有当一个目录
确实包含多个产品共享的宿主适配时，才允许建立顶层 `codex/`、`claude/` 等目录。

依赖方向应为：

```text
commands / AI adapters / OS adapters
                  ↓
             product domain
                  ↓
        narrow shared infrastructure
```

领域代码不得直接调用：

- `launchctl`、`schtasks`、`taskkill`；
- Win32 console API；
- shell PATH 修改；
- AI 宿主特有的 SessionStart、app-server 或 TUI API。

这些调用必须位于对应 `operating_systems/` 或 `ai_platforms/` 适配器中。

## 5. 命名规范

### 5.1 公开接口

公开命令和 skill 名称是用户协议。重命名必须：

1. 给出新名称与职责的对应关系；
2. 同步修改 manifest、entrypoint、代码、文档和测试；
3. 明确旧名称是兼容别名、迁移清理目标，还是立即移除；
4. 对长期进程保留独立的 `status`、`stop`、`restart`、版本和日志诊断入口。

不得仅因实现迁入包内就取消有独立运维价值的公开进程命令。

### 5.2 文件和模块

文件名必须描述“领域对象 + 职责”。禁止在缺少明确父目录语境时使用：

```text
runtime.py
installer.py
daemon.py
server.py
manager.py
platform.py
utils.py
common.py
control_server.py
```

推荐：

```text
installation/version_activation.py
message_hub/websocket_subscriptions.py
operating_systems/windows/task_scheduler_registration.py
ai_platforms/claude_code/session_message_monitor.py
```

`server.py`、`manager.py` 等词只有在父目录已经唯一确定对象时才可使用。

### 5.3 迁移器

需要跨版本长期保留的迁移入口必须按迁移对象命名，不在文件名中绑定首次引入它的
版本号：

```text
installation/legacy_layout_migration.py
installers/shared/migrate-<product>-legacy-layout.py
```

只有一次发布过程使用的临时脚本才可带版本号，并必须在发布结束后删除。

## 6. 安装器分层

同时支持多个 AI 平台和 OS 时，安装入口必须显式表达四种组合：

```text
installers/
├── claude-code/
│   ├── install-on-macos.sh
│   └── install-on-windows.ps1
├── codex/
│   ├── install-on-macos.sh
│   └── install-on-windows.ps1
└── shared/
    ├── install-<product>-package.py
    ├── activate-<product>-version.py
    ├── register-claude-marketplace.py
    ├── register-codex-marketplace.py
    └── migrate-<product>-legacy-layout.py
```

平台入口负责选择宿主与 OS 行为；`shared/` 只保存幂等、可测试、与入口 shell 无关的
步骤。安装器必须在每一步失败时停止，不能把 marketplace 注册失败伪装成完整成功。

Windows 安装不得假定 `bash`、`python3` 或管理员权限存在；应优先使用
`py -3`，再回退到明确发现的 `python`。包含用户文本的 argv 禁止经 `%*` 或额外的
`cmd.exe` 层重解析。

## 7. 共享运行时和用户数据

只有需要公共系统命令、后台进程或跨宿主共享 Python 包的产品才建立共享主机运行时。
推荐布局：

```text
~/.<product>/
├── runtimes/<version>/
├── active-runtime.json
├── bin/
├── config.json
├── db/
├── logs/
└── run/
```

约束：

1. 版本目录安装完成后不可原地覆盖；
2. 校验全部 entrypoint 后才原子切换激活版本；
3. 稳定命令位于 `bin/`，POSIX 可使用原子替换的符号链接，Windows 使用
   argv-safe 的 console launcher；
4. 已运行进程可以继续使用已经加载的旧版本；
5. 新会话和 SessionStart 不得把共享运行时降级；
6. 用户数据与运行时版本目录分离；
7. 清理只命中已知旧文件和服务名，不递归删除不明目录。

纯 skill 插件没有共享运行时，不应创建上述结构。

## 8. AI 平台适配规范

Claude Code 适配可负责 hooks、SessionStart 上下文、status line 和会话 monitor；
Codex 适配可负责 plugin UI、AGENTS 指令、app-server、TUI 或 thread/turn 生命周期。

共同规则：

- hook 失败不得阻止宿主会话启动；
- hook 读取插件缓存中的只读资产；
- 安装、更新与运行态修复必须有清楚边界；
- 宿主 A 的代码不得读取宿主 B 的 manifest；
- 共用 skill 时，文档必须分别说明两个宿主的调用语法。

## 9. OS 适配规范

macOS 和 Windows 的差异集中在 OS adapter：

| 能力 | macOS | Windows |
|---|---|---|
| 登录后常驻 | launchd LaunchAgent | Startup + 用户级 Task Scheduler，或明确声明需管理员的 Service |
| 后台进程 | POSIX session/signal | Windows creation flags/process control |
| 命令入口 | 可执行脚本/符号链接 | pip 生成的 `.exe` 或等价 argv-safe launcher |
| PATH | shell 配置 | 用户环境变量 |
| 终端标题 | ANSI/宿主能力 | Win32 console adapter |

不能把 POSIX 测试结果当作 Windows 验收结论。无管理员安装也不能声称提供“登录前”
Windows Service。

## 10. 兼容和迁移

兼容代码必须是单向的：识别旧布局、迁移或清理，然后进入新路径。禁止新运行态继续
写旧格式。

迁移器必须：

- 按旧产物是否存在判断，而不是只按版本字符串判断；
- 可重复执行；
- 覆盖两个 OS 的旧服务、wrapper、PID、sentinel 和用户配置；
- 只删除精确白名单目标；
- 有全新安装、旧安装、部分迁移和重复执行测试。

支持窗口结束后，先从正常安装路径移除迁移器，再在后续版本删除实现。

## 11. 验证门槛

每个支持的 “OS × AI 平台” 格子至少验证：

| 类别 | 必测内容 |
|---|---|
| 分发 | marketplace schema、manifest schema、插件发现与安装 |
| 主机运行时 | 全新安装、重复安装、版本激活、公开 entrypoint |
| 使用 | skill 发现、直接/群组消息或该产品的核心交互 |
| 运行态 | 启动、健康检查、停止、重启、崩溃恢复 |
| 更新 | 新旧会话并存、禁止降级、旧布局迁移 |
| 参数 | Unicode、空格、引号、`< > & |` |
| 安全 | loopback/LAN 边界、认证、精确清理 |

CI 中应同时包含：

- Python/脚本单元测试；
- manifest 和 skill validator；
- 架构边界测试（领域模块不含 OS/AI 专用调用）；
- 隔离目录中的打包安装 smoke；
- 可用时的 macOS 与 Windows 真机 release smoke。

未执行的真机格子必须明确标为“延期/未验证”，不能写成“通过”。

## 12. 合并检查清单

- [ ] 产品、AI 平台、OS 三条轴在目录中可辨认；
- [ ] 两份 marketplace 与两个宿主 manifest 各司其职；
- [ ] 插件缓存只读，用户数据和动态依赖位于缓存外；
- [ ] 文件名能从名称判断对象与职责；
- [ ] 每个公开长期进程保留独立诊断命令；
- [ ] Windows 不经不安全的 argv 转发层；
- [ ] 升级迁移幂等且只清理白名单目标；
- [ ] 四维验证矩阵有证据，延期项被明确记录；
- [ ] 插件专属架构位于该插件自己的 `docs/`。
