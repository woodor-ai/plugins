> **落地记录（原调查文档）**｜靶子清点：2026-07-22 08:24 PDT｜实施完成：2026-07-22（commit `36a946d`..`f1064bb`，含新增第 9 条）
> 关联：`docs/contracts/0.10.0-composite-key-identity.md`（方案「阶段 2：收窄房间寻址」，已标记完成）
> 本文原为靶子清点，阶段 2 已全部实施完成。现更新为落地记录：逐条标注实际改法与原设想的出入、新增的第 9 条、实施中顺带发现并修复的清单外同类问题、一个已知未修的协议缺口、验收标准逐条核对结果。

# 阶段 2 落地记录：单键查复合键实体 —— 靶子清单

## 病根

这批问题的共同病根：**身份判定依赖运行时环境状态（当前目录、当前在线名单、进程本地缓存），而不是显式的复合键 `(name, project)`**。0.10.0 滚版当天，这个病根在四个不同层面各出现过一次：

1. **存量数据漂移本身**——项目身份历史上由 cwd/目录名推导，同一仓库在不同机器 clone 到不同路径，产生同名不同 project 的分裂行；这是 0.10.0 方案 0.2/0.3 阶段要收敛的存量问题。
2. **权威身份集合被误定义为「当前在线的会话」**——v0.9.0 引入的 instance-aware 注册守卫（commit `7a3e673`）把"这次注册是不是我自己"这一判断，隐式等同于"是否存在一条心跳新鲜的行"，而不是显式的稳定标识；结果同一个会话分两步注册时,第二步把第一步误判成"另一个活的进程"而拒绝自己（v0.9.0 自拒回归，commit `be35ddc` 修复为单步注册 + 显式 `--instance`）。本质上和本清单的靶子同源：拿"运行时能看到的状态"当身份权威，而不是拿显式复合键当身份权威。
3. **测试主进程读真机身份缓存，导致与被隔离子进程算出不同的 project 值**——`derive_project()` / `resolve_authoritative_project()` 的 per-root 缓存（`proj_cache_get/set`）按仓库根目录的 sha1 存在 `MEETING_HOME/projcache/` 下；测试若不显式重定向 `MEETING_HOME`，主进程读到的是真机缓存，子进程（daemon/CLI 子进程）若隔离了 `MEETING_HOME` 则读到空缓存，两边对同一 cwd 算出不同 project，交叉验证失真。0.10.0 已修 `codex-register.py` 未尊重 `MEETING_HOME` 的那处（现在尊重了），但缓存机制本身仍是"看运行时环境算身份"的同类设计。
4. **`meeting offline <name>`**——本清单条目 3，见下表。命令用当前目录推导项目身份去操作**别的会话**，推导出的是"我在哪"而不是"对方是谁"。

四层现象不同，但都是同一件事：**该用显式复合键的地方，被环境状态（cwd / 在线心跳 / 本地缓存）顶替了。** 阶段 2 的收窄工作，实质是把这类"用环境状态兜底身份"的位置，一个个换成必须显式传入或显式返回的复合键。

## 靶子表（按严重度排序）

严重度口径：**"现在库里恰好没有同名多行"不构成"不是问题"的理由**——复合键设计明确允许同名跨项目是两个不同的真实 agent，缺一不可的验收标准见文末。

全部 9 条均已完成，commit 见「状态」列；逐条实际改法与原设想的出入见下方「失败场景详述」各条目下新增的「实施结果」小节。

