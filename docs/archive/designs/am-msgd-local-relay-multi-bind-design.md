# am-msgd 本地中转、多地址监听与服务管理设计

> 归档于 2026-07-30。本文是 0.16.0 阶段的设计记录，当前行为以 CLI surface 和代码为准。

状态：0.16.0 已实现；Windows 真机验收按本次范围暂缓

更新时间：2026-07-29

关联文档：

- [agent-meeting / amcodex 运行时架构](../architecture/agent-meeting-runtime-architecture.md)
- [CLI surface](../../../agent-meeting/docs/CLI_SURFACE.md)

## 1. 背景与目标

0.15.4 及更早版本的 `am-msgd` 主要作为 LAN 中央会话/消息中心使用。新需求希望每台安装
agent-meeting 的机器都具备一个默认可用的本地消息中心，并可以在需要时临时开放
某个本机网卡地址，使其成为其他 agent 可访问的消息中转。

目标包括：

1. 安装 agent-meeting 时安装并启用 `am-msgd` 用户级自启动服务；
2. 新安装默认只监听 `127.0.0.1`，不主动暴露到局域网；
3. 执行 `am-msgd --bind=<ip>` 时，在不重启、不替换当前进程的前提下新增指定
   IP 的 listener；
4. 提供 `am-msgd status|start|stop|restart`，管理当前配置的
   `ip-list:port` 服务；
5. 提供 `am-msgd agent-list`，显示当前 `am-msgd` 已知的全部 agent identity
   及其 `online|empty|historical` 状态；
6. 在外部消息中心或网络不可用时，允许同机 agent 显式切换到本机
   `am-msgd`；也允许把本机的具体 LAN IP 临时开放给其他机器。

非目标：

- 本设计不实现多个 `am-msgd` 之间的数据复制或 federation；
- 本设计不实现远端消息中心到本地消息中心的静默自动故障转移；
- 本设计不默认安装需要 root/管理员权限的系统级开机服务；
- 本设计不允许为每个 bind 地址启动一个独立 `am-msgd` 进程。

## 2. 当前实现与需求差异

截至 0.15.4：

- `am-msgd` 默认监听 `0.0.0.0:8765`；
- 服务进程只创建一个 `ThreadingHTTPServer`，只能管理一个监听地址；
- macOS 和 Windows 的持久化启动只在 `is_host=true` 时启用；
- Linux 仍使用 session-bound 进程，没有 `systemd --user` 适配；
- 生命周期入口是 `meeting am-msgd [status|stop|restart]`；
- 直接运行的 `am-msgd` 仍是前台服务进程入口；
- mDNS 发布不区分 loopback-only 与 LAN listener。

现有 macOS LaunchAgent、Windows Startup/Task Scheduler、稳定 runtime wrapper 和
生命周期 adapter 可以复用，但需要把“是否运行本机服务”从旧的 `is_host` 语义中
拆出来。

## 3. 核心设计结论

服务生命周期和 listener 生命周期必须分离：

```text
am-msgd CLI
├── status/start/stop/restart ──> OS 服务管理层
│                                ├── macOS launchd
│                                ├── Windows Task Scheduler/Startup
│                                └── Linux systemd --user
│
├── agent-list ──> 本机 am-msgd `/list`
│                  └── name、project、online|empty|historical
│
└── --bind IP ──> 本地管理接口 ──> 正在运行的 am-msgd
                                  └── ListenerManager
                                      ├── 127.0.0.1:8765
                                      ├── 192.168.x.x:8765
                                      └── 其他指定地址
```

一个机器、一个配置只运行一个 `am-msgd` 进程。进程内部的 `ListenerManager`
为每个实际监听地址创建一个 HTTP server 和 listener 线程，所有 listener 共享：

- 同一个 PID；
- 同一个进程 `instance_id`；
- 同一个 SQLite 数据库；
- 同一组 WebSocket subscribers、心跳和推送状态；
- 同一套认证和消息处理逻辑。

不能采用“每个 IP 一个进程”的方案。WebSocket subscriber、心跳和实时投递状态
保存在进程内存中，即使多个进程共享同一个 SQLite，也会出现在线状态分裂、推送
不一致、重复投递或遗漏。

### 3.1 客户端命令重命名

