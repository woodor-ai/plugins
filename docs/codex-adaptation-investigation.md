# Codex 适配调研 — 测试、分析与结论

文档日期：2026-06-17 03:56 PDT（初版）；05:35 PDT（重测更正）；08:xx PDT（落地完成）
分支：`codex-adapt`（含 handoff 0.1.7 配方 + save-money 0.1.3 A-1 + 本文档）
状态：**落地完成。handoff Codex 配方 + save-money auto-handoff 已建并测；text-truncate/image-delegate 已文档/源码级证伪；init-agents 无目标物。唯一待决=是否并 main。**

> ⚠️ 初版（03:56）结论"平台层阻断、四插件全死、建议暂停"是**错的**，已被 05:35 的实测推翻。错因见 §3 更正记录——初版只测了"插件包内自带 hook"（`plugin_hooks=removed`）+ 那条坏掉的 bypass 捷径，从没测用户级/项目级 `~/.codex/hooks.json` 的合法信任路径。Tommy 当时不买账、要求查官方文档+实测点火，是对的。

---

## 1. 目标

让本仓 4 个插件（agent-meeting / handoff / init-agents / save-money）做到"一套代码、双宿主"——同一个下载目录同时被 Claude Code 和 OpenAI Codex 加载，源码尽量单份共享、运行时按环境变量分流。补充框定：分发不是障碍（安装脚本能 merge-write `~/.codex/hooks.json`），**唯一真问题是能力**——hook 能否 fire、payload 给不给到要的数据。

---

## 2. 跨平台（Mac/Windows）——✅ 早已完成

4 个插件全部已支持 Mac/Windows（agent-meeting 三平台分支 + launchd/schtasks 双持久化；save-money 全走 expanduser+gettempdir；handoff 纯 pathlib；init-agents 无平台专属代码）。这条不是瓶颈。

---

## 3. Codex hook 能力 —— 实测复核（核心）

### 3.1 初版的错（更正记录）
初版用 `codex features list` 看到 `plugin_hooks=removed`，又把"插件包内 hook"点火失败，外推成"hook 死路"。两个硬伤：
1. 只测了**插件包内自带 hook**（`.codex-plugin` 携带），那条确实被移除；**从没测用户级 `~/.codex/hooks.json` / 项目级 `<proj>/.codex/hooks.json`**——而 `hooks` 功能本身是 `stable=true`，是另一条代码路径。
2. 尝试点火用的是坏掉的 `--dangerously-bypass-hook-trust`（0.134.0 该 flag 没透传到 app-server 的 bug），不是合法信任路径。

### 3.2 官方文档
`https://developers.openai.com/codex/hooks` 返 200，真实存在。事件含 SessionStart / PreToolUse / PostToolUse（matcher 支持 Bash/Read/Write/Edit/MCP）等。hooks.json 格式：`{"hooks": {"<Event>": [{matcher, hooks:[{type:"command", command}]}]}}`，与 Claude Code 同构。

### 3.3 合法信任机制（实测，codex-cli 0.134.0）
两层都要写进 `~/.codex/config.toml`：
1. 项目信任：`[projects."<绝对路径>"] trust_level = "trusted"`（精确路径匹配、不继承子目录）。
2. 每个 handler 的 trusted_hash：对 handler identity（`event_name` snake_case + `hooks` 数组含 `async/command/timeout/type` + `matcher`）做 canonical-json（键排序、`separators=(',',':')`）的 sha256，写 `[hooks.state."<...key...>"] enabled=true, trusted_hash="sha256:<hash>"`。command 一改 hash 即失效，要重算。

### 3.4 点火实测 —— 三事件全 FIRE
项目级 `.codex/hooks.json` 配 SessionStart/PreToolUse/PostToolUse，handler 落盘 stdin。配齐信任后起 `codex exec` 会话：**三个事件实测全部 fire**（/tmp 落盘文件为证），不是推断。