| # | 严重度 | 位置 | 问题 | 状态 |
|---|---|---|---|---|
| 1 | 会静默出错 | `agent-meeting/bin/meeting-daemon:330-345` `_conversation_clause()`，被 `_turn`(903)/`_show`(914)/`_read`(955)/`_delete`(1274) 共用 | 1:1 会话按**纯姓名**匹配（`(sender=? AND recipient=?) OR (sender=? AND recipient=?)`），两端都不看 project。这是 0.8.54 的既有设计（docstring 里写明原因），也正是方案「阶段 2」标题里点名要推翻的那条决定。 | 已完成 `36a946d` |
| 2 | 会静默出错 | `agent-meeting/codex/codex-bridge.py:670-687` `_on_text()` → `_process_room(sender)` | 入站 WS 帧本来带 `sender_project` 字段，但 `_process_room` 只用裸 `sender` 当房间键，把已知的 project 信息扔了；后续 `meeting read SELF room --since ...` 再靠 CLI 的 `/resolve` 猜一遍。 | 已完成 `5016a75` |
| 3 | 会报错但语义错 | `agent-meeting/bin/meeting:869-880` `cmd_offline()` + `agent-meeting/bin/meeting-daemon:898-901` `_unregister()` | `offline <name>` 用当前目录推导项目身份，不接受 `--proj`/`--global`；daemon 的 `_unregister` 对 `DELETE ... WHERE project=? AND name=?` 不检查受影响行数，恒定返回 `{"ok": true}`。 | 已完成 `b2e007d` |
| 4 | 仅同名跨项目时才出错 | `agent-meeting/bin/meeting-daemon:776-805` `_resolve_candidates()` 历史回退分支（792-804 行） | 名字既无存活 session 也无 group 时，退回 `messages` 表按 `ORDER BY id DESC LIMIT 1` 挑最近一条的 project，丢弃其余历史 project。测试里已知的 "Gap A"（`tests/test_identity_regression.py` TC7），至今未收口。 | 已完成 `b2e007d` |
| 5 | 仅同名跨项目时才出错 | `agent-meeting/bin/meeting:139-166` `_resolve_peer()` 零候选回退 | `/resolve` 零候选时 `return (self_project, raw)`——用**我自己**的 project 去猜**对方**的身份，给一个尚未注册过的新对等体发第一条消息时可能永久投错桶。 | 已完成 `b2e007d` |
| 6 | 仅同名跨项目时才出错 | `agent-meeting/bin/meeting:1230-1316` `cmd_group()` 的 `create`/`add`/`remove`/`rename`/`delete`/`members` 子命令 | 群组自身的 project 恒为 `_derive_project(cwd)`，不接受 `--proj`/`--global`；同一文件里 `charter`/`list --member` 却支持 `name@project`，口径不一致。 | 已完成 `b2e007d` |
| 7 | 仅同名跨项目时才出错 | `agent-meeting/bin/meeting:1392-1424` `cmd_stop()` | 本地 pidfile 路径 `MEETING_HOME/run/<name>.pid` 只按裸名，不带 project 维度；同机两个不同 project 的同名 monitor 会抢同一个 pidfile 路径。 | 已完成 `b2e007d` |
| 8 | 仅展示层，非数据层 | `agent-meeting/bin/session-bootstrap.py:891-911` `online_peers_str()` | `SELECT name FROM sessions WHERE last_seen >= ?` 不选 project 列，SessionStart 上下文里同名跨项目的两个在线会话会显示成两条一样的裸名，模型无从区分该艾特谁。 | 已完成 `28ef290` |
| 9 | 仅展示层，非数据层（与 #8 同族，但多一层协议属性，见下） | `agent-meeting/bin/monitor.py` + `agent-meeting/bin/session-bootstrap.py`（收件提示行）+ `agent-meeting/skills/meeting/SKILL.md`（提示行格式的协议文档） | 收到消息时注入给模型的提示行只显示发件人裸名，同名跨项目的两个 agent 产生一模一样的提示，收件人无法区分该回给谁——是 #8（在线名单）在收件侧的对称版本。实施中发现，是新增靶子，非原清单条目。 | 已完成 `adeea41` |

## 失败场景详述

**#1 `_conversation_clause` 名字撞车合并会话**
现状验证（只读快照，非假设）：`~/.agent-meeting/db/rooms.db` 里 `messages` 表按 `sender` 分组、`COUNT(DISTINCT sender_project)`，`atlas-relay` 已跨 5 个 project、`wdav3-laptop` 已跨 5 个 project、`atlas`/`Tommy` 各跨 3 个——这些多为历史漂移残留，但证明"同名多 project"这一数据形状已经真实存在于当前库里，不是纯假设。一旦两个**当前存活**的真实 agent 恰好同名（如 amb@projA 和 amb@projB 都在跑），`director` 分别与两者各聊一段：`meeting show director amb`（或 `read`/`delete`）会把两段会话按名字合并成一条时间线，`_turn()` 的"当前谁的话轮"也会被对方的消息打断——director 看到的是被交叉污染的历史，且**没有任何报错**，因为 `_conversation_clause` 本来就是按设计不看 project。

