#!/usr/bin/env python3
"""
Test suite for migrations/0.10.0-apply-identity-remap.py.

Self-contained (stdlib only, no daemon). Builds a temp SQLite DB with the
composite-key schema (same shape as test_identity_regression.py /
test_migration_canonical_project.py plus a groups table with a 'charter'
column, matching the real rooms.db), seeds rows that exercise every
rewrite/collision/deletion case, invokes the migration module directly, and
asserts the resulting DB state. Never touches $MEETING_HOME/db/rooms.db --
every test builds its own tempdir DB and passes it via --db / the module's
apply_or_dry_run(db_path, ...) directly.

Usage:
    python3 agent-meeting/tests/test_apply_identity_remap.py
"""

import importlib.util
import json
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
"""

_MIGRATION_PATH = os.path.join(
    os.path.dirname(__file__), "..", "migrations", "0.10.0-apply-identity-remap.py"
)
_spec = importlib.util.spec_from_file_location("migration_0_10_0_apply", _MIGRATION_PATH)
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
    tmpdir = tempfile.mkdtemp(prefix="am-remap-apply-test-")
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


def log_rows(db_path: str) -> list:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, table_name, row_key, column_name, old_value, new_value, "
            "mapping_from, mapping_to, op, applied_at FROM identity_remap_log ORDER BY id"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return rows


def mk(name, project):
    return {"name": name, "project": project}


def make_remap(canonical, mappings, deletions=None, row_overrides=None):
    return {
        "schema": migration.SCHEMA_VERSION,
        "generated_at": "2026-07-22 07:46 PDT",
        "generator": "agent-meeting/migrations/0.10.0-identity-remap.py",
        "source_db": "test",
        "canonical": canonical,
        "mappings": mappings,
        "deletions": deletions or [],
        "row_overrides": row_overrides or [],
    }


def mapping_entry(from_name, from_project, to_name, to_project, basis="manual", rule=None, note="test"):
    return {
        "from": mk(from_name, from_project),
        "to": mk(to_name, to_project),
        "basis": basis,
        "rule": rule,
        "note": note if basis == "manual" else None,
        "affected": {"messages": 0, "sessions": 0, "read_cursors": 0, "group_members": 0},
    }


# ---------- shared fixture ----------

def seed_basic(db_path: str):
    """One mapping per table dimension, no collisions."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")

    conn.execute(
        "INSERT INTO messages (sender_project, sender, recipient_project, recipient, kind, body, created_at)"
        " VALUES ('OldProj','alice','SomeOtherProj','carol','msg','hi',1)"
    )
    conn.execute(
        "INSERT INTO messages (sender_project, sender, recipient_project, recipient, kind, body, created_at)"
        " VALUES ('SomeOtherProj','dave','OldProj','alice','msg','both-sides',2)"
    )

    conn.execute(
        "INSERT INTO sessions (project, name, cwd, host, last_seen) VALUES "
        "('OldProj','alice','/old/dir','host-a', 100)"
    )

    conn.execute(
        "INSERT INTO read_cursors (project, member_name, cursor, updated_at) VALUES "
        "('OldProj','alice', 10, 500)"
    )

    conn.execute("INSERT INTO groups (project, name, created_at) VALUES ('CanonProj','team', 1)")
    conn.execute(
        "INSERT INTO group_members (group_project, group_name, member_project, member_name, added_at)"
        " VALUES ('CanonProj','team','OldProj','alice', 1)"
    )

    conn.commit()
    conn.close()


BASIC_CANONICAL = [mk("alice", "CanonProj"), mk("carol", "SomeOtherProj"), mk("dave", "SomeOtherProj")]
BASIC_MAPPINGS = [mapping_entry("alice", "OldProj", "alice", "CanonProj")]


# ---------- tests ----------

def test_messages_basic_rewrite():
    print("\n[1] messages: both endpoints independently rewritten")
    db = new_db()
    seed_basic(db)
    remap = make_remap(BASIC_CANONICAL, BASIC_MAPPINGS)
    migration.apply_or_dry_run(db, remap, dry_run=False)

    conn = sqlite3.connect(db)
    row1 = conn.execute(
        "SELECT sender_project, sender, recipient_project, recipient FROM messages WHERE sender='alice'"
    ).fetchone()
    check("sender-side endpoint rewritten", row1 == ("CanonProj", "alice", "SomeOtherProj", "carol"), str(row1))

    row2 = conn.execute(
        "SELECT sender_project, sender, recipient_project, recipient FROM messages WHERE sender='dave'"
    ).fetchone()
    check("recipient-side endpoint rewritten, sender untouched", row2 == ("SomeOtherProj", "dave", "CanonProj", "alice"), str(row2))
    conn.close()


