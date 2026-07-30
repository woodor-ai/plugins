# am-ctld 本机 Agent 会话生命周期控制架构

状态：已确认架构，macOS/POSIX 主路径已实现，Windows 终端主路径延期

日期：2026-07-30

适用范围：agent-meeting、amcodex、amclaude、Claude Code CLI、Codex CLI，支持
macOS 与 Windows；不管理 Claude/Codex 桌面 App。

## 1. 已确认的前置决策

1. 删除 handoff plugin 写入 `~/.codex/AGENTS.md` 和
   `~/.claude/CLAUDE.md` 的自动 handoff 描述。保留显式 handoff 能力，
   不再允许 LLM 依据时间、轮数或任务边界自行停止接收工作。
2. 从 agent-meeting 消息通道彻底删除 `control:restart`、
   `control:clear` 及 `[control:...]` 指令类功能：
   - `am-msgd` 不承载会话生命周期控制；
   - `am-codexd` 不再把 `kind=control:*` 渲染为控制 turn；
   - agent 指令不再把普通消息正文中的控制前缀当作可执行命令；
   - 升级时应将尚未消费的历史 `control:*` 消息静默丢弃或标记为不支持，
     不得把旧控制正文重新渲染给 agent。
3. 生命周期管理改走本机、非消息化、可鉴权、可确认执行结果的控制面。
4. 新增始终运行的本机控制 daemon。服务进程名使用 `am-ctld`，用户 CLI
   使用 `am-ctl`。

### 1.1 当前实现进度（0.17.0）

已实现：

- 安装/升级时删除 AGENTS.md、CLAUDE.md 中旧的自动 handoff 注入块；
- 消息通道不再渲染或执行 `control:*`，历史控制消息只作静默 tombstone；
- `amcodex`、`amclaude` wrapper，二者均在原终端持有前台子进程，并通过
  鉴权的 loopback 控制端支持 status、双中断 exit 和原终端 restart；
- `am-codexd` 会话盘点、权威 idle/working 状态、token 化 ingress
  pause/resume，以及暂停状态下的二次注入闸门；
- working turn 的 `turn/steer` 有界摘要投递、active turn竞态保护、限频、
  失败保留 pending，以及 idle 大批量摘要 fallback；
- `am-ctld` 每 5 分钟盘点、轮转日志、本机鉴权 API、`am-ctl` CLI、
  macOS LaunchAgent 和 Windows 登录任务定义；
- Claude monitor 鉴权 pause/resume IPC；pause 只有在订阅循环确认断开后
  才成功，resume 后由原订阅 cursor 补收；
- Codex compact 通过 app-server 的 `thread/compact/start` 执行，动作期间
  pause ingress，并验证新的 compaction item 落盘后才 resume；
- Codex handoff 通过 app-server 启动受信任的本机生命周期 turn，只有新的
  handoff card 落盘且 thread 回到 idle 才成功；旧实例保持 draining，
  restart 成功后才用原 pause token 恢复 ingress；
- Claude compact/handoff 通过 tmux 或 iTerm2 Automation 发送受控 slash
  command，验证 transcript compact boundary 或新的 handoff card；handoff
  成功后同样保持 draining；
- Codex clear 通过受控终端发送 TUI `/clear`，并由 am-codexd 确认映射已切换到
  新的 idle thread 后才恢复 ingress；
- Claude transcript token utilization、compact 次数与分平台自动规则；
- 动作状态原子持久化、daemon 重启后的 interrupted action 恢复、持久 cooldown
  与连续失败上限；
- tmux TerminalAdapter 和 iTerm2 session-ID/Automation adapter；
- save-money 自动 handoff 改为后台调用 `am-ctl handoff` + `restart`，不再写
  AMBridge trigger 或向 agent turn 注入控制正文。

仍未完成：

- wrapper 持有的 Windows ConPTY 输入控制；
- Windows 真机、重启登录和 ConPTY 验收；
- AMBridge 生命周期接入按用户要求留在 AMBridge 项目单独处理。

## 2. 目标与非目标

