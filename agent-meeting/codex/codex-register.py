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
     of time by the launcher shell (module 4). Shape:
       {"name": "<meeting-name>", "ws_addr": "ws://127.0.0.1:<port>",
        "control_url": "<optional http url of the agent-meeting control>"}
  3. Registers this session into agent-meeting: `meeting online <name> --cwd
     <cwd> [--host <control_url>]` (worker role; --host omitted → LAN autodiscover).
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
    #    to guard against, and no claim/lock is needed.
    cmd = [sys.executable, str(MEETING_BIN), "online", name, "--cwd", cwd]
    if control_url:
        cmd += ["--host", control_url]
    reg_status = "?"
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            reg_status = "online"
        else:
            # e.g. name already registered — non-fatal, keep session alive
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

    emit(
        f"[meeting] 已注册为 {name}（{reg_status}），"
        f"session_id={session_id or '?'}，映射={map_status}，桥接就绪。"
    )


if __name__ == "__main__":
    main()