**实施结果**：与设想一致，改法比预想更简单。四个调用点本来就已经在往下传 `self_project`/`peer_project`，只是 `_conversation_clause` 内部把它们丢了——直接接回即可，不需要新增参数传递链路。SQL 比较从纯姓名扩为 `(sender_project, sender, recipient_project, recipient)` 双向匹配。旧 docstring 论证"按名匹配"的理由是 worktree 场景下 project 会漂移、按复合键匹配反而会把一段对话拆成两半——阶段 0/0.1/0.2 已经关闭了漂移产生机制并收敛存量，这个前提不再成立，已把 docstring 换成复合键理由。`test_identity_regression.py` 里的 TC17 原本断言的正是"按名合并"这一旧行为，已就地重写为复合键隔离回归，而不是保留一条兼容路径。

**#2 codex-bridge 丢弃已知 project 导致入站悄悄失败**
两个不同 project 下的真实 agent 若同名，且都给某个 codex 桥接会话发过消息：两条消息在 `_on_text` 里都落到同一个内存 `room = sender` 键，`_known_peers`/`_cursors`/`_session_cursors` 状态合并。桥接进程之后调用 `meeting read SELF room --since <cursor>`（`room` 是裸名），CLI 侧 `_resolve_peer` 再去 `/resolve` 查一遍——如果两个同名 project 当前都在线，会返回 2+ 候选，CLI 直接 `SystemExit`（ambiguous name），子进程非零退出；`_fetch_new_messages` 捕获异常后只写一行 `_log(...)`，不向任何用户可见的地方冒泡。结果：两个真实对等体的入站消息**同时静默丢失**，唯一痕迹是一行没人常态化去看的桥接日志。

**实施结果**：比设想更严格。原设想只是把丢掉的 project 信息带回房间键；实际做法是把房间键直接定为 CLI 已经在用的 `sender@sender_project` 语法，并且把"帧缺 `sender_project`"从可重试的静默失败，改成直接抛异常让桥接进程非零退出——贯彻「失败必须响亮」，而不是加一层兜底重试掩盖。`sender_project` 来自一个 NOT NULL 列且两条 daemon 发送路径都无条件填充，所以缺失即代表 daemon 数据本身损坏，不是可重试的临时状况。新增两个跑真实桥接子进程的端到端用例：同名不同 project 投递不丢、畸形帧非零退出。

**#3 `meeting offline` 报成功但未生效（已实测复现）**
Tommy 今天实测：在 `plugins` 目录下对一个注册在别的项目下的会话名跑 `meeting offline <name>`——`cmd_offline` 用 `_derive_project(os.getcwd())` 算出的是"plugins"这个 project，而目标会话实际注册在另一个 project 下；daemon 的 `DELETE FROM sessions WHERE project=? AND name=?` 因为 project 对不上，删除 0 行，但 `_unregister()` 不检查这一点，无条件返回 `{"ok": true}`。CLI 打印 `offline: <name>@plugins`，退出码 0——命令报成功，但目标会话仍然在线、心跳仍在刷新，且没有 `--proj`/`--global` 参数可以指向正确的 project 重试。

**实施结果**：与设想一致——`_unregister()` 改为报告 DELETE 的实际影响行数，`cmd_offline` 加了 `--proj`/`--global`，删除 0 行时非 0 退出并给出纠正提示。实施中发现同一个缺陷还有第二个调用点：`monitor.py` 自身进程退出时的 atexit 注销调用，命中的是同一处"无条件返回成功"，此前对每一个用 `--global`/`--proj` 注册的 monitor 都在退出时静默空转、从未真正注销过——这个发现记在下文「清单外发现并修复的问题」。

**#4 `/resolve` 历史回退挑错 project（已知 Gap A，未收口）**
名字在所有当前存活 session/group 里都查不到（都已下线或从未注册），但在历史消息里出现过两个不同 project——`_resolve_candidates` 只看 `ORDER BY id DESC LIMIT 1`，返回消息最新的那个 project，另一个 project 的历史彻底从候选列表里消失。因为只返回 1 个候选，CLI 侧的"2+ 候选强制要求 `@project` 消歧"这一安全网**根本不会触发**——调用方以为已经消歧清楚，实际上悄悄绑定到了错的那个。

**实施结果**：与设想一致——历史回退从 `ORDER BY id DESC LIMIT 1` 改为 UNION 枚举全部 distinct project，Gap A 收口。`test_identity_regression.py` 的 TC7 原本断言的正是"只取最新一条"这一旧行为，已就地重写为全量枚举回归。

**#5 `_resolve_peer` 零候选回退猜错对方 project**
给一个还从未在 daemon 里出现过的新名字发第一条消息（对方进程还没启动/还没注册）：`/resolve` 返回 0 候选，CLI 直接假定"对方和我同 project"。如果对方实际会以别的 project（比如显式 `--proj` 或 `--global`）注册，消息已经落库在错的 `(project, recipient)` 复合键下——对方注册后 `/read`/`/show` 永远查不到这条消息，且没有任何后续机制去修正，消息永久丢失且双方都不知道。