公开客户端命令从 `meeting` 直接重命名为 `am`，不保留旧 launcher 或兼容别名：

```text
am          agent identity、消息、群组和 control 客户端
am-msgd     本机 message hub 服务及其运维
am-codexd   Codex session broker
```

`imagent` 和 `talkto` 技能继续保留原名，但其全部 shell 调用改为
`~/.agent-meeting/bin/am` 或 Windows 的 `am.exe`。安装、升级、运行时激活、
AGENTS.md 模板、技能、测试和文档必须在同一版本完成迁移。runtime 激活成功后删除
旧的 `~/.agent-meeting/bin/meeting[.exe]`，旧调用直接失败。

不重新使用旧名称 `amctl`，避免与 pre-0.15 中央 daemon 的残留脚本和迁移逻辑
混淆。

## 4. 配置模型

建议新增专用配置文件：

```text
~/.agent-meeting/am-msgd.json
```

初始结构：

```json
{
  "schema_version": 1,
  "enabled": true,
  "port": 8765,
  "binds": [
    "127.0.0.1"
  ],
  "mdns": "auto"
}
```

配置约束：

- 新安装始终以 `enabled=true`、`binds=["127.0.0.1"]` 初始化；
- `binds` 保存期望状态，运行时另行维护 active/pending/error 状态；
- bind 参数只接受数字 IP，不隐式解析 hostname；
- 正常配置中必须保留 `127.0.0.1`；旧 `is_host=true` 迁移得到的
  `0.0.0.0` 也覆盖 loopback，并作为兼容例外；
- 所有写入都使用原子替换；
- CLI 并发修改配置时必须加跨进程文件锁；
- daemon 运行时由 daemon 作为 bind-list 的唯一写入者；
- daemon 停止时，CLI 才可以在文件锁保护下直接修改期望配置；
- `stop` 只改变 `enabled`，不删除 bind-list 或 port；
- 更新安装不能覆盖用户已有 bind-list、port 或手工 stop 状态。

业务 `auth_token` 可以继续保留在现有 `config.json`，服务生命周期配置不要继续与
`is_host` 混在同一语义中。

## 5. ListenerManager

当前单个 `ThreadingHTTPServer` 应改为进程内 listener 集合。每个 listener 至少
记录：

```text
address
port
state: starting | active | failed | stopping
server
thread
last_error
```

### 5.1 启动

启动顺序：

1. 读取并校验配置；
2. 优先启动 loopback listener；
3. 再逐个启动非 loopback listener；
4. 启动成功后更新 runtime 状态；
5. 存在 LAN listener 时按需发布 mDNS；
6. 启动 watchdog 和 WebSocket heartbeat。

非 loopback 地址启动失败不能拖垮 loopback 服务。典型场景是开机时网卡尚未就绪、
DHCP 地址已经变化或临时 VPN 接口不存在。此时服务进入 `degraded` 状态，保留
loopback listener，并对配置中尚未激活的地址进行有限频率重试。

### 5.2 动态新增 listener

`am-msgd --bind=<ip>` 的成功条件：

1. IP 参数合法；
2. 该地址不是已有 active listener，也没有被 wildcard listener 覆盖；
3. OS 成功 bind `ip:port`；
4. listener 线程已经开始服务；
5. 配置原子写入成功。

操作必须满足：

- PID 和 `instance_id` 不变；
- 已建立的 HTTP/WebSocket 连接不中断；
- 已有 listener 不停止；
- 新 bind 失败时，不改变已有 listener，也不污染持久化配置；
- 重复添加同一 IP 是幂等成功；
- 成功结果返回实际 active listener 列表。

### 5.3 动态移除 listener

“临时中转”必须有明确的收口命令，建议同时提供：

```bash
am-msgd --unbind 192.168.1.20
am-msgd --local-only
```

`--unbind` 只关闭指定 listener；`--local-only` 关闭全部非 loopback listener。
最后一个 loopback listener 默认不能删除。

移除 listener 不终止 daemon，也不影响其他地址上的连接。被移除 listener 上已经
建立的长连接可以选择立即关闭，或设置一个很短的 draining 窗口；第一版建议立即
关闭并在 CLI 中明确提示。

### 5.4 Wildcard 地址

