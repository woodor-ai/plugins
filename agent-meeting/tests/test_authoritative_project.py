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

Phase 2 CLI-addressing regression (docs/contracts/phase2-single-key-targets.md,
targets #3/#5/#6/#7 -- daemon-HTTP-level addressing for targets #1/#4 lives in
test_identity_regression.py):
  T3  - `meeting offline <name>` against a mismatched project must fail loudly
        (non-zero exit, session row untouched) instead of returning success
        with zero rows deleted; --proj/--global retarget the right project.
  T5  - `meeting send` to a peer with zero live sessions and zero message
        history anywhere must refuse to guess self's own project (no message
        inserted) instead of silently filing it under a project the peer may
        never register under.
  T6  - group management subcommands (create/add/remove/rename/delete/members)
        accept name@project like charter/list --member already did, so a group
        can be managed without cd-ing back to its original directory.
  T7  - two projects' same-named monitors get distinct pidfiles, and
        `meeting stop <name>` accepts --proj/--global to target the right one.

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


def run_meeting(meeting_home: str, cwd: str, args: list) -> subprocess.CompletedProcess:
    """Invoke the real source-tree `meeting` CLI with an arbitrary subcommand."""
    env = os.environ.copy()
    env["MEETING_HOME"] = meeting_home
    cmd = [sys.executable, MEETING_PATH] + args
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=15)


def _message_count(meeting_home: str) -> int:
    db_path = os.path.join(meeting_home, "db", "rooms.db")
    conn = sqlite3.connect(db_path, timeout=5)
    n = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    conn.close()
    return n


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


# ---------- T3: offline must not report success on a project mismatch ----------

def test_offline_project_scoped(meeting_home: str):
    """Phase 2 target #3: `meeting offline <name>` against a mismatched
    project must fail loudly (non-zero exit, session row untouched) instead of
    returning success with zero rows deleted; --proj retargets correctly."""
    print("\n[T3] offline 项目不匹配必须响亮失败，且有 --proj 逃生舱")

    home_cwd = tempfile.mkdtemp(prefix="am-authproj-offline-home-")
    mismatched_cwd = tempfile.mkdtemp(prefix="am-authproj-offline-mismatch-")
    try:
        r = run_meeting(meeting_home, home_cwd,
                        ["online", "offsess", "--cwd", home_cwd, "--proj", "offProjA",
                         "--host", HOST_URL])
        check("T3 setup: online registers offsess@offProjA", r.returncode == 0,
              f"stdout={r.stdout!r} stderr={r.stderr!r}")

        # cwd here derives some OTHER (mismatched) project -- bare `offline
        # offsess` must NOT silently report success.
        r = run_meeting(meeting_home, mismatched_cwd, ["offline", "offsess", "--host", HOST_URL])
        check("T3: offline from a mismatched project exits non-zero",
              r.returncode != 0, f"returncode={r.returncode} stdout={r.stdout!r} stderr={r.stderr!r}")
        row = _sessions_row(meeting_home, "offProjA", "offsess")
        check("T3: session row still present after mismatched offline (not silently deleted)",
              row is not None, str(row))

        # The --proj escape hatch must actually take it offline.
        r = run_meeting(meeting_home, mismatched_cwd,
                        ["offline", "offsess", "--proj", "offProjA", "--host", HOST_URL])
        check("T3: offline --proj offProjA exits 0", r.returncode == 0,
              f"stdout={r.stdout!r} stderr={r.stderr!r}")
        row2 = _sessions_row(meeting_home, "offProjA", "offsess")
        check("T3: session row gone after correctly-scoped offline", row2 is None, str(row2))
    finally:
        shutil.rmtree(home_cwd, ignore_errors=True)
        shutil.rmtree(mismatched_cwd, ignore_errors=True)


# ---------- T5: send to a never-seen peer must not guess self_project ----------

def test_resolve_peer_no_guessing(meeting_home: str):
    """Phase 2 target #5: `meeting send` to a peer with zero live sessions and
    zero message history anywhere must refuse to guess self's own project --
    fail loudly with an explicit-qualifier hint, insert no message."""
    print("\n[T5] send 给全新裸名不得默认猜 self_project")

    cwd = tempfile.mkdtemp(prefix="am-authproj-resolvepeer-")
    try:
        before = _message_count(meeting_home)
        r = run_meeting(meeting_home, cwd,
                        ["send", "selfNevr", "peerNevr", "hello", "--host", HOST_URL])
        check("T5: send to a never-seen peer exits non-zero", r.returncode != 0,
              f"returncode={r.returncode} stdout={r.stdout!r} stderr={r.stderr!r}")
        check("T5: error names the peer and points at an explicit qualifier",
              "peerNevr" in r.stderr and "@" in r.stderr, f"stderr={r.stderr!r}")
        after = _message_count(meeting_home)
        check("T5: no message was inserted", after == before, f"before={before} after={after}")

        # The explicit @project qualifier must still work.
        r2 = run_meeting(meeting_home, cwd,
                         ["send", "selfNevr", "peerNevr@someProj", "hello", "--host", HOST_URL])
        check("T5: send with an explicit @project qualifier succeeds", r2.returncode == 0,
              f"stdout={r2.stdout!r} stderr={r2.stderr!r}")
    finally:
        shutil.rmtree(cwd, ignore_errors=True)