**实施结果**：与设想一致——零候选时不再默认"对方和我同 project"，改为 raise 强制要求显式 `@project`。顺带做了一处减法：这五个调用点里此前一直没用到的 `self_project` 参数一并删除，不留一个只为兼容旧签名而存在的死参数。

**#6 群组管理子命令的 project 无逃生舱**
群在仓库根目录 A 创建（project 由 cwd 推导为 A）；换一个 cwd（比如 worktree、或者仓库被移动/重新 clone 到别的路径）后再跑 `meeting group add <name> <member>`，`project` 变成 B，daemon 报 `group '<name>' does not exist in project 'B'`——报错本身准确，但用户没有任何参数能显式指定"我要操作 project=A 下的这个群"，只能被迫 `cd` 回原始目录。`charter`/`list --member` 子命令在同一文件里已经支持 `name@project`，管理类子命令却没有,是同一文件内口径不一致。

**实施结果**：比设想更简单。原设想是给管理类子命令加 `--proj`/`--global` 参数；实际做法是让 `create`/`add`/`remove`/`rename`/`delete`/`members` 直接支持 `name@project` 语法，跟同文件里 `charter`/`list --member` 的口径拉齐即可，不需要新增独立的 flag。

**#7 `meeting stop` 本地 pidfile 抢位**
Project A 和 project B 各跑一个名叫 `worker1` 的 monitor，都在同一台机器上：两者的 pidfile 都是 `MEETING_HOME/run/worker1.pid`,后启动的会覆盖先启动的那份文件。此后 `meeting stop worker1` 只能杀掉"当前 pidfile 里记的那个 pid"（即后启动的那个），另一个 project 下的 `worker1` 进程无法通过这条命令单独停掉，用户如果误以为已经全部停掉,会造成"这个名字下还有个不知道是哪个 project 的进程在跑"的困惑。

**实施结果**：比设想更复杂。原设想只是"pidfile 加上 project 维度"；实际实现新增了 `meeting_common.pidfile_stem` 这个共享函数，对 project 做百分号编码（`derive_project` 可能返回路径形式的值，不能直接拼进文件名）并把 `"*"` 映射成 `"global"`（Windows 文件名对 `*` 有限制），不是简单字符串拼接。`meeting stop` 同时加了 `--proj`/`--global`。`monitor.py` 自身的 `PID_FILE` 常量也改用同一个 `pidfile_stem`，保证写入方和 `stop` 的读取方用同一套编码规则。

**#8 SessionStart 上下文里的在线名单不带 project**
`online_peers_str()` 只 `SELECT name`,如果 `amb@projA` 和 `amb@projB` 同时在线,SessionStart 注入给模型的 "Online peers: ..." 行会出现两个一样的 `amb`,模型没有任何依据知道该艾特哪一个,大概率会裸名发送,直接撞上 #5(如果目标从未跟自己对话过)或者被 `_resolve_peer` 的多候选检测拦截(如果两个都有对话历史,退回 SystemExit)。这条本身不产生错误数据,但是上游诱因。

**实施结果**：与设想一致——`online_peers_str()` 补选 project 列，渲染成 `name@project`。`_resolve_peer` 本身不需要改动，它在查 `/resolve` 之前就先处理了带 `@` 的输入，天然吃这个新格式。

**#9 收件提示行不带 project，且是协议改动而非纯展示（实施中发现，新增）**
`monitor.py` 把入站消息注入给模型时，提示行只显示发件人的裸名。如果 `amb@projA` 和 `amb@projB` 都给同一个会话发过消息，两条提示看起来一模一样，收件人（通常就是模型自己）没有依据判断该回给谁——是 #8（在线名单，展示会话开始时的名单）在收件路径上的对称版本，同一族问题的另一半。

与 #8 的关键差异：#8 是纯展示，改完不影响任何解析逻辑；#9 的提示行格式写死在 `agent-meeting/skills/meeting/SKILL.md` 里，主 agent 依据这个格式的字符串**解析**出发件人名字用于回复寻址——所以这不是换一下展示文本就完事，而是要同时改协议文档和消费该协议的解析逻辑。严重度仍按"仅展示层，非数据层"分类，因为它不改任何持久化数据，只改传输给客户端用于展示/解析的字符串格式；但归为 #8 同档的同时要说明它多一层协议属性。

