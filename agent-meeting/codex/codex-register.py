#!/usr/bin/env python3
"""
agent-meeting: Codex SessionStart register hook. Cross-platform.

Invoked by codex's SessionStart hook (installed by install-codex-hook.py) when
a codex session starts on an app-server thread. It:

  1. Reads the SessionStart payload from stdin (JSON). Fields observed on
     codex 0.140.0 (Windows app-server, 2026-07-07):
       {session_id, transcript_path, cwd, hook_event_name, model,
        permission_mode, source}
     `session_id` == the thread id (thread.sessionId == thread.id), i.e. the id
     the bridge daemon uses to `thread/resume`.
  2. Reads runtime config `~/.agent-meeting/codex/runtime.json`, written ahead
     of time by the launcher (codex-meeting.py's Launcher.setup()). Shape:
       {"name": "<meeting-name>", "ws_addr": "ws://127.0.0.1:<port>",
        "control_url": "<optional http url of the agent-meeting control>",
        "instance": "<uuid, unique per `mycodex <name>` launch>"}
  3. Registers this session into agent-meeting: `meeting online <name> --cwd
     <cwd> [--instance <instance>] [--host <control_url>]` (worker role;
     --host omitted → LAN autodiscover; --instance ties every hook firing of
     THIS launch together so the daemon never refuses them, see below).
  4. Atomically writes the mapping file
       ~/.agent-meeting/codex/sessions/<name>.json
         = {name, session_id, ws_addr, cwd, source, ts}
     for the bridge daemon to read (it resumes `session_id` on `ws_addr`).
  5. Emits additionalContext so the session knows it is bridged.

Robustness: NEVER fails the codex session. Every error path still exits 0 (a
failed hook would show as exit 1 in codex); status is reported via
additionalContext / stderr instead. A codex lifecycle event fires exactly one
matching matcher (verified on 0.140.0: session start emits a single sessionStart
hook run, source=startup), so there is no concurrent-matcher race — `meeting
online` is idempotent and the mapping is written atomically regardless.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
AM_HOME = HOME / ".agent-meeting"
CODEX_DIR = AM_HOME / "codex"
RUNTIME_JSON = CODEX_DIR / "runtime.json"
SESSIONS_DIR = CODEX_DIR / "sessions"
MEETING_BIN = AM_HOME / "bin" / "meeting"


def emit(ctx: str) -> None:
    """Print SessionStart additionalContext and exit 0."""
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": ctx,
        }
    }))
    sys.exit(0)


def read_stdin_payload() -> dict:
    if sys.stdin.isatty():
        return {}
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except (ValueError, json.JSONDecodeError, OSError):
        return {}


def atomic_write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # unique temp name per process to avoid cross-matcher temp collisions
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)  # atomic on same filesystem (Windows + POSIX)


def main() -> None:
    payload = read_stdin_payload()
    session_id = payload.get("session_id") or ""
    cwd = payload.get("cwd") or os.getcwd()
    source = payload.get("source") or payload.get("hook_event_name") or "startup"

    if not RUNTIME_JSON.is_file():
        emit("[meeting] runtime.json 未找到，本会话未桥接（启动壳未写入运行时配置）。")

    try:
        rt = json.loads(RUNTIME_JSON.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        emit(f"[meeting] runtime.json 解析失败，本会话未桥接：{e}")

    name = (rt.get("name") or "").strip()
    ws_addr = (rt.get("ws_addr") or "").strip()
    control_url = (rt.get("control_url") or "").strip()
    if not name:
        emit("[meeting] runtime.json 缺 name，本会话未桥接。")

    # 1) register into agent-meeting. `meeting online` is idempotent (the control
    #    upserts by (project, name)), so a repeat call is harmless. A codex
    #    lifecycle event fires exactly ONE matching matcher — verified: every
    #    app-server session start emits a single hook/started/hook/completed with
    #    source=startup, never all four — so there is no concurrent-matcher race
    #    to guard against, and no claim/lock is needed. This hook fires multiple
    #    times across a single `mycodex <name>` launch though (warm-up=startup,
    #    the real foreground session=resume, plus every /clear or /compact), each
    #    a fresh process.
    # --instance ties every one of THOSE re-registrations, and codex-bridge.py's
    # (the same launch's long-running bridge daemon), to codex-meeting.py's
    # Launcher.setup() uuid via the shared runtime.json — the daemon always lets
    # a same-instance re-register through regardless of heartbeat, so this
    # never returns rc=1 "already registered" for anything belonging to THIS
    # launch. A DIFFERENT live process (a genuinely separate `mycodex <name>`
    # launch, e.g. from another machine) registering the same name has a
    # different instance and, if its heartbeat is fresh, gets refused (rc=3)
    # instead of silently displacing this one — see the rc==3 branch below.
    instance = (rt.get("instance") or "").strip() or None
    cmd = [sys.executable, str(MEETING_BIN), "online", name, "--cwd", cwd]
    if instance:
        cmd += ["--instance", instance]
    if control_url:
        cmd += ["--host", control_url]
    reg_status = "?"
    name_taken = False
    kw = {"creationflags": 0x08000000} if sys.platform.startswith("win") else {}  # CREATE_NO_WINDOW
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, **kw)
        if r.returncode == 0:
            reg_status = "online"
        elif r.returncode == 3:
            name_taken = True
            reg_status = f"refused: {(r.stderr or r.stdout).strip()[:200]}"
        else:
            # some other registration failure — non-fatal, keep session alive
            reg_status = f"meeting online rc={r.returncode}: {(r.stderr or r.stdout).strip()[:160]}"
    except (subprocess.TimeoutExpired, OSError) as e:
        reg_status = f"meeting online 调用失败：{e}"

    # 2) drop the mapping file for the bridge daemon (always, idempotent)
    mapping = {
        "name": name,
        "session_id": session_id,
        "ws_addr": ws_addr,
        "cwd": cwd,
        "source": source,
        "ts": int(time.time()),
    }
    try:
        atomic_write_json(SESSIONS_DIR / f"{name}.json", mapping)
        map_status = "ok"
    except OSError as e:
        map_status = f"落映射失败：{e}"

    if name_taken:
        # Unmistakable refusal message -- do NOT reuse the "已注册为...桥接就绪"
        # success wording here (TDP: don't silently fail, don't dress it up as ok).
        emit(
            f"[meeting] 未注册为 {name}：名字已被另一台机器/进程上一个仍存活的会话占用"
            f"（{reg_status}）。本会话未桥接，收不到 live-wake 消息。"
            f"跑 `meeting list` 看谁占着，`meeting stop {name}` 停掉它后重开，"
            "或用别的名字重新启动 mycodex。"
        )
    else:
        emit(
            f"[meeting] 已注册为 {name}（{reg_status}），"
            f"session_id={session_id or '?'}，映射={map_status}，桥接就绪。"
        )


if __name__ == "__main__":
    main()
