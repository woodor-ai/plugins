---
name: init-proj
description: Scaffold a new Tommy-standard project. Creates <parent>/<name>/ with a git repo, the three tiered subagent profiles (explore/rd/planner), a .gitignore that keeps the local API key out of git, an interactively-entered project API key that never touches the transcript, and a launched agent-meeting director session. Rolls back the freshly-created directory on any failure. Use when starting a brand-new project from scratch (not for adding agents to an existing repo — use /init-agents for that).
user-invocable: true
allowed-tools:
  - Read
---

# /init-proj — 从零搭一个 Tommy 标准项目

**和 `/init-agents` 的区别**：`/init-agents` 只在**已有项目**里补 `.claude/agents/`；`/init-proj` 是**从零建一个新项目**（含目录、git、key、总监）。给已有项目加 agent 用 `/init-agents`，开新项目用 `/init-proj`。

**依赖**：本 skill 需要本机 `~/AIAgent/AMBridge` 存在 amp 包。若报 `No module named amp`，说明本机没装 AMBridge。

## 调用

```
/init-proj <name>
```

## 执行步骤（主 agent 按序跑）

1. **采集参数**：从参数取 `<name>`（正则 `^[a-zA-Z0-9-]{2,40}$`，没给就问一次，不要猜）。父目录默认 `~/AIAgent`，用户指定别的就用别的。总监会话名（`--wic-name`）默认等于项目名；若项目名超过 20 字符或不符合 `^[a-zA-Z0-9-]{2,20}$`，问一次要个符合规范的 ≤20 字符总监名。

2. **让用户跑这条命令**（把 `<name>` `<wic>` 替换成真实值；留空回车 = 跳过 key，走订阅登录）：

   ```
   ! read -rs -p 'ANTHROPIC_API_KEY (留空回车=订阅登录跳过): ' K && printf '%s' "$K" | ( cd ~/AIAgent/AMBridge && python3 -m amp.cli.newproject --name '<name>' --wic-name '<wic>' --parent ~/AIAgent --tui claude ) ; unset K
   ```

   key 经 `read -rs` 静默读入、管道喂给 CLI stdin、跑完 `unset`，**不经过对话或任何命令行参数**。主 agent 不得用 Bash 帮用户输 key，不得把 key 放进任何参数，不得回读 `settings.local.json`。

3. **解读 CLI 输出的单行 JSON**：
   - `{"ok": true, ...}`：一行报告——项目路径（`path`）、key 写了还是跳过（`keyWritten`）、总监 `<wic>` 已起。
   - `{"ok": false, "error": "..."}`：把 `error` 报给用户；CLI 已自动回滚，目录无残留。

## Windows 说明

CLI 的 director 启动走 Mac osascript → iTerm2 路线；Windows 暂未支持，在非 Mac 机器上跑到起 director 这步 CLI 会报错并自动回滚。TODO：Windows 版待做。