`0.0.0.0` 和 `::` 需要特殊处理：

- 新安装和常规操作应鼓励绑定具体 IP，不推荐 wildcard；
- 已存在 `0.0.0.0` 时，具体 IPv4 地址已经被覆盖，不再创建重复 listener；
- 已存在具体 IPv4 listener 时，动态添加 `0.0.0.0` 通常无法跨平台无中断完成，
  应拒绝并要求修改配置后执行 `restart`；
- IPv6 server 必须显式设置 `IPV6_V6ONLY`，不能依赖操作系统默认值推断其是否
  覆盖 IPv4；
- status 输出 IPv6 URL 时必须使用方括号。

## 6. 本地管理接口

跨平台第一版建议复用 loopback HTTP 服务提供管理接口：

```text
GET    /_admin/listeners
POST   /_admin/listeners
DELETE /_admin/listeners/<ip>
```

必须同时满足：

- 只接受 loopback 来源；
- 状态变更只允许 POST/DELETE，不能使用 GET；
- 安装时生成独立的本地管理 token；
- token 文件只允许当前用户读取；
- 非 loopback 来源的 `/_admin/*` 请求一律拒绝；即使旧配置使用
  `0.0.0.0` wildcard，也只接受实际来源为 loopback 的请求；
- 管理 token 不得写入日志、status 或普通错误输出；
- bind 成功后再持久化配置；
- 配置持久化失败时回滚刚创建的 listener。

Unix domain socket 和 Windows named pipe 的隔离性更强，但会增加第一版跨平台
复杂度。loopback、来源检查和独立本地 token 的组合可以作为初始实现。

## 7. CLI 语义

公开命令：

```bash
am-msgd status
am-msgd start
am-msgd stop
am-msgd restart
am-msgd agent-list

am-msgd --bind 192.168.1.20
am-msgd --unbind 192.168.1.20
am-msgd --local-only
```

### 7.1 生命周期命令

`start`

- 设置 `enabled=true`；
- 安装或修复当前 OS 的用户级自启动定义；
- 按保存的 bind-list 和 port 启动；
- 重复执行时幂等成功。

`stop`

- 设置 `enabled=false`；
- 停止服务并阻止 KeepAlive/supervisor 立即拉起；
- 保留 bind-list、port、数据库和日志；
- 后续 SessionStart 或插件更新不得擅自重新启用。

`restart`

- 设置或保持 `enabled=true`；
- 停止当前 daemon；
- 按完整配置重新启动；
- 允许短暂服务中断；
- daemon 原本未运行时，其行为等同于 `start`。

`status`

- 同时读取配置、OS service 状态和 `/health`；
- 区分 configured、active、pending 和 failed listeners；
- 健康且全部期望 listener 激活时退出码为 0；
- stopped、degraded 和配置错误应使用可区分的非零退出码；
- 提供 `--json`，供安装器和测试稳定消费。

### 7.2 bind 命令

服务运行时：

- CLI 通过本地管理接口向当前进程增加 listener；
- 成功后打印新增地址及完整 active 列表；
- 失败返回非零，但现有服务继续运行。

服务停止时：

- CLI 在文件锁下把 IP 写入期望配置；
- 不暗中启动服务；
- 输出“已保存，将在下次 start 生效”。

### 7.3 agent-list 命令

`am-msgd agent-list` 查询本机正在运行的 `am-msgd`，显示该 hub 当前已知的全部
agent identity。默认输出三列：

```text
NAME        PROJ          STATUS
alice       project-a     online
bob         project-a     empty
reviewer    project-b     historical
```

每个 identity 以 `(project, name)` 为唯一键；不同 project 中的同名 agent 必须
分别显示。输出中的 `PROJ` 对应服务端 canonical `project`；全局 identity 的
`PROJ` 显示为 `*`。该命令不显示 group。

状态定义必须直接复用服务端 `/list` 和 session repository 的现有判定：

- `online`：存在 session lease，且 `last_seen` 未超过服务端 online threshold；
- `empty`：仍有 session identity 记录，但 lease 已过期或已经 offline；
- `historical`：当前没有 session identity 记录，但消息历史仍引用该 identity。

命令行为：