### 2.1 目标

- 每台安装 agent-meeting 的机器均安装并在用户登录后启动 `am-ctld`。
- 至少每 5 分钟完整盘点一次本机 Claude Code CLI 与 Codex CLI 会话。
- 识别会话状态、上下文利用率、compact 次数和可执行能力。
- 只对高置信度 idle 会话执行管理动作。
- 在管理动作期间暂停 agent-meeting 普通消息入口，避免积压消息抢先注入。
- 支持 compact、clear、checkpoint/handoff、exit、原终端 restart。
- 为 save-money、AMBridge 和人工运维提供统一 CLI/本机 API。
- macOS 与 Windows 使用同一状态机、规则模型和审计格式。

### 2.2 非目标

- 不通过 agent-meeting 消息触发任何生命周期动作。
- 不管理 Claude/Codex 桌面 App。
- 不对能力不足的直接启动会话强行注入输入或强行重启。
- 不因状态无法判断而猜测 idle。
- 默认 exit 只发送两次中断并报告结果，不自动升级为强杀。

## 3. 总体架构

```text
                  am-ctl CLI / AMBridge / save-money
                               |
                    local socket / named pipe
                               |
                            am-ctld
              +----------------+----------------+
              |                                 |
      CodexSessionAdapter               ClaudeSessionAdapter
              |                                 |
          am-codexd                       Claude monitor
              |                                 |
      Codex app-server                 Claude wrapper / TTY
              +----------------+----------------+
                               |
           TerminalAdapter: tmux / iTerm2 / tty / ConPTY
```

### 3.1 进程边界

- `am-msgd`：身份、消息、群组、持久投递游标；不理解生命周期动作。
- `am-codexd`：Codex app-server、Codex thread 映射和 meeting inbox 注入。
- `am-ctld`：跨 Claude/Codex 的本机会话发现、状态判断、规则、动作和审计。
- Claude monitor：Claude 会话的 agent-meeting 消息入口，并向 `am-ctld`
  注册本机实例。
- `amcodex` / `amclaude` wrapper：持有前台子进程和原始终端资源，接受
  `am-ctld` 的退出、重启请求。

## 4. 为什么 am-ctld 不与 am-codexd 合并

结论：两个 daemon 保持独立进程，但放在同一个安装包中并共享协议库。

原因：

1. `am-ctld` 在 Claude-only 机器上也必须常驻；`am-codexd` 仅服务 amcodex。
2. `am-codexd` 当前按需启动；`am-ctld` 要求用户登录后常驻。
3. Codex app-server 或 WebSocket proxy 故障不能同时中断 Claude 生命周期管理。
4. 终端控制和 Windows/macOS 适配不应进入 Codex broker 的核心路径。
5. 两者升级、回滚、日志和故障隔离边界不同。

允许的整合方式：

- 共用 `SessionIdentity`、`SessionState`、IPC DTO 和错误码定义；
- `am-ctld` 通过本机受保护 API 调用 `am-codexd`；
- 安装器统一安装、升级和报告两个 daemon；
- `am-ctl status` 汇总 `am-msgd`、`am-codexd`、`am-ctld` 状态。

不采用的方式：在一个 Python 进程中同时运行控制器、Codex app-server proxy
和 Claude/终端适配器。

## 5. TerminalAdapter 模块

`TerminalAdapter` 是一个独立的新模块族，不属于规则引擎，也不塞进
`am-codexd`。建议目录：

```text
agent_meeting/
  lifecycle_control/
    controller.py
    models.py
    rules.py
    actions.py
    sessions/
      codex.py
      claude.py
    terminals/
      base.py
      tmux.py
      mac_iterm2.py
      mac_tty.py
      windows_conpty.py
      windows_terminal.py
```

统一接口至少包含：

```text
capabilities(handle)
send_text(handle, text)
send_interrupt(handle, count=2)
wait_foreground_exit(handle, timeout)
restart_in_place(handle, launch_recipe)
is_alive(handle)
```

每个 adapter 必须显式返回能力，不能让上层根据操作系统猜测：

