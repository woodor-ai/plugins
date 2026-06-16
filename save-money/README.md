# save-money

三个成本控制 hook 打包在一个 marketplace 插件里，统一由 `~/.claude/cost-opt.json` 开关控制（PWA Save Money 页面写入）。

## 安装

本插件靠 marketplace 分发，`/plugin install save-money` 后 hook 由 `hooks/hooks.json` 自动注册到 Claude Code。开关统一在 `~/.claude/cost-opt.json`（由 AMBridge PWA Save Money 页面写入）。

---

## Hook 1：auto-handoff（Stop hook）

**作用**：每次 Stop 事件读取 transcript 里最后一条 assistant 消息的 usage，若 context token 数超过配置阈值则向 AMBridge 写触发文件，由 AMBridge 发起 handoff 重启。

**读 cost-opt.json 哪段**：
```json
{
  "auto_handoff": {
    "enabled": true,
    "thresholds_pct": {
      "opus": 60,
      "sonnet": 70,
      "haiku": 80
    }
  }
}
```

**默认值**：未配置或 `enabled` 非 `true` 时静默退出（不触发）。

**关键行为**：
- 窗口大小按 family 固定：opus/sonnet 各 1M token，haiku 200k。无法从 Stop hook stdin 读取，按 family 断言。
- 100k token 绝对下限（`MIN_FIRE_TOKENS`）：即使 pct 阈值算出来的绝对值低于 100k，也不在 100k 以下触发，防止新 session 基线即超阈值导致死循环重启。
- dedup：同一 `session_id` 只触发一次，靠 `~/.cache/cost-auto-handoff/fired/<session_id>` 标记文件去重。
- 触发文件原子写入 `~/.ambridge/handoff-triggers/<agent>.json`（先写 .tmp 再 rename）。
- 依赖 `meeting list` 解析当前 agent 名；找不到或有歧义时静默退出。

---

## Hook 2：truncate-output（PostToolUse hook，matcher: Bash|Read）

**作用**：当 Bash stdout 或 Read 文本内容超过阈值字符数时，替换为 head + 指针行 + tail 摘要，完整内容存 `/tmp`，避免大输出赖在主 agent 上下文每轮复读。

**读 cost-opt.json 哪段**：
```json
{
  "text_truncate": {
    "enabled": true,
    "threshold_tokens": 25000
  }
}
```

**默认值**：配置缺失或 `enabled` 非 `false` 时**默认开启**，阈值默认 25000 token（≈100k 字符，1 token ≈ 4 字符）。只有显式 `enabled: false` 才关。

**关键行为**：
- 图片输出（`isImage: true` 或 content block 含 `type: image`）**永远放行**，不截断，避免破坏 base64 数据。
- Bash：截 stdout，其余字段（stderr/interrupted/isImage）原样保留。
- Read：tool_response 是字符串时直接截；是 content block 列表（图片）时放行。
- 其他工具结构未知，一律放行。
- 截断后 head 5k token + 指针行（含 /tmp 完整路径）+ tail 3k token。

---

## Hook 3：image-delegate（PreToolUse hook，matcher: Read）

**作用**：拦截主 agent 对图片文件的 Read，deny 并提示改派 explore subagent 读图、回文字结论。subagent 读图永远放行（靠 `agent_id` 字段区分），防止死锁。

**读 cost-opt.json 哪段**：
```json
{
  "image_delegate": {
    "enabled": true
  }
}
```

**默认值**：配置缺失或未显式 `enabled: true` 时**默认关闭**（opt-in）。这个 hook 拦截所有主 agent 图片读，影响面大，必须主动开启。

**关键行为**：
- 识别扩展名：`.png .jpg .jpeg .gif .webp .bmp .svg`（大小写不敏感）。
- `/tmp/amb-shot` 前缀路径豁免：AMBridge 自检截图需要主 agent 亲自读像素，硬编码 allowlist，不放进 cost-opt.json（amp 会覆盖该文件）。
- subagent 检测：stdin 含 `agent_id` 字段即视为 subagent，放行。
- deny 时返回中文提示，引导改派 explore subagent。
