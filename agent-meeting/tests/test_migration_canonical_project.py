#!/usr/bin/env python3
"""
Test suite for migrations/0.8.55-canonical-project-identity.py.

Self-contained (stdlib only, no daemon). Builds a temp SQLite DB with the
same composite-key schema embedded in test_identity_regression.py, seeds
rows that exercise every fold/collision case, invokes the migration module
directly, and asserts the resulting DB state.

Usage:
    python3 agent-meeting/tests/test_migration_canonical_project.py
"""

import importlib.util
import os
import sqlite3
import sys
import tempfile

_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sessions (
  project       TEXT NOT NULL,
  name          TEXT NOT NULL,
  cwd           TEXT,
  host          TEXT,
  os            TEXT,
  registered_at TEXT,
  last_seen     REAL,
  role          TEXT NOT NULL DEFAULT 'worker',
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
"""

_MIGRATION_PATH = os.path.join(
    os.path.dirname(__file__), "..", "migrations", "0.8.55-canonical-project-identity.py"
)
_spec = importlib.util.spec_from_file_location("migration_0_8_55", _MIGRATION_PATH)
migration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migration)


# ---------- harness ----------

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


def new_db() -> str:
    tmpdir = tempfile.mkdtemp(prefix="am-migration-test-")
    db_path = os.path.join(tmpdir, "rooms.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return db_path


def dump_state(db_path: str) -> tuple:
    conn = sqlite3.connect(db_path)
    tables = ("sessions", "messages", "read_cursors", "groups", "group_members")
    dump = tuple(
        (t, tuple(sorted(conn.execute(f"SELECT * FROM {t}").fetchall())))
        for t in tables
    )
    conn.close()
    return dump


def seed_all_cases(db_path: str):
    """Seed rows for every case exercised by the test suite."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")

    # 1. messages: mapped values (WoodorAudit->wda-v3) + unmapped (SomeOtherProj)
    conn.execute(
        "INSERT INTO messages (sender_project, sender, recipient_project, recipient, kind, body, created_at)"
        " VALUES ('WoodorAudit','alice','wda-v3','bob','msg','hi',1)"
    )
    conn.execute(
        "INSERT INTO messages (sender_project, sender, recipient_project, recipient, kind, body, created_at)"
        " VALUES ('SomeOtherProj','carol','SomeOtherProj','dave','msg','untouched',2)"
    )

    # 2. sessions collision: (WoodorAudit, X) vs (wda-v3, X), different last_seen
    conn.execute(
        "INSERT INTO sessions (project, name, cwd, host, last_seen) VALUES "
        "('WoodorAudit','X','/old/dir','host-a', 100)"
    )
    conn.execute(
        "INSERT INTO sessions (project, name, cwd, host, last_seen) VALUES "
        "('wda-v3','X','/new/dir','host-b', 999)"
    )

    # 3. read_cursors collision: (WoodorAudit, m) cursor=10 vs (wda-v3, m) cursor=5
    conn.execute(
        "INSERT INTO read_cursors (project, member_name, cursor, updated_at) VALUES "
        "('WoodorAudit','m', 10, 500)"
    )
    conn.execute(
        "INSERT INTO read_cursors (project, member_name, cursor, updated_at) VALUES "
        "('wda-v3','m', 5, 100)"
    )

    # 4. group_members dedupe: group under wda-v3, member present under both old+canonical
    conn.execute(
        "INSERT INTO groups (project, name, created_at) VALUES ('wda-v3','team', 1)"
    )
    conn.execute(
        "INSERT INTO group_members (group_project, group_name, member_project, member_name, added_at)"
        " VALUES ('wda-v3','team','WoodorAudit','memberX', 1)"
    )
    conn.execute(
        "INSERT INTO group_members (group_project, group_name, member_project, member_name, added_at)"
        " VALUES ('wda-v3','team','wda-v3','memberX', 2)"
    )
    # a non-colliding member under the old project, to verify plain fold too
    conn.execute(
        "INSERT INTO group_members (group_project, group_name, member_project, member_name, added_at)"
        " VALUES ('wda-v3','team','WoodorAudit','memberY', 3)"
    )

    conn.commit()
    conn.close()