```text
can_send_text
can_interrupt
can_restart_in_place
can_resolve_window
requires_user_permission
```

已有 AMBridge `amp/host` 中的 macOS iTerm2、tmux、Windows Terminal 和进程树
逻辑可作为迁移参考，但控制接口和持久句柄应在 agent-meeting 中重新形成稳定契约，
避免 `am-ctld` 依赖 AMBridge。

## 6. 会话发现与受控能力

`am-ctld` 使用“事件注册 + 每 5 分钟全量核对”：

- daemon、wrapper、monitor 启动/退出时主动注册；
- 每 300 秒扫描进程、注册表和近期会话日志进行纠偏；
- 外部 CLI 指令立即唤醒规则循环，不等待下一次扫描。

内部会话主键：

```text
platform + name + project + instance_id
```

`name + project` 仅用于用户选择，不能作为内部唯一键。

### 6.1 Codex

- amcodex 会话：从 `am-codexd` 获取 `launch_id`、thread、cwd、PID、rollout、
  token 和状态，属于完全受控会话。
- 直接启动的 Codex CLI：允许通过进程和 rollout 发现并显示状态，但按已确认要求
  不执行管理动作。

### 6.2 Claude Code

- 有 agent-meeting monitor：monitor 向 `am-ctld` 注册 instance、PID、cwd、
  TTY、父进程链和消息入口控制能力，可以执行其声明支持的动作。
- 无 monitor：只读发现，不执行管理动作。
- 若需要可靠原位 restart，还必须由受控 Claude wrapper 启动；仅有 monitor
  不等于拥有终端和父进程生命周期。

### 6.3 Desktop App 排除

采用正向准入而不是只靠进程名排除：

- amcodex lease；
- Claude monitor 注册；
- 或确认存在 CLI TTY/PTY 且可关联到终端会话。

没有 CLI TTY、来自 `.app`/桌面安装目录、无法关联 CLI rollout/transcript 的进程
均不得进入可管理集合。

## 7. 会话状态与置信度

统一运行状态：

```text
working
idle
transitioning
paused
draining
unknown
exited
```

另设控制动作状态：

```text
none
pausing_ingress
maintenance
verifying
resuming_ingress
exiting
restarting
failed
```

判断优先级：

1. 官方或本地接口：
   - Codex：通过 `am-codexd` / app-server 获取 thread 状态；
   - Claude：wrapper/monitor 暴露的状态。
2. 进程和终端状态：前台命令、子进程、未完成工具进程。
3. 日志推断：
   - Codex rollout JSONL；
   - Claude transcript JSONL。
4. 仍无法判断时返回 `unknown`。

日志推断需同时返回：

```text
state
confidence
evidence_timestamp
evidence_source
```

只有 `state=idle` 且 `confidence=high` 才允许自动执行动作。执行前必须再次检查，
防止扫描后会话已经开始工作。

## 8. 规则引擎

至少支持三类输入：

- 当前上下文 token 利用率；
- 当前会话 compact 次数；
- 通过 `am-ctl` 或本机 API 提交的外部指令。

规则配置示例：

```toml
[automation]
enabled = false
action_cooldown_seconds = 600

[codex]
compact_token_pct = 60
handoff_token_pct = 80
max_compactions = 2

[claude]
compact_token_pct = 60
handoff_token_pct = 80
max_compactions = 2
```

0.17.0 默认 `enabled=false`。Codex 从 app-server 读取 token utilization 与
compaction count；Claude 从当前 transcript 的最后 usage/compact boundary
读取对应指标。两者达到 compact 阈值时执行 compact，达到 handoff 阈值或
compact 次数上限时执行 handoff。

`[automation] max_consecutive_failures` 默认值为 3。动作状态、最后更新时间和
连续失败次数持久化在 `control/action-state.json`；daemon 重启时未完成动作会
记为失败。自动动作达到上限后不再重试，直到人工动作成功清零；人工显式命令不受
自动熔断限制。

约束：