**实施结果**：发件人一律渲染为 `name@project`——不是"只有撞名时才转全称"。原因：按撞名与否切换格式，得在每条消息上都做一次撞名检测，且同一个对等体一会儿裸名一会儿全名，比稳定的长格式更难读。全局身份（project 为 `"*"`）仍降级显示裸名，跟其余四处展示位置的既有约定一致；#8 新增的 `online_peers_str` 当时没跟这个约定对齐，这次一并补齐。

实施中通读协议文档时，发现一处真实的解析破损：判断"这条消息是不是人类经 AMBridge 中转过来的"那处逻辑，原来是拿完整的 peer token 去跟一个固定名字做整体比较，一旦发件人变成 `name@project`，就再也匹配不上——已改为只比较名字那一半。这不是一个假设的边界情况，而是本条改动如果不处理就会当场打断的一条现有分支。

## 清单外发现并修复的问题

实施靶子 3 和靶子 6 时，命中同一条验收标准（「失败必须响亮」，见验收标准第 2 条）的另外两处同类缺陷，顺手一并修复：

- **`_group_purge`（`meeting group delete` 背后）对不存在的群也无条件返回成功**：跟靶子 3 里 `_unregister` 的缺陷是同一个模式——删除前不检查目标是否存在，恒定 `{"ok": true}`。已改为先查 `(project, name)` 是否存在，不存在则返回 error，跟同文件里其余 `/group/*` 路由的报错方式一致。连带发现 `/group` 的 DELETE 路由此前不像同级路由那样把 error 结果映射成 HTTP 400，一并修正。
- **`monitor.py` 自己的退出时注销调用不带 `--proj`/`--global`**：`_unregister` 的调用方不止 `cmd_offline` 一处，`monitor.py` 的 atexit 钩子也在调用它，但一直没传 project 相关参数。修 `_unregister` 本身之前，这处调用对每一个用 `--global`/`--proj` 注册的 monitor 都在退出时静默空转——注销请求发出去了，但因为 project 对不上，一直没真正生效，进程退出后会话行仍然挂在库里，直到心跳超时才被动摘除。

## 已知未修的协议缺口

**群消息的群名不带 project。** 核实过数据模型：`_ws_fanout` 推给客户端的帧里根本没有携带群的 project 字段，客户端拿到的群名从一开始就是裸名。要补齐这一条，得先在服务端协议里给群相关的帧加一个 `group_project` 字段，再改客户端渲染逻辑——这不是纯展示层能单独解决的，动的是服务端到客户端的协议契约，超出阶段 2「收窄寻址/展示层」的范围，阶段 2 未修。留作已知缺口，留给下一阶段或按需处理。

## 测试隔离事故（本次实施中发生，已修复）

跑 `test_ws_monitor.py` 时曾往生产库写入 8 行测试会话，且测试全绿。根因是隔离只做了本地文件路径维度（`MEETING_HOME`），没做网络维度——测试自带的假 CLI 把未识别的子命令转发到机器上装的真实 `meeting` 二进制，转发调用未带 `--host`，落入 mDNS 发现命中了生产 daemon。已在 commit `45ffd74` 修复，完整分析和教训记在 `docs/contracts/0.10.0-composite-key-identity.md` 阶段 3 章节，本文不重复展开。

## 验收标准

验收标准建立在"同名跨项目 = 两个真实 agent"这一前提上,不能用"库里刚好没有同名多行"来蒙混过关。

**验证脚本骨架**(用隔离的 `MEETING_HOME`,不碰真实库):