MAPPING = {"WoodorAudit": "wda-v3"}


# ---------- tests ----------

def test_messages_fold_and_unmapped():
    print("\n[1] messages folded for mapped values; unmapped left alone")
    db = new_db()
    seed_all_cases(db)
    migration.apply_or_dry_run(db, MAPPING, dry_run=False)

    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT sender_project, recipient_project FROM messages WHERE sender='alice'"
    ).fetchall()
    check("mapped message folded to wda-v3/wda-v3", rows == [("wda-v3", "wda-v3")], str(rows))

    untouched = conn.execute(
        "SELECT sender_project, recipient_project FROM messages WHERE sender='carol'"
    ).fetchall()
    check("unmapped message untouched", untouched == [("SomeOtherProj", "SomeOtherProj")], str(untouched))
    conn.close()


def test_sessions_collision():
    print("\n[2] sessions collision keeps greater last_seen")
    db = new_db()
    seed_all_cases(db)
    migration.apply_or_dry_run(db, MAPPING, dry_run=False)

    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT project, name, cwd, last_seen FROM sessions WHERE name='X'").fetchall()
    check("exactly one (wda-v3, X) session row", len(rows) == 1, str(rows))
    if rows:
        check("survivor is the greater last_seen row", rows[0] == ("wda-v3", "X", "/new/dir", 999), str(rows))
    conn.close()


def test_read_cursors_collision():
    print("\n[3] read_cursors collision merges by MAX(cursor)")
    db = new_db()
    seed_all_cases(db)
    migration.apply_or_dry_run(db, MAPPING, dry_run=False)

    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT project, member_name, cursor FROM read_cursors WHERE member_name='m'").fetchall()
    check("exactly one (wda-v3, m) cursor row", len(rows) == 1, str(rows))
    if rows:
        check("cursor merged to MAX(10,5)=10", rows[0] == ("wda-v3", "m", 10), str(rows))
    conn.close()


def test_group_members_dedupe():
    print("\n[4] group_members folds member_project with dedupe")
    db = new_db()
    seed_all_cases(db)
    migration.apply_or_dry_run(db, MAPPING, dry_run=False)

    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT member_project, member_name FROM group_members WHERE group_project='wda-v3' AND group_name='team'"
        " ORDER BY member_name"
    ).fetchall()
    check(
        "one row per PK: memberX deduped, memberY folded",
        rows == [("wda-v3", "memberX"), ("wda-v3", "memberY")],
        str(rows),
    )
    conn.close()


def test_idempotency():
    print("\n[5] running apply twice yields identical DB state")
    db = new_db()
    seed_all_cases(db)
    migration.apply_or_dry_run(db, MAPPING, dry_run=False)
    state1 = dump_state(db)
    migration.apply_or_dry_run(db, MAPPING, dry_run=False)
    state2 = dump_state(db)
    check("DB state unchanged on second apply", state1 == state2, "state differs across re-run")


def test_unmapped_value_untouched():
    print("\n[6] unmapped project value untouched across all tables")
    db = new_db()
    seed_all_cases(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO sessions (project, name, cwd, host, last_seen) VALUES ('SomeOtherProj','Y','/d','h', 1)"
    )
    conn.execute(
        "INSERT INTO read_cursors (project, member_name, cursor, updated_at) VALUES ('SomeOtherProj','Y', 1, 1)"
    )
    conn.commit()
    conn.close()

    migration.apply_or_dry_run(db, MAPPING, dry_run=False)

    conn = sqlite3.connect(db)
    s = conn.execute("SELECT project FROM sessions WHERE name='Y'").fetchall()
    c = conn.execute("SELECT project FROM read_cursors WHERE member_name='Y'").fetchall()
    check("unmapped sessions row untouched", s == [("SomeOtherProj",)], str(s))
    check("unmapped read_cursors row untouched", c == [("SomeOtherProj",)], str(c))
    conn.close()