- 外部显式指令优先于自动规则；
- 同一会话同一时间只允许一个动作；
- 每轮扫描每个会话最多执行一个动作；
- 使用 cooldown 和 hysteresis 防止阈值附近反复触发；
- 规则命中不等于执行，仍须通过 capability 和 idle 复核；
- 阈值与动作次序在实现前另行评估，不把 AMBridge 现有数值直接视为最终产品默认值。

## 9. 消息入口暂停协议

管理动作必须使用独立控制 lane。暂停的只是 agent-meeting 普通消息入口，
不能阻断 `am-ctld` 自己发起的管理动作。

### 9.1 amcodex / am-codexd

新增本机 API：

```text
GET  /v1/sessions
GET  /v1/sessions/{launch_id}/status
POST /v1/sessions/{launch_id}/ingress/pause
POST /v1/sessions/{launch_id}/ingress/resume
POST /v1/sessions/{launch_id}/actions
```

pause 返回一次性的 `pause_token`。resume 必须携带同一 token，避免一个动作错误解除
另一个动作持有的暂停。

暂停期间：

- scheduler 不得调用 `turn/start` 注入 meeting inbox；
- pending 消息不得 ack；
- 新消息继续由 `am-msgd` 持久保存；
- controller action lane 保持可用。

### 9.2 Claude monitor

不采用 SIGSTOP 或直接杀 monitor 作为正常暂停方式。monitor 新增本机 IPC：

```text
pause_delivery
resume_delivery
status
```

暂停期间 monitor 保持 instance 注册和心跳，但停止 delivery subscription，
不得推进消息 cursor。恢复时从原 cursor 继续补收。

### 9.3 Codex working turn 的 steer 投递与背压

生命周期 pause 只保护维护动作，不能解决普通长程 turn 期间的消息积压。
`am-codexd` 使用 app-server 官方 `turn/steer`，将消息通知追加到当前
in-flight turn，而不是等 idle 后再创建一个包含几十行通知的新 turn。

执行条件：

1. thread 非 idle，且能取得状态为 `inProgress` 的 active turn ID；
2. `expectedTurnId` 必须与当前 active turn 匹配；
3. ingress 未 paused，消息仍属于当前 lease 的 durable pending 队列；
4. 同一 active turn 受 debounce、cooldown、最大 steer 次数和单批最大消息数
   四重限制。

默认参数：

```text
debounce = 3s
cooldown = 30s
max_steers_per_turn = 3
max_messages_per_steer = 100
max_messages_per_idle_turn = 100
idle_digest_threshold = 10
```

可分别通过 `MEETING_STEER_DEBOUNCE_S`、
`MEETING_STEER_COOLDOWN_S`、`MEETING_STEER_MAX_PER_TURN` 和
`MEETING_STEER_MAX_MESSAGES` 覆盖；idle fallback 的硬上限由
`MEETING_IDLE_MAX_MESSAGES` 控制，超过
`MEETING_IDLE_DIGEST_THRESHOLD` 时也改用单个摘要，不再逐条渲染通知行。

steer 内容只包含一个合并摘要、sender 计数和精确 Message ID 列表，不包含
peer 正文。agent 在安全检查点逐个调用 `am message` 读取正文。只有
`turn/steer` 成功后才能 ack 所选消息；active turn 已结束、ID竞态、
`activeTurnNotSteerable`（例如 `/review` 或手动 `/compact`）或其它错误时
保留 pending；同一 active turn 不反复重试，等 active turn 变化或回到 idle
后再投递。

`turn/steer` 不等于 interrupt。正在运行的 shell/tool 不会被取消；追加输入
通常要等当前工具返回、模型重新获得控制后才会被处理。普通消息不得使用
`turn/interrupt`。

## 10. 动作执行

通用动作流程：

1. 获取 `(platform, name, project, instance_id)` 动作锁；
2. 重新确认实例未更换、状态为高置信度 idle；
3. 暂停 meeting ingress；
4. 进入 `maintenance`；
5. 通过平台 action adapter 执行动作；
6. 通过接口、日志、token 指标或交接卡验证；
7. 对非终止动作恢复 ingress；
8. 写入审计日志；
9. 异常时在 `finally` 中恢复 ingress，除非会话已进入 draining/exit/restart。