- 只查询本机 loopback listener，不通过 mDNS 自动选择其他 hub；
- daemon 未运行或 `/health` 失败时返回非零，并提示执行 `am-msgd start`；
- 默认按 `online`、`empty`、`historical` 分组，再按 `project`、`name` 稳定排序；
- 没有任何 agent 时输出表头和空结果，退出码为 0；
- 提供 `am-msgd agent-list --json`，返回稳定的对象数组；
- JSON 每项至少包含 `name`、`proj`、`status`；其中 `proj` 映射服务端
  `project`。后续可以兼容增加 `role`、`last_seen`、`host` 或 `os`，但三项
  基础字段不得删除或改名；
- 业务认证开启时，CLI 从本机配置读取 `auth_token`，不能绕过 `/list` 认证；
- 不直接读取 SQLite，以确保状态阈值、迁移和 repository 语义只有一个权威实现。

`am list` 继续作为可查询显式 `--host`/当前 control 的通用客户端命令；
`am-msgd agent-list` 是本机 hub 的运维视图。二者必须复用相同服务端数据，不能各自
重新计算状态。

### 7.4 内部 serve 命令

OS service 不再调用含糊的裸 `am-msgd`，改为：

```bash
am-msgd serve --config ~/.agent-meeting/am-msgd.json
```

`serve` 是 daemon 内部/运维入口；其余子命令是控制器。这样可以避免服务定义调用
控制器后再次控制自身。

旧的裸进程参数模式不保留兼容层。原有 `am-msgd --port ...` 调用、launchd plist、
Windows 任务和测试必须在同一版本中直接迁移为 `am-msgd serve ...`；旧调用返回
明确的参数错误，不隐式进入前台服务模式。

现有 `meeting am-msgd ...` 在同一版本中直接删除，不保留兼容别名。所有服务生命
周期、listener 管理和本机 hub 运维入口统一归属 `am-msgd`；原调用方、技能、文档
和测试必须同步迁移。

## 8. status 输出

人类可读输出建议：

```text
service:    running
autostart:  enabled (launchd)
pid:        12345
version:    0.15.x
instance:   4f9c...
port:       8765
listeners:
  127.0.0.1:8765       active
  192.168.1.20:8765    active
mdns:       advertising 192.168.1.20:8765
auth:       disabled (LAN exposure warning)
config:     ~/.agent-meeting/am-msgd.json
log:        ~/.agent-meeting/logs/am-msgd.log
```

status 不能只看 PID 或 service manager。至少应区分：

- OS service 定义是否存在；
- 自启动是否启用；
- daemon 是否存活；
- `/health` 是否成功；
- daemon 版本和 `instance_id`；
- 配置中的 listener 与实际 listener 是否一致；
- 每个未激活地址的最后错误；
- mDNS 和业务认证状态。

`/health` 建议新增：

```json
{
  "configured_listeners": ["127.0.0.1:8765", "192.168.1.20:8765"],
  "active_listeners": ["127.0.0.1:8765"],
  "listener_errors": {
    "192.168.1.20:8765": "address not available"
  }
}
```

## 9. 自启动与平台边界

默认采用用户级服务：

| 平台 | 推荐实现 | 默认启动时机 |
|---|---|---|
| macOS | LaunchAgent，`RunAtLoad + KeepAlive` | 用户登录后 |
| Windows | 现有 Startup/Task Scheduler supervisor | 用户登录后 |
| Linux | `systemd --user enable --now am-msgd.service` | 用户登录后 |

文档和 CLI 应使用“随用户登录自动启动”，而不是承诺“未登录时随操作系统启动”。
真正的系统级 LaunchDaemon、Windows system task 或 Linux system service/linger
需要额外权限，只能作为显式高级安装选项。

安装路径必须统一调用共享的 `ensure_local_message_hub_service()`，不能在 Claude、Codex、
macOS、Windows 安装器中分别复制业务逻辑。

首次安装：

1. 创建默认 loopback 配置；
2. 安装用户级服务定义；
3. 立即启动；
4. 验证 loopback `/health`；
5. 安装失败时输出明确诊断，但不能留下半写入配置。

更新安装：

1. 激活新 immutable runtime；
2. 保留配置、数据库和日志；
3. 修复 service definition，使其继续指向稳定 wrapper；
4. 仅在 `enabled=true` 时重启服务；
5. `enabled=false` 时保持停止状态。

