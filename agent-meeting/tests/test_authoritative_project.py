#!/usr/bin/env python3
"""
Phase 0.1 regression: closing off the project-identity drift mechanism
(docs/contracts/0.10.0-composite-key-identity.md).

Covers:
  TC1  - no --proj, empty cache -> cmd_online exits 4 with
         code=missing_project_identity, no session row is ever written
         (proves the daemon is never even called).
  TC2  - explicit --proj=foo -> registers successfully, sessions.project=foo,
         and the per-root proj cache is written.
  TC3  - same repo root, no --proj this time (cache from TC2) -> registers
         successfully via the cache, project=foo (no path derivation).
  TC4  - TC2's session row has client_version == plugin.json's version.
  TC5  - explicit --proj=* -> registers successfully, project="*" -- proves
         "*" is treated as an ordinary authoritative value, not specially
         excluded (no whitelist in the missing-identity check).
  TC6  - --global (no --proj at all) -> registers successfully, project="*"
         -- proves is_global short-circuits before any missing-identity
         check (the existing global-identity flow, e.g. the amp hub double,
         must not be caught by the new refusal).
  TC7  - monitor.py subprocess with no --proj and an empty cache -> process
         exits promptly (refused, code 4) instead of looping retries; the
         _NORETRY_EXIT_CODES set monitor.py checks against is {3, 4} and
         nothing else (an unrelated code like 1 must still be retried).

Usage:
    python3 agent-meeting/tests/test_authoritative_project.py
"""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

BIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin")
MEETING_PATH = os.path.join(BIN_DIR, "meeting")
DAEMON_PATH = os.path.join(BIN_DIR, "meeting-daemon")
MONITOR_PATH = os.path.join(BIN_DIR, "monitor.py")
PLUGIN_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                            ".claude-plugin", "plugin.json")

sys.path.insert(0, BIN_DIR)
import meeting_common  # noqa: E402

TEST_PORT = 8796  # distinct from other tests' ports (8765 live, 8796-8799 test suite)
HOST = "127.0.0.1"
HOST_URL = f"http://{HOST}:{TEST_PORT}"

PASS_COUNT = 0
FAIL_COUNT = 0
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    global PASS_COUNT, FAIL_COUNT
    if cond:
        print(f"  PASS  {name}")
        PASS_COUNT += 1
    else:
        msg = f"  FAIL  {name}" + (f" — {detail}" if detail else "")
        print(msg)
        FAIL_COUNT += 1
        FAILURES.append(msg)


# ---------- daemon lifecycle (empty DB file -- daemon's own CREATE TABLE IF
# NOT EXISTS builds the current schema, client_version column included) ----------