### 3.5 payload 能力（实测，真 payload）
- **token/上下文占用%**：payload **没有**任何 cost 字段。三事件 payload 仅含 `session_id`/`turn_id`/`transcript_path`/`cwd`/`hook_event_name`/`model`/`permission_mode`，Pre/Post 多 `tool_name`/`tool_input`，Post 多 `tool_response`。
- **PreToolUse 改写工具入参**：✅ 实测生效（`updatedInput` + `permissionDecision:"allow"` 两者缺一不可）。
- **PostToolUse 改写工具输出**：❌ 字段 `updatedMCPToolOutput` 存在但源码标 unsupported、实测无效，只能观测。
- **SessionStart 注入上下文**：✅ 实测生效（`additionalContext`，模型后续引用了注入文本）。

---

## 4. 逐插件可移植性结论（基于实测能力）

| 插件 | 能否走 hooks.json 配方 | 依据 |
|---|---|---|
| **handoff** | ✅ **能做** | SessionStart 实测 fire + `additionalContext` 注入实测生效 + payload 带 `cwd`/`transcript_path` → 读 handoff-pending 再注入会话上下文，核心齐了。脚本写 `~/.codex/hooks.json`（含 trust）即可。 |
| **save-money** | ⚖️ 三机制分裂：auto-handoff ✅ 已做；truncate/image ❌ 不可行 | **auto-handoff ✅**：payload 无 token 字段，但 transcript JSONL 有 `event_msg.token_count.info`（`total_token_usage.input_tokens`+`model_context_window`），挂 PostToolUse 读它算占用%，离线验证 18.41%/5.65% 算对，已落地（0.1.3）。**text-truncate ❌**：官方文档明示 `updatedMCPToolOutput` unsupported、源码无 `updatedToolOutput`，hook 改不了输出；且 Codex 原生 `tool_output_token_limit`（默认 12000）已干这事，做也多余。**image-delegate ❌**：源码级 hook 只对 Bash/unified_exec/apply_patch/MCP fire（issue #20204），图片工具全在外，拦不到。 |
| **init-agents** | ⚖️ 部分支持（已建 0.1.4） | **更正**：Codex 2026/3 GA 了 subagents，`.codex/agents/*.toml`（含 `developer_instructions`/`model`/`model_reasoning_effort`/`sandbox_mode`）对标 `.claude/agents/*.md`。三档已能生成（explore=gpt-5.4-mini/low、rd=gpt-5.4/high、planner=gpt-5.5/high）。**局限**：`spawn_agent` 暂不能按名调度自定义 subagent（issue #14039 open），profile 可加载但主 agent 只能用通用 agent_type 派、不能按名 dispatch。 |
| **agent-meeting** | —（不走 hook） | 走 amp 进程外驱动（Monitor 工具弹来电 Codex 无等价物）。amp PR5 已验证外驱起 Codex agent 跑通真实 turn。 |

---

## 5. 结论与建议

- **不是"插件 hook 全死"**：Codex 用户级/项目级 hook 实测好好的（fire + 入参改写 + 上下文注入都生效），分发也不是问题。初版的平台级"死路"判断已推翻。
- **已落地（均在 codex-adapt 分支）**：
  - **handoff 0.1.7**：Codex SessionStart 配方 + 安装脚本，实测 codex exec 下 fire、additionalContext 注入有效。
  - **save-money 0.1.3**：auto-handoff（A-1）落地，PostToolUse 读 transcript token_count 算占用%、additionalContext 注入交接提示，28 测试全绿、未碰 ~/.codex。
  - **init-agents 0.1.4**：生成 `.codex/agents/{explore,rd,planner}.toml` 三档 profile（model/effort/sandbox_mode 映射 codex debug models 实查）。局限：按名调度待 #14039。
- **已证不可行（硬证据，文档/源码级）**：save-money 的 text-truncate（官方文档明示改输出 unsupported；Codex 原生 `tool_output_token_limit` 已覆盖需求，做也多余）、image-delegate（源码级 hook 只对 4 类工具 fire，图片工具全在外，issue #20204）。
- **init-agents 更正**：初版/中途判"无目标物"是错的——Codex 有 `.codex/agents/*.toml`，已建（见上）。
- agent-meeting 仍是 amp 外驱，与本 hook 路线无关。
- **唯一待决**：codex-adapt 分支是否并 main（含 handoff 0.1.7 + save-money 0.1.3 + 本文档）。并之前建议先清/squash 分支历史（含早期 plugin-bundled 试点的死代码，后续已弃）。

---

## 6. 晚点继续时的接手点