卸载：

- 停止并删除 service definition；
- 删除运行时和管理 token；
- 数据库、消息和用户配置是否删除必须作为独立选择，不能默认破坏。

## 10. is_host 与旧版本迁移

`is_host` 不再控制 daemon 是否运行。新语义中每台安装机器都有 loopback daemon，
是否开放 LAN 由 bind-list 决定。

迁移建议：

- 旧配置 `is_host=false` 且没有新服务配置：
  - 创建 `enabled=true`；
  - 创建 `binds=["127.0.0.1"]`。
- 旧配置 `is_host=true` 且没有新服务配置：
  - 为保持既有 LAN 行为，迁移为 `binds=["0.0.0.0"]`；
  - status 输出 wildcard 和无认证风险提示；
  - 后续引导用户改为具体 IP。
- 已有 Windows stop sentinel 或 macOS 手工停止状态：
  - 迁移成 `enabled=false`；
  - 安装或 SessionStart 不自动复活。
- 旧 service definition：
  - 更新为 `am-msgd serve --config ...`；
  - 指向 `~/.agent-meeting/bin/am-msgd` 稳定 wrapper；
  - 确认新服务健康后再移除旧任务/plist。

迁移必须幂等，并允许安装被中断后安全重试。

## 11. mDNS

loopback-only 模式不发布 mDNS。否则其他机器会发现一个无法通过 LAN 连接的服务。

`mdns=auto` 的规则：

- 只有 loopback listener：不发布；
- 第一个非 loopback listener 激活：开始发布；
- 非 loopback 地址变化：更新发布记录；
- 最后一个非 loopback listener 被移除：撤销发布；
- daemon degraded 且没有可用 LAN listener：撤销发布。

mDNS 应发布实际可达的具体地址，不把 `0.0.0.0` 当作客户端连接地址。

## 12. 网络故障与消息权威

本地 daemon 解决的是“显式使用本机作为临时消息权威”，不等于透明高可用。

远端 hub 与本地 hub 拥有不同的：

- SQLite 消息历史；
- 在线 session；
- WebSocket subscription；
- 投递和读取 cursor；
- group 状态。

因此网络故障时不能自动把现有客户端静默切换到本地 hub。否则容易形成两个独立消息
世界，网络恢复后也无法自动合并。

第一阶段使用显式切换：

```bash
amcodex --am-msgd 127.0.0.1
```

或者让其他机器显式连接临时中转机：

```bash
amcodex --am-msgd 192.168.1.20
```

若未来需要自动故障转移，应先设计 hub identity、leader 选择、消息复制、cursor
合并和冲突解决，不应仅增加一个 URL fallback 列表。

## 13. 安全要求

- 默认只监听 loopback；
- 优先允许具体私网 IP，不鼓励 `0.0.0.0`；
- 第一次增加非 loopback listener 时输出网络暴露警告；
- status 必须显示业务 `auth_token` 是否启用；
- `agent-list` 必须服从现有业务认证，不能成为绕过 `/list` 的本地数据库后门；
- 绑定公网地址或 wildcard 且未配置认证时，建议要求显式
  `--allow-unauthenticated`；
- 指定非本机 IP 时，由实际 socket bind 校验并返回失败；
- 管理接口永远不通过 LAN listener 开放；
- 日志不得包含管理 token 或业务 token；
- `--local-only` 必须可以快速收回临时网络暴露；
- 服务配置和 token 文件必须使用当前用户专属权限。

## 14. 测试范围

### 14.1 单元测试

- 配置默认值、schema 校验和原子写入；
- `is_host`、stop sentinel、旧 plist/task 迁移；
- CLI parser 和退出码；
- `agent-list` 三种状态、稳定排序、同名跨 project 和全局 identity；
- `agent-list --json` schema、空结果和 daemon 不可达错误；
- IPv4、IPv6、wildcard 覆盖关系；
- 重复 bind/unbind 的幂等性；
- mDNS auto 状态转换；
- enabled 状态不会被安装/update/SessionStart 覆盖。

### 14.2 进程集成测试