动作解析：

- handoff 优先使用 Woodor handoff plugin；
- 未安装时尝试平台自带 handoff；
- 两者都不可用时明确失败，不临时拼接未经验证的 prompt。

### 10.1 compact

- 优先使用平台正式 API；
- 无正式 API时才使用受控 wrapper/TerminalAdapter；
- 验证 compact 事件出现、compact 次数增加或 token 利用率下降；
- 未验证成功不得更新规则计数。

### 10.2 clear

- 执行前按配置决定是否先生成 checkpoint；
- 等待新 thread/session 映射稳定；
- 更新 `am-codexd` 或 Claude monitor 的实例映射后再恢复消息。

### 10.3 checkpoint 与 handoff

必须拆开：

- `checkpoint`：只写交接卡，会话继续工作，动作完成后恢复消息。
- `handoff`：写交接卡并把会话置为 `draining`，不恢复旧会话的消息入口，
  等待 exit/restart 或显式 cancel。

如果 handoff 完成后无条件恢复消息，就会重现“已准备退出的旧会话收到积压消息”。

## 11. exit 与原位 restart

### 11.1 exit

1. 暂停 ingress；
2. TerminalAdapter 或 wrapper 向对应前台会话发送两次 Ctrl-C/等价中断；
3. 等待前台进程、monitor/lease退出；
4. 确认 PID、instance 和本地注册均已消失；
5. 超时返回失败，不默认升级为 SIGKILL；
6. 未来如增加 `--force`，必须由用户显式请求。

### 11.2 restart

1. 需要保留上下文时先执行 handoff；
2. 保存并校验 launch recipe 与 terminal handle；
3. 执行 exit；
4. 确认旧实例完全退出；
5. 在原终端资源中执行保存的启动配方；
6. 等待新 PID、instance、thread/monitor 注册完成；
7. 新实例进入 idle/working 后才报告成功；
8. meeting 消息只能恢复到新实例。

持久化 launch recipe 只保存白名单字段：

```text
tui
cwd
name
project
launcher
terminal_type
terminal_handle
safe_cli_args
environment_profile
```

不得保存 token、完整环境变量或未经脱敏的原始命令行。

## 12. wrapper 设计

### 12.1 amcodex

`amcodex` 是由现有 `mycodex` 改名后的 Codex 外部 wrapper。它启动前台
Codex 子进程、持有 `launch_id`，退出时释放 `am-codexd` lease。需要扩展：

- 向 `am-ctld` 注册 launch recipe 和终端 handle；
- 接收本机 exit/restart 请求；
- restart 时在同一 wrapper 和同一 TTY 中重新启动 Codex；
- 将异常退出与正常控制退出区分记录。

### 12.2 amclaude

不继承现有 `myclaude` 的订阅/API利用率判断或配置切换逻辑。旧 `myclaude`
退出本架构，新增 `amclaude`，从第一版开始就是纯粹的 Claude 生命周期 wrapper。

`amclaude` 与 `amcodex` 对等，职责是 Claude session supervisor：

- 成为 Claude CLI 的稳定父进程；
- 注册 instance、PID、cwd、TTY/ConPTY、launch recipe；
- 与 `am-ctld` 建立本机控制通道；
- 支持暂停输入、发送管理命令、发送两次中断；
- 等待 Claude 子进程退出；
- restart 时在同一 TTY/ConPTY 中再次启动 Claude；
- 退出时可靠注销 monitor 和本机会话记录。

对于 Claude，若需要从 daemon 向交互会话可靠注入 `/compact`、handoff 等输入，
wrapper 还需要拥有 PTY（macOS）或 ConPTY（Windows）的控制端；仅作为普通
shell 父进程可以等待和重启子进程，但不能跨平台可靠地向任意已有终端注入输入。

因此，“有 monitor”只说明可以暂停 meeting 消息，不等于具备
`restart_same_terminal` 能力。完整能力需要 `amclaude` wrapper。