def test_sessions_collision_last_seen():
    print("\n[2] sessions PK collision merges by greater last_seen (tie favors canonical)")
    db = new_db()
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO sessions (project, name, cwd, host, last_seen) VALUES ('OldProj','bob','/old','host-a', 999)"
    )
    conn.execute(
        "INSERT INTO sessions (project, name, cwd, host, last_seen) VALUES ('CanonProj','bob','/new','host-b', 100)"
    )
    conn.commit()
    conn.close()

    remap = make_remap([mk("bob", "CanonProj")], [mapping_entry("bob", "OldProj", "bob", "CanonProj")])
    migration.apply_or_dry_run(db, remap, dry_run=False)

    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT project, name, cwd, last_seen FROM sessions WHERE name='bob'").fetchall()
    check("exactly one surviving row", len(rows) == 1, str(rows))
    if rows:
        check("survivor is the greater-last_seen row's fields, rekeyed to canonical",
              rows[0] == ("CanonProj", "bob", "/old", 999), str(rows))
    conn.close()

    # tie -> canonical wins
    db2 = new_db()
    conn = sqlite3.connect(db2)
    conn.execute(
        "INSERT INTO sessions (project, name, cwd, host, last_seen) VALUES ('OldProj','bob','/old','host-a', 500)"
    )
    conn.execute(
        "INSERT INTO sessions (project, name, cwd, host, last_seen) VALUES ('CanonProj','bob','/new','host-b', 500)"
    )
    conn.commit()
    conn.close()
    migration.apply_or_dry_run(db2, remap, dry_run=False)
    conn = sqlite3.connect(db2)
    rows = conn.execute("SELECT project, name, cwd, last_seen FROM sessions WHERE name='bob'").fetchall()
    check("tie favors canonical row's fields", rows == [("CanonProj", "bob", "/new", 500)], str(rows))
    conn.close()


def test_read_cursors_collision_max():
    print("\n[3] read_cursors PK collision merges by MAX(cursor), MAX(updated_at)")
    db = new_db()
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO read_cursors (project, member_name, cursor, updated_at) VALUES ('OldProj','m', 10, 100)")
    conn.execute("INSERT INTO read_cursors (project, member_name, cursor, updated_at) VALUES ('CanonProj','m', 5, 900)")
    conn.commit()
    conn.close()

    remap = make_remap([mk("m", "CanonProj")], [mapping_entry("m", "OldProj", "m", "CanonProj")])
    migration.apply_or_dry_run(db, remap, dry_run=False)

    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT project, member_name, cursor, updated_at FROM read_cursors WHERE member_name='m'").fetchall()
    check("exactly one surviving row", len(rows) == 1, str(rows))
    if rows:
        check("cursor=MAX(10,5)=10, updated_at=MAX(100,900)=900",
              rows[0] == ("CanonProj", "m", 10, 900), str(rows))
    conn.close()


def test_group_members_basic_rewrite():
    print("\n[4] group_members: group-side and member-side rewritten together, groups FK stays clean")
    db = new_db()
    seed_basic(db)
    remap = make_remap(BASIC_CANONICAL, BASIC_MAPPINGS)
    migration.apply_or_dry_run(db, remap, dry_run=False)

    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = ON;")
    rows = conn.execute(
        "SELECT group_project, group_name, member_project, member_name FROM group_members"
    ).fetchall()
    check("member-side rewritten to canonical", rows == [("CanonProj", "team", "CanonProj", "alice")], str(rows))
    fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    check("no FK violations", fk_violations == [], str(fk_violations))
    conn.close()


def test_deletions_guard_blocks_when_messages_exist():
    print("\n[5] deletions guard: aborts + rolls back when a deletion key still carries messages")
    db = new_db()
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO messages (sender_project, sender, recipient_project, recipient, kind, body, created_at)"
        " VALUES ('DeadProj','ghost','SomeOtherProj','carol','msg','still here',1)"
    )
    conn.execute(
        "INSERT INTO sessions (project, name, cwd, host, last_seen) VALUES ('DeadProj','ghost','/d','h', 1)"
    )
    conn.commit()
    conn.close()
    before = dump_state(db)

    remap = make_remap([], [], deletions=[{"key": mk("ghost", "DeadProj"), "note": "test", "affected": {}}])
    raised = None
    try:
        migration.apply_or_dry_run(db, remap, dry_run=False)
    except Exception as e:
        raised = e
    check("apply raises on dirty deletion key", raised is not None, "expected RuntimeError")
    after = dump_state(db)
    check("DB state unchanged (rolled back)", before == after, "deletions guard did not roll back cleanly")