- 新安装默认只有 loopback 可访问；
- 动态 bind 后 PID 和 `instance_id` 不变；
- 原有 WebSocket 连接不中断；
- 新地址和旧地址返回相同 `instance_id`；
- `agent-list` 与 `am list` 对同一 hub 返回一致的 identity 和状态；
- bind 失败时旧 listener 继续健康；
- 配置写入失败时新 listener 回滚；
- `--unbind` 和 `--local-only` 不终止 daemon；
- restart 后 bind-list 保留；
- stop 后 SessionStart 不自动复活；
- 非 loopback 地址暂时不可用时，loopback 正常且 status 为 degraded；
- mDNS 只在存在 active LAN listener 时发布。

### 14.3 安装与更新测试

- macOS LaunchAgent 定义和真实 launchctl smoke；
- Windows Startup、Task Scheduler、supervisor 真机 smoke；
- Linux `systemd --user` 安装、enable、stop 和 update smoke；
- 从 pre-0.15 及 0.15.4 配置升级；
- 更新后数据库、bind-list、port 和 enabled 状态不变；
- `am-update` 激活 `am`、删除旧 `meeting` launcher、迁移服务定义并按
  `enabled` 状态重启或保持停止；
- `am-update` 对活跃 `am-codexd` 只执行既有的 `update --defer-if-active`，
  不能终止发起更新的 Codex 会话；
- 卸载服务时不默认删除数据库。

## 15. 0.16.0 实现记录

### 阶段一：核心进程与 CLI（已完成）

- 引入 `am-msgd.json` 和配置锁；
- 实现 `ListenerManager`；
- 实现本地管理接口；
- 实现
  `serve/status/start/stop/restart/agent-list/--bind/--unbind/--local-only`；
- 扩展 `/health`；
- 删除 `meeting am-msgd ...` parser、实现和全部调用方；
- 直接删除旧 foreground CLI 参数模式，并迁移全部调用方和测试。

### 阶段二：平台服务（代码与模拟测试已完成）

- 改造 macOS LaunchAgent adapter；
- 改造 Windows persistence/supervisor；
- 新增 Linux `systemd --user` adapter；
- 统一 stop/restart/enabled 语义；
- 更新 mDNS 生命周期。

### 阶段三：安装、迁移与发布（已完成，Windows 真机测试除外）

- 所有安装器接入 `ensure_local_message_hub_service()`；
- SessionStart 改为自愈而非 host-role 启动器；
- 完成旧配置和 service artifact 迁移；
- `am-update` 接入 runtime、命令和服务配置的完整升级事务；
- 更新 README、CLI surface、帮助文本和卸载流程；
- macOS/Linux 自动化测试和隔离安装验证完成后发布；
- Windows 保留等价实现和模拟测试，本次按要求不执行真机测试。

`am-update` 继续先安装、验证并原子激活新的 immutable runtime，再迁移服务配置。
它只通过 `am-codexd update --defer-if-active` 请求 Codex broker 更新，不直接终止
活跃 Codex 会话。测试必须使用临时 `MEETING_HOME` 或 fake runner，禁止在开发会话
中执行当前已安装的 `~/.agent-meeting/bin/am-update`。

## 16. 验收标准

实现可视为完成，需要同时满足：

1. 新安装后无需打开 Claude/Codex session，本机 `127.0.0.1:8765` 服务已运行；
2. 默认没有 LAN listener，也不发布 mDNS；
3. `am-msgd --bind=<local-ip>` 成功后，原 PID、`instance_id` 和 WebSocket 保持；
4. bind 失败不影响现有服务；
5. `status` 准确显示 service、autostart、配置和实际 listener；
6. `stop` 后重启机器、打开新 session 或更新插件都不会擅自复活；
7. `start/restart` 始终使用当前保存的完整 `ip-list:port`；
8. `--local-only` 能立即收回所有临时 LAN 暴露；
9. `agent-list` 显示当前 hub 的全部 identity，并准确区分
   `online|empty|historical`；
10. `am-update` 在隔离测试中完成 `meeting → am` 和服务迁移，且不会终止活跃
    Codex 会话；
11. macOS/Linux 的用户级自启动与升级通过自动化/隔离验证；Windows 真机的
    Startup、Task Scheduler、supervisor 和升级行为作为发布后的待验项；
12. 旧 `is_host` 用户升级后不会无提示丢失既有 LAN 服务。