# ---------- T6: group management subcommands need a project escape hatch ----------

def test_group_management_escape_hatch(meeting_home: str):
    """Phase 2 target #6: group management subcommands (create/add/remove/
    rename/delete/members) must accept name@project like charter/list
    --member already do in the same file, instead of being pinned to cwd
    derivation with no way to target a group from a different directory."""
    print("\n[T6] group 管理类子命令支持 name@project 逃生舱")

    home_cwd = tempfile.mkdtemp(prefix="am-authproj-group-home-")
    other_cwd = tempfile.mkdtemp(prefix="am-authproj-group-other-")
    try:
        for m in ("gm1", "gm2", "gm3"):
            r = run_meeting(meeting_home, home_cwd,
                            ["online", m, "--cwd", home_cwd, "--proj", "gProjA",
                             "--host", HOST_URL])
            check(f"T6 setup: {m}@gProjA registers", r.returncode == 0,
                  f"stdout={r.stdout!r} stderr={r.stderr!r}")

        # create: group_name@project from a cwd that derives a DIFFERENT project.
        r = run_meeting(meeting_home, other_cwd,
                        ["group", "--host", HOST_URL, "create", "gteam@gProjA",
                         "--members", "gm1,gm2"])
        check("T6 create: group_name@project creates under the right project",
              r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}")

        r = run_meeting(meeting_home, other_cwd,
                        ["group", "--host", HOST_URL, "members", "gteam@gProjA"])
        check("T6 members: lists gm1@gProjA via @project from a mismatched cwd",
              "gm1@gProjA" in r.stdout, f"stdout={r.stdout!r} stderr={r.stderr!r}")

        # add: add a third member from the same mismatched cwd via @project.
        r = run_meeting(meeting_home, other_cwd,
                        ["group", "--host", HOST_URL, "add", "gteam@gProjA", "gm3@gProjA"])
        check("T6 add: succeeds cross-cwd via @project", r.returncode == 0,
              f"stdout={r.stdout!r} stderr={r.stderr!r}")
        r = run_meeting(meeting_home, other_cwd,
                        ["group", "--host", HOST_URL, "members", "gteam@gProjA"])
        check("T6 add: gm3@gProjA now a member", "gm3@gProjA" in r.stdout, f"stdout={r.stdout!r}")

        # remove: remove gm3 the same way.
        r = run_meeting(meeting_home, other_cwd,
                        ["group", "--host", HOST_URL, "remove", "gteam@gProjA", "gm3@gProjA"])
        check("T6 remove: succeeds cross-cwd via @project", r.returncode == 0,
              f"stdout={r.stdout!r} stderr={r.stderr!r}")
        r = run_meeting(meeting_home, other_cwd,
                        ["group", "--host", HOST_URL, "members", "gteam@gProjA"])
        check("T6 remove: gm3@gProjA no longer a member",
              "gm3@gProjA" not in r.stdout, f"stdout={r.stdout!r}")

        # rename: old_name@project from a mismatched cwd.
        r = run_meeting(meeting_home, other_cwd,
                        ["group", "--host", HOST_URL, "rename", "gteam@gProjA", "gteam2"])
        check("T6 rename: succeeds cross-cwd via @project", r.returncode == 0,
              f"stdout={r.stdout!r} stderr={r.stderr!r}")
        r = run_meeting(meeting_home, other_cwd,
                        ["group", "--host", HOST_URL, "members", "gteam2@gProjA"])
        check("T6 rename: new name resolvable under the same project",
              "gm1@gProjA" in r.stdout, f"stdout={r.stdout!r}")

        # delete: group_name@project from a mismatched cwd.
        r = run_meeting(meeting_home, other_cwd,
                        ["group", "--host", HOST_URL, "delete", "gteam2@gProjA"])
        check("T6 delete: succeeds cross-cwd via @project", r.returncode == 0,
              f"stdout={r.stdout!r} stderr={r.stderr!r}")

        # Discovered alongside target #6 (same acceptance criterion): deleting
        # an already-gone / never-existed group must not report success.
        r = run_meeting(meeting_home, other_cwd,
                        ["group", "--host", HOST_URL, "delete", "gteam2@gProjA"])
        check("T6 delete: re-deleting an already-purged group is not a fake success",
              r.returncode != 0, f"returncode={r.returncode} stdout={r.stdout!r} stderr={r.stderr!r}")
    finally:
        shutil.rmtree(home_cwd, ignore_errors=True)
        shutil.rmtree(other_cwd, ignore_errors=True)