def test_idempotent_second_apply_is_noop():
    print("\n[6] running --apply twice: second run is a no-op, no duplicate log rows")
    db = new_db()
    seed_basic(db)
    remap = make_remap(BASIC_CANONICAL, BASIC_MAPPINGS)
    migration.apply_or_dry_run(db, remap, dry_run=False)
    state1 = dump_state(db)
    log1 = log_rows(db)

    migration.apply_or_dry_run(db, remap, dry_run=False)
    state2 = dump_state(db)
    log2 = log_rows(db)

    check("DB state unchanged on second apply", state1 == state2, "state differs across re-run")
    check("no new log rows written on second apply", len(log1) == len(log2), f"{len(log1)} -> {len(log2)}")


def test_dry_run_no_mutation():
    print("\n[7] --dry-run does not mutate the DB")
    db = new_db()
    seed_basic(db)
    remap = make_remap(BASIC_CANONICAL, BASIC_MAPPINGS)
    before = dump_state(db)
    migration.apply_or_dry_run(db, remap, dry_run=True)
    after = dump_state(db)
    check("DB state identical after dry-run", before == after, "dry-run mutated the DB")


def test_self_check_pass_on_clean_migration():
    print("\n[8] self-check passes after a clean --apply")
    db = new_db()
    seed_basic(db)
    remap = make_remap(BASIC_CANONICAL, BASIC_MAPPINGS)
    migration.apply_or_dry_run(db, remap, dry_run=False)
    problems = migration.self_check(db, remap)
    check("no self-check problems", problems == [], str(problems))


def test_self_check_catches_unreachable_endpoint():
    print("\n[9] self-check catches a message endpoint that never lands in canonical")
    db = new_db()
    conn = sqlite3.connect(db)
    # 'orphan' is never mentioned by any mapping and is not in canonical -- a
    # drifted identity the remap table failed to capture.
    conn.execute(
        "INSERT INTO messages (sender_project, sender, recipient_project, recipient, kind, body, created_at)"
        " VALUES ('orphan-proj','orphan','CanonProj','alice','msg','uncaptured drift',1)"
    )
    conn.commit()
    conn.close()

    remap = make_remap(BASIC_CANONICAL, BASIC_MAPPINGS)
    migration.apply_or_dry_run(db, remap, dry_run=False)
    problems = migration.self_check(db, remap)
    check("self-check flags the unreachable sender endpoint",
          any("orphan" in p for p in problems), str(problems))


def test_validate_remap_rejects_schema_mismatch():
    print("\n[10] validate_remap rejects a wrong/missing schema field")
    remap = make_remap([], [])
    remap["schema"] = "some-other-schema@1"
    errors = migration.validate_remap(remap)
    check("schema mismatch produces an error", len(errors) == 1 and "schema mismatch" in errors[0], str(errors))


def test_validate_remap_rejects_chained_mapping():
    print("\n[11] validate_remap rejects a chained mapping (to == another from)")
    remap = make_remap(
        [mk("x", "Z")],
        [mapping_entry("x", "A", "x", "B"), mapping_entry("x", "B", "x", "Z")],
    )
    errors = migration.validate_remap(remap)
    check("chained mapping produces an error", any("chained" in e for e in errors), str(errors))


# ---------- reverse migration (most important test) ----------

def _pk_cols(table):
    return {
        "sessions": ["project", "name"],
        "read_cursors": ["project", "member_name"],
        "group_members": ["group_project", "group_name", "member_project", "member_name"],
        "groups": ["project", "name"],
    }[table]