## 13. 终端能力与限制

### 13.1 tmux

- 可稳定定位 pane；
- 可发送文本和 Ctrl-C；
- 可在原 pane 重启；
- 是 macOS/Linux 上最可靠的通用 adapter。

### 13.2 iTerm2

- 保存 iTerm2 session ID后可通过 AppleScript/Python API定位原 session；
- 控制程序向 iTerm2 发送 Apple Events 需要 macOS Automation 授权；
- Python API 的 Enable 开关与 macOS Automation/TCC 授权是两件不同的事。

macOS Automation 默认不会静默开启。首次由某个控制应用向 iTerm2 发送受保护
Apple Event 时，系统会要求用户允许；之后可在
System Settings → Privacy & Security → Automation 中查看或修改。

修改 iTerm2 plist 或偏好设置不能授予 macOS Automation/TCC 权限。iTerm2 偏好
可以启用它自己的 Python API，但不能代替系统授权。受管企业设备可以通过 MDM 的
Privacy Preferences Policy Control（PPPC）配置 AppleEvents 权限。

官方参考：

- Apple：<https://support.apple.com/guide/mac-help/mchl108e1718/mac>
- Apple PPPC：<https://support.apple.com/guide/deployment/dep38df53c2a/web>
- iTerm2 General Preferences：
  <https://iterm2.com/documentation-preferences-general.html>

安装器应：

1. 检测 Automation capability；
2. 在用户首次启用 iTerm2控制时发起最小 Apple Event以触发系统授权；
3. 授权被拒时将能力降级为只读或 tmux-only；
4. 不修改 TCC 数据库，不声称通过 iTerm2 配置即可自动授权。

### 13.3 普通 tty

daemon 无法跨 macOS/Windows可靠接管任意已存在 tty。只有 wrapper 持有
PTY/ConPTY 控制端时，才能保证输入和原位 restart。

### 13.4 Windows Terminal

`wt.exe` 可以创建 tab，但不能作为通用接口可靠控制任意已有 tab的输入。
跨平台可靠方案是由 amcodex/amclaude wrapper 持有 ConPTY和子进程，
Windows Terminal仅作为显示前端。

0.17.0 对普通 tty、Windows Terminal 和未由 wrapper 持有控制端的 ConPTY
显式报告 `can_send_text=false`；compact/clear/handoff 因此 fail closed。
不会尝试 `wt.exe`、未授权键盘模拟或根据操作系统猜测输入能力。ConPTY
控制端实现与 Windows 真机验收作为延期项。

## 14. 本机 API、权限和持久化

- macOS：Unix domain socket，文件权限仅当前用户。
- Windows：Named Pipe，ACL仅当前用户 SID。
- 不监听 LAN；AMBridge/save-money 调用本机 CLI 或受保护的本机 API。
- 每个动作带 request ID、目标 instance ID、TTL和幂等键。
- 外部请求必须命中当前实例；旧实例请求不得作用于新会话。

建议目录：

```text
~/.agent-meeting/control/state.db
~/.agent-meeting/control/am-ctld.log
~/.agent-meeting/control/actions.jsonl
~/.agent-meeting/control/run/
```

日志至少记录：

- 会话发现、丢失和能力变化；
- 状态来源、状态和置信度；
- 规则命中与未执行原因；
- pause/resume token；
- 动作开始、验证、超时、失败和恢复；
- exit/restart 的旧新 instance 与 PID；
- daemon 启停和升级。

不得记录消息正文、认证 token、完整环境变量或未经脱敏的命令行。

## 15. CLI

```text
am-ctl status
am-ctl status --json
am-ctl start
am-ctl stop
am-ctl restart
am-ctl update
am-ctl help

am-ctl agent --name NAME --proj PROJECT --cmd status
am-ctl agent --name NAME --proj PROJECT --cmd compact
am-ctl agent --name NAME --proj PROJECT --cmd clear
am-ctl agent --name NAME --proj PROJECT --cmd handoff
am-ctl agent --name NAME --proj PROJECT --cmd exit
am-ctl agent --name NAME --proj PROJECT --cmd restart
```