# ---------- T7: stop pidfile must be scoped by (project, name) ----------

def test_stop_pidfile_scoped_by_project(meeting_home: str):
    """Phase 2 target #7: two projects' same-named monitors must not share a
    pidfile, and `meeting stop <name>` must accept --proj to target the right
    one instead of only ever being able to hit whichever process wrote the
    shared pidfile last."""
    print("\n[T7] stop pidfile 按 project 隔离，且有 --proj 逃生舱")

    install_local_meeting_cli(meeting_home)
    cwd_a = tempfile.mkdtemp(prefix="am-authproj-stop-a-")
    cwd_b = tempfile.mkdtemp(prefix="am-authproj-stop-b-")
    proc_a = proc_b = None
    try:
        env = os.environ.copy()
        env["MEETING_HOME"] = meeting_home
        proc_a = subprocess.Popen(
            [sys.executable, MONITOR_PATH, "dupmon", "--proj", "stopProjA", "--host", HOST_URL],
            cwd=cwd_a, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        proc_b = subprocess.Popen(
            [sys.executable, MONITOR_PATH, "dupmon", "--proj", "stopProjB", "--host", HOST_URL],
            cwd=cwd_b, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

        pid_a_path = os.path.join(meeting_home, "run",
                                   f"{meeting_common.pidfile_stem('dupmon', 'stopProjA')}.pid")
        pid_b_path = os.path.join(meeting_home, "run",
                                   f"{meeting_common.pidfile_stem('dupmon', 'stopProjB')}.pid")

        deadline = time.time() + 15
        while time.time() < deadline and not (os.path.exists(pid_a_path) and os.path.exists(pid_b_path)):
            time.sleep(0.2)

        check("T7: monitor A pidfile exists", os.path.exists(pid_a_path), pid_a_path)
        check("T7: monitor B pidfile exists", os.path.exists(pid_b_path), pid_b_path)
        check("T7: A and B pidfiles are distinct paths", pid_a_path != pid_b_path,
              f"a={pid_a_path} b={pid_b_path}")

        # `meeting stop dupmon --proj stopProjA` must stop ONLY monitor A.
        r = run_meeting(meeting_home, cwd_a, ["stop", "dupmon", "--proj", "stopProjA"])
        check("T7: stop --proj stopProjA exits 0", r.returncode == 0,
              f"stdout={r.stdout!r} stderr={r.stderr!r}")

        a_deadline = time.time() + 5
        while time.time() < a_deadline and proc_a.poll() is None:
            time.sleep(0.1)
        check("T7: monitor A process exited", proc_a.poll() is not None, "still running")
        check("T7: monitor B process still running (untouched)", proc_b.poll() is None,
              f"B exited early with code={proc_b.poll()}")

        r2 = run_meeting(meeting_home, cwd_b, ["stop", "dupmon", "--proj", "stopProjB"])
        check("T7: stop --proj stopProjB exits 0", r2.returncode == 0,
              f"stdout={r2.stdout!r} stderr={r2.stderr!r}")
        b_deadline = time.time() + 5
        while time.time() < b_deadline and proc_b.poll() is None:
            time.sleep(0.1)
        check("T7: monitor B process exited", proc_b.poll() is not None, "still running")
    finally:
        for p in (proc_a, proc_b):
            if p is not None and p.poll() is None:
                try:
                    p.terminate()
                    p.wait(timeout=5)
                except Exception:
                    try:
                        p.kill()
                    except Exception:
                        pass
        shutil.rmtree(cwd_a, ignore_errors=True)
        shutil.rmtree(cwd_b, ignore_errors=True)


# ---------- main ----------

def main():
    meeting_home = tempfile.mkdtemp(prefix="am-authproj-home-")
    daemon_proc = None
    try:
        daemon_proc = start_daemon(meeting_home)
        test_authoritative_resolution(meeting_home)
        test_monitor_noretry(meeting_home)
        test_offline_project_scoped(meeting_home)
        test_resolve_peer_no_guessing(meeting_home)
        test_group_management_escape_hatch(meeting_home)
        test_stop_pidfile_scoped_by_project(meeting_home)
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