1. **并 main 决策**：codex-adapt 已含 handoff 0.1.7（`handoff/codex/install-codex-hook.py`）+ save-money 0.1.3（`save-money/codex/install-codex-hook.py` + auto-handoff.py 宿主分流）+ 本文档。要并 main 前先清分支历史（早期 `411ada6` 加的 plugin-bundled 试点死代码，后续 commit 已删 `.codex-plugin/`，建议 squash 成干净的 feature 提交）。
2. **trusted_hash 计算是脆点**（两个安装脚本都已收口）：command 一改 hash 即失效、hook 静默不 fire。脚本每次按当前 command 重算覆盖，免人工维护。canonical-json 公式见 §3.3。
3. **测试纪律**：任何 fire-test 绝不碰 Tommy 真实 `~/.codex/config.toml`（曾被污染、已清，备份 `~/.codex/config.toml.pre-cleanup-bak`）。优先离线喂真实 transcript 验证。
4. **auto-handoff 已知小限制**：挂 PostToolUse，纯对话无工具调用的会话不会触发检查（agentic 会话工具频繁、实际够用）。若要更可靠可探 UserPromptSubmit（未 fire-test）。
5. **truncate/image 别再回炉**：已文档/源码级证伪（§4 save-money 行）。除非 Codex 放开 PostToolUse 改输出 + 扩大 hook 工具覆盖，否则无解。
6. 复查命令：`codex features list | grep -E 'plugin_hooks|hooks'`（确认 `hooks` 仍 stable）。

---

## 7. Windows 现状 —— 未验证，已交 plugins-win

本调研与全部实测**只在 Mac（OMI-MacDev）做**。Windows 上 Codex 的支持程度**未验证**，且有两类已知风险：

1. **安装脚本是 Mac/Linux-only（确定 bug，待修）**：`handoff/codex/install-codex-hook.py` 与 `save-money/codex/install-codex-hook.py` 写死 `HOOK_COMMAND = f"python3 {脚本}"`——Windows 上 `python3` 常不存在（需 `py -3`/`python` 三段 fallback，Claude 侧 hooks.json 已有此 fallback，Codex 安装脚本未带）。且路径嵌进 TOML 命令字符串时 Windows 反斜杠需转义，脚本未处理。
2. **Codex 自身 Windows 运行时未验证**：`codex features list` 里 Windows 沙箱相关 flag（`experimental_windows_sandbox`/`elevated_windows_sandbox`）为 `removed`——init-agents 三档用的 `sandbox_mode`（only-read / workspace-write）在 Windows 能否照常生效存疑；hook 在 Windows Codex 上是否 fire 也未测。

平台中立、无 Windows 风险的部分：`.codex/agents/*.toml` 内容、save-money 读 transcript JSONL 算占用%（纯文本/数据逻辑）。

**归属**：Windows 全部工作交 plugins-win（D:\AIAgent\plugins）。基准在 main。

### 7.1 sandbox_mode 三档 Windows 实测结论（plugins-win，codex-cli 0.140.0）

本节为 plugins-win 实测后补入，标记 ✅/❌ 均为实测结果，非推断。

| 档位 | sandbox_mode | Windows 上实测结果 |
|---|---|---|
| explore / planner | `read-only` | ✅ 实测挡写，隔离有效；不依赖全局 config |
| rd | `workspace-write` | 依赖全局配置（见下）；不配则退化只读 |

**rd 档写文件的必要条件**：用户全局 `~/.codex/config.toml` 必须配：

```toml
[windows]
sandbox = "unelevated"
```

不配则 workspace-write 在 Windows 上退化成只读，rd 写不了任何文件——profile TOML 里的 `sandbox_mode = "workspace-write"` 本身不够。

**两个可选值的取舍**：
- `unelevated`：免管理员、不弹 UAC、实测能写。✅ 推荐。
- `elevated`：需管理员 UAC，且撞 codex 0.140 已知 bug（helper `codex-windows-sandbox-setup.exe` 找不到，ShellExecuteExW error 1223，openai/codex#28457）。❌ 不推荐。

**概念澄清**：`sandbox_mode` 是"限定 agent 只能写工作区"的护栏，不是写权限总开关。护栏在 Windows 需系统层配合（`[windows]sandbox`），Mac/Linux 无此问题，三档直接生效。