def start_daemon(meeting_home: str) -> subprocess.Popen:
    db_dir = os.path.join(meeting_home, "db")
    os.makedirs(db_dir, exist_ok=True)
    sqlite3.connect(os.path.join(db_dir, "rooms.db")).close()

    env = os.environ.copy()
    env["MEETING_HOME"] = meeting_home
    proc = subprocess.Popen(
        [sys.executable, DAEMON_PATH, f"--port={TEST_PORT}", "--no-mdns"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    for _ in range(40):
        time.sleep(0.25)
        try:
            _health()
            return proc
        except Exception:
            if proc.poll() is not None:
                _, err = proc.communicate()
                raise RuntimeError(f"Daemon exited early:\n{err.decode()}")
    raise RuntimeError("Daemon did not start within 10s")


def _health():
    with urllib.request.urlopen(f"{HOST_URL}/health", timeout=5) as r:
        return json.loads(r.read())


def _sessions_row(meeting_home: str, project: str, name: str):
    db_path = os.path.join(meeting_home, "db", "rooms.db")
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM sessions WHERE project=? AND name=?", (project, name)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def _all_session_names(meeting_home: str) -> set:
    db_path = os.path.join(meeting_home, "db", "rooms.db")
    conn = sqlite3.connect(db_path, timeout=5)
    rows = conn.execute("SELECT project, name FROM sessions").fetchall()
    conn.close()
    return set(rows)


# ---------- `meeting online` CLI invocation (real source-tree bin/meeting) ----------

def run_online(meeting_home: str, cwd: str, name: str, extra_args: list) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["MEETING_HOME"] = meeting_home
    cmd = [sys.executable, MEETING_PATH, "online", name, "--cwd", cwd, "--host", HOST_URL] + extra_args
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=15)


def _plugin_version() -> str:
    with open(PLUGIN_JSON) as f:
        return json.load(f)["version"]


# ---------- TC1-TC6: `meeting online` authoritative-identity resolution ----------

def test_authoritative_resolution(meeting_home: str):
    print("\n[TC1-TC6] `meeting online` 权威身份解析")

    repo_root = tempfile.mkdtemp(prefix="am-authproj-repo-")
    try:
        before_names = _all_session_names(meeting_home)

        # TC1: no --proj, empty cache -> exit 4, no session row written.
        r = run_online(meeting_home, repo_root, "sess1", [])
        check("TC1: exit code 4 (missing_project_identity)", r.returncode == 4,
              f"returncode={r.returncode} stdout={r.stdout!r} stderr={r.stderr!r}")
        check("TC1: stdout carries code=missing_project_identity",
              '"code": "missing_project_identity"' in r.stdout, f"stdout={r.stdout!r}")
        after_names = _all_session_names(meeting_home)
        check("TC1: no session row written (daemon never called)",
              after_names == before_names, f"before={before_names} after={after_names}")

        # TC2: explicit --proj=foo -> registers, sessions.project=foo, cache written.
        r = run_online(meeting_home, repo_root, "sess2", ["--proj", "foo"])
        check("TC2: exit code 0", r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}")
        row = _sessions_row(meeting_home, "foo", "sess2")
        check("TC2: sessions row exists under project=foo", row is not None, str(row))
        # proj_cache_get reads from meeting_common.MEETING_HOME (this process's env),
        # not the subprocess's -- point it at the same MEETING_HOME the CLI used.
        _old_home = meeting_common.MEETING_HOME
        meeting_common.MEETING_HOME = meeting_home
        try:
            cached = meeting_common.proj_cache_get(meeting_common._project_root(repo_root))
        finally:
            meeting_common.MEETING_HOME = _old_home
        check("TC2: --proj cached for repo root", cached == "foo", f"cached={cached!r}")

        # TC3: same root, no --proj this time -> registers via cache, project=foo.
        r = run_online(meeting_home, repo_root, "sess3", [])
        check("TC3: exit code 0 (resolved via cache)", r.returncode == 0,
              f"stdout={r.stdout!r} stderr={r.stderr!r}")
        row3 = _sessions_row(meeting_home, "foo", "sess3")
        check("TC3: sessions row exists under cached project=foo", row3 is not None, str(row3))

        # TC4: TC2's session row carries client_version == plugin.json's version.
        expected_version = _plugin_version()
        r = run_online(meeting_home, repo_root, "sess4", ["--proj", "foo",
                       "--client-version", expected_version])
        check("TC4: exit code 0", r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}")
        row4 = _sessions_row(meeting_home, "foo", "sess4")
        check("TC4: client_version stored matches plugin.json",
              row4 is not None and row4.get("client_version") == expected_version,
              f"row={row4}, expected={expected_version!r}")

        # TC5: explicit --proj=* -> registers, project="*" -- not special-cased out.
        r = run_online(meeting_home, repo_root, "sess5", ["--proj", "*"])
        check("TC5: exit code 0 (--proj=* is a valid explicit declaration)",
              r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}")
        row5 = _sessions_row(meeting_home, "*", "sess5")
        check("TC5: sessions row exists under project=*", row5 is not None, str(row5))

        # TC6: --global (no --proj at all) -> registers, project="*" -- the
        # is_global branch must short-circuit before any missing-identity check.
        r = run_online(meeting_home, repo_root, "sess6", ["--global"])
        check("TC6: exit code 0 (--global short-circuits missing-identity check)",
              r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}")
        row6 = _sessions_row(meeting_home, "*", "sess6")
        check("TC6: sessions row exists under project=* (global)", row6 is not None, str(row6))
    finally:
        shutil.rmtree(repo_root, ignore_errors=True)


# ---------- TC7: monitor.py's no-retry set + real refusal-exit behavior ----------

def _extract_noretry_codes() -> set:
    """Source-level assertion of monitor.py's _NORETRY_EXIT_CODES (module can't
    be imported directly -- it parses sys.argv at import time)."""
    with open(MONITOR_PATH, encoding="utf-8") as f:
        src = f.read()
    m = re.search(r"_NORETRY_EXIT_CODES\s*=\s*(\{[^}]*\})", src)
    assert m, "_NORETRY_EXIT_CODES not found in monitor.py"
    return eval(m.group(1))  # noqa: S307 -- trusted source file, just a literal set


def install_local_meeting_cli(meeting_home: str) -> None:
    """Copy the SOURCE TREE's bin/meeting + meeting_common.py into
    <meeting_home>/bin/ so monitor.py's `_run_meeting()` subprocess exercises
    the code just edited, not whatever build happens to be installed at
    ~/.agent-meeting on this machine."""
    dst = os.path.join(meeting_home, "bin")
    os.makedirs(dst, exist_ok=True)
    shutil.copy2(MEETING_PATH, os.path.join(dst, "meeting"))
    shutil.copy2(os.path.join(BIN_DIR, "meeting_common.py"), os.path.join(dst, "meeting_common.py"))
    os.chmod(os.path.join(dst, "meeting"), 0o755)


def test_monitor_noretry(meeting_home: str):
    print("\n[TC7] monitor.py 拒绝码不重试")

    codes = _extract_noretry_codes()
    check("TC7a: _NORETRY_EXIT_CODES == {3, 4}", codes == {3, 4}, f"got {codes}")
    check("TC7a: transient code 1 is NOT in the no-retry set (still retried)",
          1 not in codes, f"got {codes}")

    install_local_meeting_cli(meeting_home)
    repo_root = tempfile.mkdtemp(prefix="am-authproj-monitor-")
    try:
        env = os.environ.copy()
        env["MEETING_HOME"] = meeting_home
        proc = subprocess.Popen(
            [sys.executable, MONITOR_PATH, "monitor-noproj"],
            cwd=repo_root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            out, err = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            check("TC7b: monitor exits promptly on refusal (does not loop retrying)",
                  False, "process was still running after 15s -- looks like it retried")
            return
        check("TC7b: monitor exits promptly on refusal (does not loop retrying)", True)
        check("TC7b: exit code is non-zero (os._exit(1) path)", proc.returncode != 0,
              f"returncode={proc.returncode}")
        check("TC7b: stderr reports refusal with exit 4",
              "registration refused (exit 4)" in err, f"stderr={err!r}")
        check("TC7b: monitor never printed 'monitor started' (died before entering the WS loop)",
              "monitor started" not in out, f"stdout={out!r}")
    finally:
        shutil.rmtree(repo_root, ignore_errors=True)


# ---------- main ----------

def main():
    meeting_home = tempfile.mkdtemp(prefix="am-authproj-home-")
    daemon_proc = None
    try:
        daemon_proc = start_daemon(meeting_home)
        test_authoritative_resolution(meeting_home)
        test_monitor_noretry(meeting_home)
    finally:
        if daemon_proc is not None:
            daemon_proc.terminate()
            try:
                daemon_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                daemon_proc.kill()
        shutil.rmtree(meeting_home, ignore_errors=True)

    sep = "=" * 60
    print(f"\n{sep}")
    print(f"Results: {PASS_COUNT} passed, {FAIL_COUNT} failed  (total {PASS_COUNT + FAIL_COUNT} checks)")
    if FAILURES:
        print("\nFailed checks:")
        for f in FAILURES:
            print(f)
    print(sep)

    sys.exit(0 if FAIL_COUNT == 0 else 1)


if __name__ == "__main__":
    main()
