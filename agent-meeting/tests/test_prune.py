#!/usr/bin/env python3
"""
Prune regression suite for meeting-daemon's /prune endpoint (phase 4 cleanup).

Covers:
  TC1 - dry run does not delete
  TC2 - apply actually deletes
  TC3 - referenced identity skipped by default (messages untouched)
  TC4 - --include-referenced deletes it (messages still untouched)
  TC5 - a live (heartbeating) session is never a candidate
  TC6 - older_than_days gating

Usage:
    MEETING_HOME=$(mktemp -d) python3 agent-meeting/tests/test_prune.py
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

TEST_PORT = 8801  # distinct port, not used by any other test file
HOST = "127.0.0.1"

# ---------- DB bootstrap (must match bin/meeting-daemon's _SCHEMA) ----------

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sessions (
  project       TEXT NOT NULL,
  name          TEXT NOT NULL,
  cwd           TEXT,
  host          TEXT,
  os            TEXT,
  instance      TEXT,
  registered_at TEXT,
  last_seen     REAL,
  role          TEXT NOT NULL DEFAULT 'worker',
  client_version TEXT,
  PRIMARY KEY (project, name)
);

CREATE TABLE IF NOT EXISTS messages (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  sender_project   TEXT NOT NULL,
  sender           TEXT NOT NULL,
  recipient_project TEXT NOT NULL,
  recipient        TEXT NOT NULL,
  kind             TEXT NOT NULL,
  body             TEXT NOT NULL,
  ask              TEXT,
  created_at       INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_recipient ON messages(recipient_project, recipient, id);
CREATE INDEX IF NOT EXISTS idx_messages_sender_recipient ON messages(sender_project, sender, recipient_project, recipient, id);

CREATE TABLE IF NOT EXISTS read_cursors (
  project     TEXT NOT NULL,
  member_name TEXT NOT NULL,
  cursor      INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL,
  PRIMARY KEY (project, member_name)
);

CREATE TABLE IF NOT EXISTS groups (
  project    TEXT NOT NULL,
  name       TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  creator    TEXT,
  charter    TEXT,
  PRIMARY KEY (project, name)
);

CREATE TABLE IF NOT EXISTS group_members (
  group_project  TEXT NOT NULL,
  group_name     TEXT NOT NULL,
  member_project TEXT NOT NULL,
  member_name    TEXT NOT NULL,
  added_at       INTEGER NOT NULL,
  PRIMARY KEY (group_project, group_name, member_project, member_name),
  FOREIGN KEY (group_project, group_name) REFERENCES groups(project, name) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_group_members_member ON group_members(member_project, member_name);
"""


def init_db(home_dir: str):
    db_dir = os.path.join(home_dir, "db")
    os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(os.path.join(db_dir, "rooms.db"))
    conn.executescript(_SCHEMA)
    conn.close()


# ---------- daemon lifecycle ----------