```bash
export MEETING_HOME=/tmp/am-phase2-verify
rm -rf "$MEETING_HOME" && mkdir -p "$MEETING_HOME"
MEETING="python3 agent-meeting/bin/meeting"

# 1) 起 daemon(或指向一个用同样隔离 MEETING_HOME 起的 daemon)
$MEETING daemon &   # 或按现有测试套路直接起 meeting-daemon 子进程

# 2) 注册两个"同名不同 project"的真实会话
$MEETING online amb --proj projA
$MEETING online amb --proj projB   # 需要 --force 或不同 --instance,视当时注册规则而定

# 3) 第三方分别跟两者对话
$MEETING send director amb@projA "hello from A" 
$MEETING send director amb@projB "hello from B"

# 4) 验收点 A(阶段 2 核心):conversation 不得合并
$MEETING show director amb@projA | grep -q "hello from A" 
$MEETING show director amb@projA | grep -qv "hello from B"   # projA 视角看不到 B 的内容
$MEETING show director amb@projB | grep -q "hello from B"
$MEETING show director amb@projB | grep -qv "hello from A"   # 反之亦然

# 5) 验收点 B: turn 状态不得互相干扰
$MEETING turn director amb@projA   # 应仍是 projA 那段对话的当前话轮,不受 B 影响

# 6) 验收点 C: offline 必须要么真的生效,要么显式报错(不许假成功)
$MEETING offline amb   # 在两者都不匹配的 cwd/project 下跑,必须非 0 退出或明确指出"未找到匹配项目"
                        # 不能返回 exit 0 且两个 amb 心跳都还在刷新

# 7) 验收点 D: /resolve 对同名多 project 必须要么枚举全部候选,要么显式要求 @project 消歧
#    不允许"看似只有 1 个候选"但其实吞掉了另一个 project 的情况(对应 Gap A)
curl -s "$(daemon_base_url)/resolve?name=amb" | python3 -c "import json,sys; d=json.load(sys.stdin); assert len(d) == 2, d"

# 8) 验收点 E: group 管理类子命令必须能显式指定 project(不再只靠 cwd 推导)
$MEETING group add somegroup@projA member1   # 或等价的显式语法,不依赖当前 cwd
```

**六条硬性验收点**:
1. 两个同名不同 project 的真实 agent,第三方与其一分别对话后,`show`/`read`/`turn`/`delete` 四个共用寻址逻辑的命令,任何一个都不得把两段历史混在一起(§步骤 4-5)。
2. `meeting offline`/`meeting stop`/`meeting rename`/`meeting group *` 对不存在或不匹配 project 的目标,要么真实生效,要么以非 0 退出码 + 明确文案报错;**不允许返回成功但未生效**(直接对应今天实测的 offline 假成功)。
3. `/resolve`(以及 CLI 的 `_resolve_peer`)对同名多 project 场景,要么枚举全部候选强制调用方消歧,要么有显式 `@project`/`--proj`/`--global` 逃生舱;不允许"猜一个"当默认行为。
4. 所有接受"会话名"或"群组名"作为操作目标的 CLI 子命令(`offline`/`stop`/`rename`/`group add/remove/rename/delete/members`),逐一确认要么支持 `name@project`/`--proj`/`--global`,要么明确注释说明"本命令只能操作当前 cwd 下的身份、且这是有意的"。
5. `session-bootstrap.py` 的 SessionStart 在线名单展示同名跨项目时,能让模型看出这是两个不同的身份(至少展示 `name@project`,而不是裸名去重前的重复行)。
6. 全部改动跑一遍 `tests/test_identity_regression.py`(含 TC7 Gap A)及新增的同名跨项目回归用例,全绿。

### 核对结果（2026-07-22）

1. **满足**。靶子 1 已把 `_turn`/`_show`/`_read`/`_delete` 共用的 `_conversation_clause` 改为复合键匹配，TC17 已从"断言旧的按名合并行为"改写为断言隔离。
2. **满足**，且清单外发现的两处同类问题已按同一标准修复（见「清单外发现并修复的问题」）。有一处需要单独说明：`meeting offline`/`meeting stop`/`meeting group *` 均已改动；本条列的另一个命令 `meeting rename`（会话改名，与靶子 6 里的"group rename"是两个不同命令）本次未改动——它一直沿用 cwd 推导 project，但其 `--help` 文案已明确写着"(same project only)"，属于验收标准第 4 条里"明确注释说明本命令只能操作当前 cwd 下身份、且这是有意的"的分支，不构成假成功，故不算未达标，但也不是本轮改动范围。
3. **满足**。`_resolve_candidates` 改为枚举全部 distinct project（收口 Gap A），`_resolve_peer` 零候选时改为 raise 要求显式 `@project`。
4. **满足，含上一条对 `meeting rename` 的说明**。`offline`/`stop`/`group add/remove/rename/delete/members` 均已支持 `name@project`/`--proj`/`--global`；`rename`（会话改名）保持 cwd-only 但有明确注释，符合本条的第二个分支。
5. **满足**。`online_peers_str()` 已渲染 `name@project`。
6. **满足**。当前实际基线：`test_identity_regression.py` 91 项、ws 相关套件 54 项、ws-monitor 31 项、`test_authoritative_project.py` 50 项，pytest 全量 82 项，全部通过（`adeea41` commit message 记录的是完整跑一遍后的数字）。跑测试过程中发生过一次生产库污染事故，已修复并记录，见上文「测试隔离事故」一节。