行为约束：

- `status/start/stop/restart` 默认作用于 `am-ctld` 服务；
- `agent` 子命令只管理本机实例；
- `name + proj` 匹配多个实例时拒绝执行，不自动猜测；
- 外部指令立即进入本地队列，不等待 5 分钟扫描；
- `exit/restart` 返回前必须给出验证后的最终状态；
- `update` 使用原子替换和服务重启，升级失败时保留可回滚版本。

后续可增加 `--instance`、`--json`、`--timeout` 和显式 `--force`，但不应改变
当前已确认的基础命令面。

## 16. 用户级自启动

### 16.1 macOS

使用用户级 LaunchAgent：

```text
~/Library/LaunchAgents/ai.woodor.am-ctld.plist
```

不使用系统级 LaunchDaemon，因为后者不在用户 GUI session 中，无法可靠访问
iTerm2 Automation和用户终端。

### 16.2 Windows

使用当前用户 AtLogOn Scheduled Task并配置失败重启。不要使用运行在 Session 0
的 Windows Service管理用户的 Windows Terminal/ConPTY。

安装、升级和卸载复用 agent-meeting 现有的 macOS/Windows 用户服务抽象。

## 17. 实施阶段

### Phase 0：移除危险旧机制

- 删除全局 AGENTS.md/CLAUDE.md 自动 handoff managed block；
- 升级时清理已安装的旧 block，不能只停止未来写入；
- 删除 agent-meeting/amcodex 的 `control:*` 生成、渲染、说明和测试；
- 对历史未消费控制消息实施一次性安全丢弃。

### Phase 1：只读控制台

- 新建 `am-ctld`、`am-ctl`、本机 IPC、日志和服务安装；
- 发现 amcodex、直接 Codex、Claude monitor和无 monitor Claude；
- 状态与置信度判断；
- `am-ctl status` 和 `agent --cmd=status`。

### Phase 2：消息入口协调

- `am-codexd` pause/resume API；
- Claude monitor pause/resume IPC；
- pause token、实例校验和失败恢复；
- 验证暂停期间消息不 ack、不注入。

### Phase 3：非终止动作

- compact；
- clear；
- checkpoint；
- 动作验证、cooldown、规则引擎。

### Phase 4：终止动作与 wrapper

- handoff → draining；
- amcodex supervisor增强；
- amclaude supervisor/PTY/ConPTY实现；
- exit、同终端 restart；
- macOS iTerm2权限探测与Windows能力降级。

### Phase 5：外部接入

- save-money仅调用 `am-ctl`/本机 API；已完成；
- AMBridge生命周期接入不在本项目修改，留给 AMBridge 项目单独实施；
- 跨机器控制由各机器上的本地 `am-ctld` 执行，远端请求必须使用独立、
  有认证和TTL的控制协议，不复用 agent-meeting消息。

## 18. 关键验收标准

1. agent运行超过 1 小时或 20 轮不会自行 handoff。
2. 普通或历史消息无法触发 handoff、clear、exit、restart。
3. 会话 working时规则只记录，不执行。
4. pause成功后即使 thread转 idle，积压消息也不会注入。
5. resume后积压消息只投递一次，顺序不变。
6. unknown或低置信度会话不执行动作。
7. handoff完成后旧实例保持 draining，不恢复消息。
8. exit严格发送两次中断并验证退出；失败不会伪报成功。
9. restart确认旧实例退出后才启动新实例，并复用受支持的原终端资源。
10. 无 wrapper、无 monitor或直接 Codex按能力安全降级。
11. `am-ctld` 崩溃不会导致 `am-msgd` 或 `am-codexd` 同时退出。
12. macOS与Windows重启机器并登录后，`am-ctld`自动恢复运行。
13. working turn 的积压消息以有界摘要 steer，不再在首次 idle 时无上限渲染；
    steer 失败不 ack、不丢消息，并能回退到 idle 投递。