def test_dry_run_no_mutation():
    print("\n[7] --dry-run does not mutate the DB")
    db = new_db()
    seed_all_cases(db)
    before = dump_state(db)
    migration.apply_or_dry_run(db, MAPPING, dry_run=True)
    after = dump_state(db)
    check("DB state identical after dry-run", before == after, "dry-run mutated the DB")


def test_groups_collision_skipped_with_warning():
    print("\n[8] groups PK collision is skipped, not auto-merged")
    db = new_db()
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO groups (project, name, created_at) VALUES ('WoodorAudit','dup-team', 1)")
    conn.execute("INSERT INTO groups (project, name, created_at) VALUES ('wda-v3','dup-team', 1)")
    conn.commit()
    conn.close()

    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    summary, warnings = migration.run_fold(conn, MAPPING)
    conn.execute("COMMIT")
    rows = conn.execute("SELECT project, name FROM groups ORDER BY project").fetchall()
    conn.close()

    check("both groups rows still present (no auto-merge)",
          rows == [("WoodorAudit", "dup-team"), ("wda-v3", "dup-team")], str(rows))
    check("a warning was recorded for the collision", len(warnings) == 1, str(warnings))


def test_group_fold_no_collision_with_members():
    """Regression: a non-colliding group WITH group_members children must fold
    without tripping the FK (group_project, group_name) -> groups(project, name)
    under PRAGMA foreign_keys=ON. The parent groups row and its child
    group_members rows are reprojected in separate statements within
    fold_groups(); with immediate FK enforcement the parent UPDATE is
    rejected as orphaning its children until the child UPDATE also runs."""
    print("\n[9] non-collision group WITH members folds without FK violation")
    db = new_db()
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("INSERT INTO groups (project, name, created_at) VALUES ('OldProj','team', 1)")
    conn.execute(
        "INSERT INTO group_members (group_project, group_name, member_project, member_name, added_at)"
        " VALUES ('OldProj','team','OldProj','alice', 1)"
    )
    conn.execute(
        "INSERT INTO group_members (group_project, group_name, member_project, member_name, added_at)"
        " VALUES ('OldProj','team','OtherProj','bob', 2)"
    )
    conn.commit()
    conn.close()

    raised = None
    try:
        migration.apply_or_dry_run(db, {"OldProj": "Canon"}, dry_run=False)
    except Exception as e:
        raised = e
    check("apply does not raise (no FK constraint failure)", raised is None, repr(raised))

    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = ON;")
    groups_rows = conn.execute("SELECT project, name FROM groups WHERE name='team'").fetchall()
    check("groups row reprojected to Canon", groups_rows == [("Canon", "team")], str(groups_rows))

    member_rows = conn.execute(
        "SELECT group_project, member_project, member_name FROM group_members"
        " WHERE group_name='team' ORDER BY member_name"
    ).fetchall()
    # alice's member_project was also 'OldProj', so the same mapping folds it
    # to 'Canon' too (independent of the group_project fold); bob's
    # member_project ('OtherProj') is unmapped and stays untouched.
    check(
        "group_members.group_project reprojected to Canon for both members",
        member_rows == [("Canon", "Canon", "alice"), ("Canon", "OtherProj", "bob")],
        str(member_rows),
    )

    fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    check("no orphaned FKs (foreign_key_check empty)", fk_violations == [], str(fk_violations))
    conn.close()


# ---------- main ----------

def main():
    test_messages_fold_and_unmapped()
    test_sessions_collision()
    test_read_cursors_collision()
    test_group_members_dedupe()
    test_idempotency()
    test_unmapped_value_untouched()
    test_dry_run_no_mutation()
    test_groups_collision_skipped_with_warning()
    test_group_fold_no_collision_with_members()

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