def start_daemon(home_dir: str) -> subprocess.Popen:
    daemon_path = os.path.join(os.path.dirname(__file__), "..", "bin", "meeting-daemon")
    env = os.environ.copy()
    # Both MUST be set: MEETING_HOME so the daemon owns our temp DB, and
    # MEETING_HOST so nothing in this process's own code path (none here,
    # but kept as a hard rule per the isolation incident) can fall back to
    # discovering/registering against a real production daemon.
    env["MEETING_HOME"] = home_dir
    env["MEETING_HOST"] = f"http://{HOST}:{TEST_PORT}"
    proc = subprocess.Popen(
        [sys.executable, daemon_path, f"--port={TEST_PORT}", "--no-mdns"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    for _ in range(40):
        time.sleep(0.25)
        try:
            _http("/health")
            return proc
        except Exception:
            if proc.poll() is not None:
                _, err = proc.communicate()
                raise RuntimeError(f"Daemon exited early:\n{err.decode()}")
    raise RuntimeError("Daemon did not start within 10s")


# ---------- HTTP helpers ----------

def _http(path: str, method: str = "GET", body=None, params=None,
          allow_error: bool = False):
    url = f"http://{HOST}:{TEST_PORT}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if allow_error:
            return json.loads(e.read().decode("utf-8", errors="replace"))
        raise


def prune(older_than_days: float, include_referenced: bool = False, apply: bool = False):
    return _http("/prune", "POST", {
        "older_than_days": older_than_days,
        "include_referenced": include_referenced,
        "apply": apply,
    })


# ---------- direct DB helpers ----------

def _db(home_dir: str):
    db_path = os.path.join(home_dir, "db", "rooms.db")
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.executescript("PRAGMA journal_mode = WAL; PRAGMA foreign_keys = ON;")
    return conn


def _set_session(home_dir: str, project: str, name: str, last_seen: float):
    conn = _db(home_dir)
    conn.execute(
        "INSERT INTO sessions (project, name, last_seen, registered_at, role)"
        " VALUES (?, ?, ?, ?, 'worker')"
        " ON CONFLICT(project, name) DO UPDATE SET last_seen=excluded.last_seen",
        (project, name, last_seen, str(int(time.time()))),
    )
    conn.close()


def _session_exists(home_dir: str, project: str, name: str) -> bool:
    conn = _db(home_dir)
    row = conn.execute(
        "SELECT 1 FROM sessions WHERE project=? AND name=?", (project, name)
    ).fetchone()
    conn.close()
    return row is not None


def _insert_message(home_dir: str, sp: str, s: str, rp: str, r: str):
    conn = _db(home_dir)
    conn.execute(
        "INSERT INTO messages (sender_project, sender, recipient_project, recipient, kind, body, ask, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, NULL, ?)",
        (sp, s, rp, r, "消息", "prune test msg", int(time.time())),
    )
    conn.close()


def _message_count(home_dir: str) -> int:
    conn = _db(home_dir)
    n = conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]
    conn.close()
    return n


# ---------- test harness ----------

PASS_COUNT = 0
FAIL_COUNT = 0
FAILURES: list = []


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


# ---------- TC1: dry run does not delete ----------

def test_tc1_dry_run_no_delete(home_dir: str):
    print("\n[TC1] dry run 不删")

    now = time.time()
    _set_session(home_dir, "pjA", "stale1", now - 30 * 86400)

    r = prune(older_than_days=7, apply=False)
    names = [(it["project"], it["name"]) for it in r["pruned"]]

    check("TC1: applied=false", r["applied"] is False, str(r))
    check("TC1: stale1 appears in pruned list", ("pjA", "stale1") in names, str(names))
    check("TC1: DB row still exists after dry run", _session_exists(home_dir, "pjA", "stale1"))


# ---------- TC2: apply actually deletes ----------

def test_tc2_apply_deletes(home_dir: str):
    print("\n[TC2] apply 真删")

    now = time.time()
    _set_session(home_dir, "pjA", "stale2", now - 30 * 86400)

    r = prune(older_than_days=7, apply=True)
    names = [(it["project"], it["name"]) for it in r["pruned"]]

    check("TC2: applied=true", r["applied"] is True, str(r))
    check("TC2: stale2 appears in pruned list", ("pjA", "stale2") in names, str(names))
    check("TC2: DB row gone after apply", not _session_exists(home_dir, "pjA", "stale2"))


# ---------- TC3: referenced identity skipped by default ----------

def test_tc3_referenced_skipped_by_default(home_dir: str):
    print("\n[TC3] 被引用的默认跳过")

    now = time.time()
    _set_session(home_dir, "pjA", "referenced3", now - 30 * 86400)
    _insert_message(home_dir, "pjA", "referenced3", "pjA", "other")
    msg_count_before = _message_count(home_dir)

    r = prune(older_than_days=7, include_referenced=False, apply=True)
    skipped_names = [(it["project"], it["name"]) for it in r["skipped_referenced"]]

    check("TC3: referenced3 in skipped_referenced", ("pjA", "referenced3") in skipped_names,
          str(skipped_names))
    check("TC3: DB row still exists", _session_exists(home_dir, "pjA", "referenced3"))
    check("TC3: message count unchanged", _message_count(home_dir) == msg_count_before,
          f"before={msg_count_before} after={_message_count(home_dir)}")


# ---------- TC4: --include-referenced deletes it, messages untouched ----------

def test_tc4_include_referenced_deletes(home_dir: str):
    print("\n[TC4] --include-referenced 才删")

    now = time.time()
    _set_session(home_dir, "pjA", "referenced4", now - 30 * 86400)
    _insert_message(home_dir, "pjA", "referenced4", "pjA", "other")
    msg_count_before = _message_count(home_dir)

    r = prune(older_than_days=7, include_referenced=True, apply=True)
    pruned_names = [(it["project"], it["name"]) for it in r["pruned"]]

    check("TC4: referenced4 in pruned list", ("pjA", "referenced4") in pruned_names,
          str(pruned_names))
    check("TC4: DB row gone", not _session_exists(home_dir, "pjA", "referenced4"))
    check("TC4: message count unchanged", _message_count(home_dir) == msg_count_before,
          f"before={msg_count_before} after={_message_count(home_dir)}")


# ---------- TC5: a live session is never a candidate ----------

def test_tc5_live_session_never_pruned(home_dir: str):
    print("\n[TC5] 在线的绝不删")

    now = time.time()
    _set_session(home_dir, "pjA", "live5", now)

    r = prune(older_than_days=0, apply=True)
    names = [(it["project"], it["name"]) for it in r["pruned"]]

    check("TC5: live5 not in pruned list", ("pjA", "live5") not in names, str(names))
    check("TC5: DB row still exists", _session_exists(home_dir, "pjA", "live5"))


# ---------- TC6: older_than_days gating ----------

def test_tc6_older_than_days_gate(home_dir: str):
    print("\n[TC6] 天数守门")

    now = time.time()
    _set_session(home_dir, "pjA", "recent6", now - 3600)

    r7 = prune(older_than_days=7, apply=False)
    names7 = [(it["project"], it["name"]) for it in r7["pruned"]]
    check("TC6: not a candidate at older_than_days=7", ("pjA", "recent6") not in names7,
          str(names7))

    r0 = prune(older_than_days=0, apply=True)
    names0 = [(it["project"], it["name"]) for it in r0["pruned"]]
    check("TC6: candidate and deleted at older_than_days=0", ("pjA", "recent6") in names0,
          str(names0))
    check("TC6: DB row gone", not _session_exists(home_dir, "pjA", "recent6"))


# ---------- main ----------

def main():
    home_dir = tempfile.mkdtemp(prefix="am-prune-test-")

    try:
        init_db(home_dir)
        proc = start_daemon(home_dir)

        try:
            test_tc1_dry_run_no_delete(home_dir)
            test_tc2_apply_deletes(home_dir)
            test_tc3_referenced_skipped_by_default(home_dir)
            test_tc4_include_referenced_deletes(home_dir)
            test_tc5_live_session_never_pruned(home_dir)
            test_tc6_older_than_days_gate(home_dir)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    finally:
        shutil.rmtree(home_dir, ignore_errors=True)

    sep = "=" * 60
    print(f"\n{sep}")
    print(f"Results: {PASS_COUNT} passed, {FAIL_COUNT} failed (total {PASS_COUNT + FAIL_COUNT} checks)")
    if FAILURES:
        print("\nFailed checks:")
        for f in FAILURES:
            print(f)
    print(sep)

    if FAIL_COUNT == 0:
        print("ALL PRUNE TESTS PASSED")
        sys.exit(0)
    sys.exit(1)


if __name__ == "__main__":
    main()