def revert_from_log(db_path: str):
    """Reconstructs pre-migration DB state purely from identity_remap_log,
    proving the log carries enough information to reverse the migration.
    This is deliberately a test-only helper (production script exposes no
    --revert flag; the contract only requires the log to make reversal
    POSSIBLE, not to ship a one-command undo)."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = OFF;")  # revert may transiently violate FK mid-flight
    for table in ("messages", "sessions", "read_cursors", "groups", "group_members"):
        rows = conn.execute(
            "SELECT row_key, column_name, old_value, new_value, op FROM identity_remap_log "
            "WHERE table_name=? ORDER BY id", (table,)
        ).fetchall()

        updates: dict = {}
        snapshots: list = []
        for row_key, column, old_value, new_value, op in rows:
            if op in ("update", "override"):
                updates.setdefault(row_key, {})[column] = (old_value, new_value)
            elif op in ("merge-drop", "delete"):
                snapshots.append(json.loads(old_value))

        # pass 1: undo update-groups, locating the row by its CURRENT (new) key
        for row_key, cols in updates.items():
            if table == "messages":
                set_sql = ", ".join(f"{c}=?" for c in cols)
                params = [old for old, new in cols.values()] + [int(row_key)]
                conn.execute(f"UPDATE messages SET {set_sql} WHERE id=?", params)
            else:
                pk_cols = _pk_cols(table)
                # row_key encodes the PK as it was BEFORE this event. Columns
                # that didn't move (e.g. a read_cursors collision winner,
                # whose PK never changes -- only cursor/updated_at do) have no
                # entry in `cols`, so their current value IS the row_key's
                # part; only PK columns present in `cols` were rekeyed.
                current_pk = dict(zip(pk_cols, row_key.split("|")))
                for c in pk_cols:
                    if c in cols:
                        current_pk[c] = cols[c][1]
                set_sql = ", ".join(f"{c}=?" for c in cols)
                set_params = [cols[c][0] for c in cols]
                where_sql = " AND ".join(f"{c}=?" for c in pk_cols)
                where_params = [current_pk[c] for c in pk_cols]
                conn.execute(f"UPDATE {table} SET {set_sql} WHERE {where_sql}", set_params + where_params)

        # pass 2: reinsert dropped/deleted rows (must happen after pass 1
        # frees up any PK slot a merge-drop's surviving sibling vacated)
        for snap in snapshots:
            cols = list(snap.keys())
            placeholders = ", ".join("?" for _ in cols)
            conn.execute(
                f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})",
                [snap[c] for c in cols],
            )

    conn.execute("DELETE FROM identity_remap_log")
    conn.commit()
    conn.close()


def test_reverse_migration_restores_original_state():
    print("\n[12] reverse migration: replaying the log restores the exact pre-migration DB state")
    db = new_db()
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = ON;")
    # messages: both-sides rewrite
    conn.execute(
        "INSERT INTO messages (sender_project, sender, recipient_project, recipient, kind, body, created_at)"
        " VALUES ('OldProj','alice','OldProj','alice','msg','hi',1)"
    )
    # sessions: simple rekey (no collision)
    conn.execute(
        "INSERT INTO sessions (project, name, cwd, host, last_seen) VALUES ('OldProj','alice','/old','h-a', 50)"
    )
    # sessions: collision (from wins)
    conn.execute(
        "INSERT INTO sessions (project, name, cwd, host, last_seen) VALUES ('OldProj','bob','/old-b','h-b', 999)"
    )
    conn.execute(
        "INSERT INTO sessions (project, name, cwd, host, last_seen) VALUES ('CanonProj','bob','/new-b','h-c', 100)"
    )
    # read_cursors: collision (merge by MAX)
    conn.execute("INSERT INTO read_cursors (project, member_name, cursor, updated_at) VALUES ('OldProj','alice', 10, 500)")
    conn.execute("INSERT INTO read_cursors (project, member_name, cursor, updated_at) VALUES ('CanonProj','alice', 3, 200)")
    # group_members: member-side rekey, no collision
    conn.execute("INSERT INTO groups (project, name, created_at, creator, charter) VALUES ('CanonProj','team', 1, 'tommy', 'charter text')")
    conn.execute(
        "INSERT INTO group_members (group_project, group_name, member_project, member_name, added_at)"
        " VALUES ('CanonProj','team','OldProj','alice', 7)"
    )
    conn.commit()
    conn.close()

    before = dump_state(db)

    remap = make_remap(
        [mk("alice", "CanonProj"), mk("bob", "CanonProj")],
        [
            mapping_entry("alice", "OldProj", "alice", "CanonProj"),
            mapping_entry("bob", "OldProj", "bob", "CanonProj"),
        ],
    )
    migration.apply_or_dry_run(db, remap, dry_run=False)
    after_migration = dump_state(db)
    check("migration actually changed DB state", before != after_migration)

    revert_from_log(db)
    restored = dump_state(db)
    check("DB state after revert matches pre-migration snapshot", restored == before, "revert did not reconstruct original state")


# ---------- main ----------

def main():
    test_messages_basic_rewrite()
    test_sessions_collision_last_seen()
    test_read_cursors_collision_max()
    test_group_members_basic_rewrite()
    test_deletions_guard_blocks_when_messages_exist()
    test_idempotent_second_apply_is_noop()
    test_dry_run_no_mutation()
    test_self_check_pass_on_clean_migration()
    test_self_check_catches_unreachable_endpoint()
    test_validate_remap_rejects_schema_mismatch()
    test_validate_remap_rejects_chained_mapping()
    test_reverse_migration_restores_original_state()

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
