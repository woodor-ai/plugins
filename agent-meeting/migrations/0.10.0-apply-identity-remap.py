#!/usr/bin/env python3
"""
Stage 0.3 of docs/contracts/0.10.0-composite-key-identity.md: apply an
explicit identity remap table (docs/contracts/identity-remap-schema.md v1)
to the central meeting DB ($MEETING_HOME/db/rooms.db).

WHY: stage 0.2's generator (0.10.0-identity-remap.py) produces a read-only
JSON remap table -- {old (name, project)} -> {canonical (name, project)} --
that is the sole source of truth for folding drifted session identities.
This script is its downstream consumer: it rewrites messages / sessions /
read_cursors / group_members (and, where the remap table's keys happen to
also be group identities, groups) so every row's composite key lands on a
canonical identity. Every rewritten row is logged to identity_remap_log so
the migration is reversible -- per the contract, reversibility matters more
than a clean acceptance run.

Tables touched (all in one BEGIN IMMEDIATE transaction):
  messages.sender_project / sender, recipient_project / recipient
  sessions            PK (project, name)
  read_cursors        PK (project, member_name)
  groups              PK (project, name)          -- only if a mapping key
                                                       happens to be a group
  group_members       PK (group_project, group_name, member_project,
                           member_name)            -- FK -> groups(project,
                                                       name)

Modes
  --report                 read-only: validate the remap table, print
                            per-mapping predicted impact and the deletions
                            pre-check. Default mode if no mode flag given.
  --dry-run                run the full rewrite inside a transaction, print
                            actual rows changed, then ROLLBACK.
  --apply                  same rewrite, COMMIT, then run the post-migration
                            self-check (unreachable identities, dangling FKs,
                            leftover 'from' keys). Self-check failure exits
                            non-zero but does NOT roll back (already
                            committed) -- see contract doc note in the CLAUDE
                            task: "自检失败不回滚，已提交".

Examples
  python3 0.10.0-apply-identity-remap.py --report
  python3 0.10.0-apply-identity-remap.py --dry-run
  python3 0.10.0-apply-identity-remap.py --apply

Input: docs/contracts/identity-remap.json (fixed name, no date stamp);
override with --remap. Never touches a DB other than the one --db (or the
default $MEETING_HOME/db/rooms.db) points at, and never guesses a mapping
that is not explicitly present in the remap file.
"""

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCHEMA_VERSION = "agent-meeting/identity-remap@1"
MIGRATION_NAME = "0.10.0-identity-remap"

OVERRIDE_PK = {
    "messages": ["id"],
    "sessions": ["project", "name"],
    "read_cursors": ["project", "member_name"],
    "group_members": ["group_project", "group_name", "member_project", "member_name"],
}


def default_db_path() -> str:
    home = os.environ.get("MEETING_HOME") or os.path.expanduser("~/.agent-meeting")
    return os.path.join(home, "db", "rooms.db")


def default_remap_path() -> str:
    return os.path.join(REPO_ROOT, "docs", "contracts", "identity-remap.json")


# ---------- remap loading & validation ----------

def load_remap(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def validate_remap(remap: dict) -> list[str]:
    """Re-validate the hard constraints the generator already enforced:
    'from' uniqueness, 'to' membership in canonical, no chained mappings.
    Never trust the input file blindly -- this is a system boundary."""
    errors = []
    if remap.get("schema") != SCHEMA_VERSION:
        errors.append(f"schema mismatch: expected {SCHEMA_VERSION!r}, got {remap.get('schema')!r}")
        return errors  # nothing else in the file can be trusted

    canonical_keys = {(c["name"], c["project"]) for c in remap["canonical"]}
    from_keys_list = [(m["from"]["name"], m["from"]["project"]) for m in remap["mappings"]]
    from_keys = set(from_keys_list)
    if len(from_keys_list) != len(from_keys):
        seen = set()
        for k in from_keys_list:
            if k in seen:
                errors.append(f"duplicate mapping 'from' key: {k!r}")
            seen.add(k)

    for m in remap["mappings"]:
        to_key = (m["to"]["name"], m["to"]["project"])
        if to_key not in canonical_keys:
            errors.append(f"mapping to {to_key!r} is not in the canonical set (unreachable)")
        if to_key in from_keys:
            errors.append(f"chained mapping detected: {to_key!r} is both a 'to' target and a 'from' source")

    return errors


def build_mapping(remap: dict) -> dict:
    """{(name, project): (to_name, to_project)}"""
    return {
        (m["from"]["name"], m["from"]["project"]): (m["to"]["name"], m["to"]["project"])
        for m in remap["mappings"]
    }


# ---------- row-level source log ----------

def ensure_log_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS identity_remap_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          migration TEXT NOT NULL,
          table_name TEXT NOT NULL,
          row_key TEXT NOT NULL,
          column_name TEXT NOT NULL,
          old_value TEXT NOT NULL,
          new_value TEXT NOT NULL,
          mapping_from TEXT NOT NULL,
          mapping_to TEXT NOT NULL,
          op TEXT NOT NULL,
          applied_at INTEGER NOT NULL
        )
    """)


def log_row(conn, applied_at, table, row_key, column, old_value, new_value, mapping_from, mapping_to, op):
    conn.execute(
        "INSERT INTO identity_remap_log "
        "(migration, table_name, row_key, column_name, old_value, new_value, mapping_from, mapping_to, op, applied_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            MIGRATION_NAME, table, row_key, column,
            "" if old_value is None else str(old_value),
            "" if new_value is None else str(new_value),
            mapping_from, mapping_to, op, applied_at,
        ),
    )


def key_str(*parts) -> str:
    return "|".join(str(p) for p in parts)


def mapping_key_json(name, project) -> str:
    return json.dumps({"name": name, "project": project}, ensure_ascii=False)


def row_snapshot(conn, table, where_sql, where_params):
    cur = conn.execute(f"SELECT * FROM {table} WHERE {where_sql}", where_params)
    row = cur.fetchone()
    if row is None:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


# ---------- per-table rewrites ----------

def rewrite_messages(conn, mapping, applied_at) -> int:
    """messages has no uniqueness constraint on (project, sender)/(project,
    recipient) so there is never a collision to resolve -- a plain rewrite
    per endpoint. Both endpoints of the same row can independently match
    different mapping entries."""
    changed = 0
    for (from_name, from_project), (to_name, to_project) in mapping.items():
        mf, mt = mapping_key_json(from_name, from_project), mapping_key_json(to_name, to_project)

        ids = [r[0] for r in conn.execute(
            "SELECT id FROM messages WHERE sender_project=? AND sender=?", (from_project, from_name)
        )]
        for id_ in ids:
            log_row(conn, applied_at, "messages", str(id_), "sender_project", from_project, to_project, mf, mt, "update")
            log_row(conn, applied_at, "messages", str(id_), "sender", from_name, to_name, mf, mt, "update")
        if ids:
            conn.execute(
                "UPDATE messages SET sender_project=?, sender=? WHERE sender_project=? AND sender=?",
                (to_project, to_name, from_project, from_name),
            )
            changed += len(ids)

        ids = [r[0] for r in conn.execute(
            "SELECT id FROM messages WHERE recipient_project=? AND recipient=?", (from_project, from_name)
        )]
        for id_ in ids:
            log_row(conn, applied_at, "messages", str(id_), "recipient_project", from_project, to_project, mf, mt, "update")
            log_row(conn, applied_at, "messages", str(id_), "recipient", from_name, to_name, mf, mt, "update")
        if ids:
            conn.execute(
                "UPDATE messages SET recipient_project=?, recipient=? WHERE recipient_project=? AND recipient=?",
                (to_project, to_name, from_project, from_name),
            )
            changed += len(ids)
    return changed


def rewrite_sessions(conn, mapping, applied_at) -> int:
    """PK (project, name). On collision keep the row with the greater
    last_seen (ties favor the canonical/'to' row); the loser is dropped and
    its full pre-drop row is logged (op=merge-drop) so it can be replayed
    back on revert."""
    changed = 0
    for (from_name, from_project), (to_name, to_project) in mapping.items():
        from_row = row_snapshot(conn, "sessions", "project=? AND name=?", (from_project, from_name))
        if from_row is None:
            continue
        mf, mt = mapping_key_json(from_name, from_project), mapping_key_json(to_name, to_project)
        old_key = key_str(from_project, from_name)
        to_row = row_snapshot(conn, "sessions", "project=? AND name=?", (to_project, to_name))

        if to_row is None:
            conn.execute(
                "UPDATE sessions SET project=?, name=? WHERE project=? AND name=?",
                (to_project, to_name, from_project, from_name),
            )
            log_row(conn, applied_at, "sessions", old_key, "project", from_project, to_project, mf, mt, "update")
            log_row(conn, applied_at, "sessions", old_key, "name", from_name, to_name, mf, mt, "update")
        else:
            from_ts = from_row["last_seen"] if from_row["last_seen"] is not None else -1
            to_ts = to_row["last_seen"] if to_row["last_seen"] is not None else -1
            to_key = key_str(to_project, to_name)
            if from_ts > to_ts:
                log_row(conn, applied_at, "sessions", to_key, "*",
                        json.dumps(to_row, ensure_ascii=False), "", mf, mt, "merge-drop")
                conn.execute("DELETE FROM sessions WHERE project=? AND name=?", (to_project, to_name))
                conn.execute(
                    "UPDATE sessions SET project=?, name=? WHERE project=? AND name=?",
                    (to_project, to_name, from_project, from_name),
                )
                log_row(conn, applied_at, "sessions", old_key, "project", from_project, to_project, mf, mt, "update")
                log_row(conn, applied_at, "sessions", old_key, "name", from_name, to_name, mf, mt, "update")
            else:
                log_row(conn, applied_at, "sessions", old_key, "*",
                        json.dumps(from_row, ensure_ascii=False), "", mf, mt, "merge-drop")
                conn.execute("DELETE FROM sessions WHERE project=? AND name=?", (from_project, from_name))
        changed += 1
    return changed


def rewrite_read_cursors(conn, mapping, applied_at) -> int:
    """PK (project, member_name). On collision merge by MAX(cursor) /
    MAX(updated_at) -- a cursor may only move forward, never backward, or a
    fold would replay old backlog."""
    changed = 0
    for (from_name, from_project), (to_name, to_project) in mapping.items():
        from_row = row_snapshot(conn, "read_cursors", "project=? AND member_name=?", (from_project, from_name))
        if from_row is None:
            continue
        mf, mt = mapping_key_json(from_name, from_project), mapping_key_json(to_name, to_project)
        old_key = key_str(from_project, from_name)
        to_row = row_snapshot(conn, "read_cursors", "project=? AND member_name=?", (to_project, to_name))

        if to_row is None:
            conn.execute(
                "UPDATE read_cursors SET project=?, member_name=? WHERE project=? AND member_name=?",
                (to_project, to_name, from_project, from_name),
            )
            log_row(conn, applied_at, "read_cursors", old_key, "project", from_project, to_project, mf, mt, "update")
            log_row(conn, applied_at, "read_cursors", old_key, "member_name", from_name, to_name, mf, mt, "update")
        else:
            new_cursor = max(from_row["cursor"], to_row["cursor"])
            new_updated_at = max(from_row["updated_at"], to_row["updated_at"])
            log_row(conn, applied_at, "read_cursors", old_key, "*",
                    json.dumps(from_row, ensure_ascii=False), "", mf, mt, "merge-drop")
            conn.execute("DELETE FROM read_cursors WHERE project=? AND member_name=?", (from_project, from_name))
            to_key = key_str(to_project, to_name)
            if new_cursor != to_row["cursor"]:
                log_row(conn, applied_at, "read_cursors", to_key, "cursor", to_row["cursor"], new_cursor, mf, mt, "update")
            if new_updated_at != to_row["updated_at"]:
                log_row(conn, applied_at, "read_cursors", to_key, "updated_at",
                        to_row["updated_at"], new_updated_at, mf, mt, "update")
            conn.execute(
                "UPDATE read_cursors SET cursor=?, updated_at=? WHERE project=? AND member_name=?",
                (new_cursor, new_updated_at, to_project, to_name),
            )
        changed += 1
    return changed


def rewrite_groups(conn, mapping, applied_at):
    """groups PK (project, name). The remap table's keys are agent
    identities, not group identities, but the schema doesn't distinguish the
    two shapes -- if a mapping key happens to also be a group's (project,
    name), group_members' FK to groups forces us to rewrite it too, or the
    FK check fails. On a groups PK collision there is no defined winner rule
    (charter/creator have no merge semantics) so, matching the 0.8.55
    precedent, skip and warn rather than guess a merge."""
    changed = 0
    warnings: list[str] = []
    skipped_keys: set = set()
    rows = conn.execute("SELECT project, name FROM groups").fetchall()
    for gp, gn in rows:
        target = mapping.get((gn, gp))
        if target is None:
            continue
        to_name, to_project = target
        existing = conn.execute("SELECT 1 FROM groups WHERE project=? AND name=?", (to_project, to_name)).fetchone()
        if existing:
            warnings.append(
                f"groups: skipped remap ({gn}@{gp!r}) -> ({to_name}@{to_project!r}); "
                f"target group already exists, needs manual merge"
            )
            skipped_keys.add((gn, gp))
            continue
        mf, mt = mapping_key_json(gn, gp), mapping_key_json(to_name, to_project)
        old_key = key_str(gp, gn)
        conn.execute("UPDATE groups SET project=?, name=? WHERE project=? AND name=?", (to_project, to_name, gp, gn))
        log_row(conn, applied_at, "groups", old_key, "project", gp, to_project, mf, mt, "update")
        log_row(conn, applied_at, "groups", old_key, "name", gn, to_name, mf, mt, "update")
        changed += 1
    return changed, warnings, skipped_keys


def rewrite_group_members(conn, mapping, applied_at, skipped_group_keys) -> int:
    """PK (group_project, group_name, member_project, member_name). Both
    halves of the composite key can independently need remapping, so this
    resolves both in a single pass per row (not two separate passes) --
    doing group-side and member-side as separate operations would each need
    their own row_key, and the second pass's 'before' key would already
    reflect the first pass's rewrite, breaking the log's 'pre-migration key'
    invariant. Group-side keys that rewrite_groups() had to skip (PK
    collision) are left unmapped here too, or group_members would point at a
    groups row that was never created.

    Collision (both halves resolve to a PK that's already occupied): keep
    the earlier added_at (ties favor the canonical/'to' row already in
    place); the loser is dropped and logged in full for revert."""
    changed = 0
    rows = conn.execute(
        "SELECT group_project, group_name, member_project, member_name, added_at FROM group_members"
    ).fetchall()
    for gp, gn, mp, mn, added_at in rows:
        group_target = mapping.get((gn, gp))
        if group_target is not None and (gn, gp) not in skipped_group_keys:
            new_gn, new_gp = group_target
        else:
            new_gn, new_gp = gn, gp
        member_target = mapping.get((mn, mp))
        if member_target is not None:
            new_mn, new_mp = member_target
        else:
            new_mn, new_mp = mn, mp

        if (new_gp, new_gn, new_mp, new_mn) == (gp, gn, mp, mn):
            continue

        old_key = key_str(gp, gn, mp, mn)
        mf_parts, mt_parts = [], []
        if (new_gn, new_gp) != (gn, gp):
            mf_parts.append(mapping_key_json(gn, gp))
            mt_parts.append(mapping_key_json(new_gn, new_gp))
        if (new_mn, new_mp) != (mn, mp):
            mf_parts.append(mapping_key_json(mn, mp))
            mt_parts.append(mapping_key_json(new_mn, new_mp))
        mf, mt = " + ".join(mf_parts), " + ".join(mt_parts)

        existing = row_snapshot(
            conn, "group_members",
            "group_project=? AND group_name=? AND member_project=? AND member_name=?",
            (new_gp, new_gn, new_mp, new_mn),
        )
        if existing is None:
            conn.execute(
                "UPDATE group_members SET group_project=?, group_name=?, member_project=?, member_name=? "
                "WHERE group_project=? AND group_name=? AND member_project=? AND member_name=?",
                (new_gp, new_gn, new_mp, new_mn, gp, gn, mp, mn),
            )
            for col, old_v, new_v in (
                ("group_project", gp, new_gp), ("group_name", gn, new_gn),
                ("member_project", mp, new_mp), ("member_name", mn, new_mn),
            ):
                log_row(conn, applied_at, "group_members", old_key, col, old_v, new_v, mf, mt, "update")
        elif added_at < existing["added_at"]:
            new_key = key_str(new_gp, new_gn, new_mp, new_mn)
            log_row(conn, applied_at, "group_members", new_key, "*",
                    json.dumps(existing, ensure_ascii=False), "", mf, mt, "merge-drop")
            conn.execute(
                "DELETE FROM group_members WHERE group_project=? AND group_name=? "
                "AND member_project=? AND member_name=?",
                (new_gp, new_gn, new_mp, new_mn),
            )
            conn.execute(
                "UPDATE group_members SET group_project=?, group_name=?, member_project=?, member_name=? "
                "WHERE group_project=? AND group_name=? AND member_project=? AND member_name=?",
                (new_gp, new_gn, new_mp, new_mn, gp, gn, mp, mn),
            )
            for col, old_v, new_v in (
                ("group_project", gp, new_gp), ("group_name", gn, new_gn),
                ("member_project", mp, new_mp), ("member_name", mn, new_mn),
            ):
                log_row(conn, applied_at, "group_members", old_key, col, old_v, new_v, mf, mt, "update")
        else:
            snapshot = row_snapshot(
                conn, "group_members",
                "group_project=? AND group_name=? AND member_project=? AND member_name=?",
                (gp, gn, mp, mn),
            )
            log_row(conn, applied_at, "group_members", old_key, "*",
                    json.dumps(snapshot, ensure_ascii=False), "", mf, mt, "merge-drop")
            conn.execute(
                "DELETE FROM group_members WHERE group_project=? AND group_name=? "
                "AND member_project=? AND member_name=?",
                (gp, gn, mp, mn),
            )
        changed += 1
    return changed


# ---------- deletions ----------

def check_deletions_clean(conn, remap) -> list[str]:
    """Last-chance guard against a bad deletions entry: a key that carries
    any message endpoint must go through mappings, not deletions (the
    generator enforces this at production time; verify it again here since
    this script must never trust an input file blindly)."""
    errors = []
    for d in remap["deletions"]:
        name, project = d["key"]["name"], d["key"]["project"]
        cnt = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE (sender_project=? AND sender=?) OR (recipient_project=? AND recipient=?)",
            (project, name, project, name),
        ).fetchone()[0]
        if cnt:
            errors.append(f"deletion key {name}@{project!r} carries {cnt} message endpoint(s); refusing to delete")
    return errors


def apply_deletions(conn, remap, applied_at) -> dict:
    changed = {"sessions": 0, "read_cursors": 0}
    for d in remap["deletions"]:
        name, project = d["key"]["name"], d["key"]["project"]
        key = key_str(project, name)
        mf = mapping_key_json(name, project)

        row = row_snapshot(conn, "sessions", "project=? AND name=?", (project, name))
        if row is not None:
            log_row(conn, applied_at, "sessions", key, "*", json.dumps(row, ensure_ascii=False), "", mf, "", "delete")
            conn.execute("DELETE FROM sessions WHERE project=? AND name=?", (project, name))
            changed["sessions"] += 1

        row = row_snapshot(conn, "read_cursors", "project=? AND member_name=?", (project, name))
        if row is not None:
            log_row(conn, applied_at, "read_cursors", key, "*", json.dumps(row, ensure_ascii=False), "", mf, "", "delete")
            conn.execute("DELETE FROM read_cursors WHERE project=? AND member_name=?", (project, name))
            changed["read_cursors"] += 1
    return changed


# ---------- row_overrides ----------

def apply_row_overrides(conn, remap, applied_at) -> int:
    """Currently always an empty list in practice; implemented so the
    format is exercised, not just documented. Idempotent: a row whose
    current value no longer matches override['from'] is treated as already
    applied and skipped, not re-applied."""
    changed = 0
    for ov in remap["row_overrides"]:
        table = ov["table"]
        pk_cols = OVERRIDE_PK[table]
        pk_values = [ov["row_id"]] if table == "messages" else ov["row_id"].split("|")
        column = ov["column"]
        where_sql = " AND ".join(f"{c}=?" for c in pk_cols)
        current = conn.execute(f"SELECT {column} FROM {table} WHERE {where_sql}", pk_values).fetchone()
        if current is None or str(current[0]) != str(ov["from"]):
            continue
        conn.execute(f"UPDATE {table} SET {column}=? WHERE {where_sql}", [ov["to"]] + pk_values)
        log_row(conn, applied_at, table, ov["row_id"], column, ov["from"], ov["to"], "override", "override", "override")
        changed += 1
    return changed


# ---------- apply / dry-run ----------

def apply_or_dry_run(db_path: str, remap: dict, dry_run: bool) -> dict:
    if not dry_run:
        bak_path = db_path + ".pre-0.10.0-remap.bak"
        if os.path.exists(bak_path):
            print(f"backup already exists, skipping: {bak_path}")
        else:
            shutil.copy2(db_path, bak_path)
            print(f"backup written: {bak_path}")

    mapping = build_mapping(remap)
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=30)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("BEGIN IMMEDIATE")
    # groups and its group_members children are reprojected in separate
    # statements; with immediate FK enforcement the parent UPDATE is
    # rejected as orphaning its children until the child UPDATE also runs.
    # Deferring resets automatically at COMMIT/ROLLBACK, scoped to this txn.
    conn.execute("PRAGMA defer_foreign_keys = ON")

    applied_at = int(time.time())
    summary = {}
    groups_warnings: list[str] = []
    deletions_summary = {"sessions": 0, "read_cursors": 0}
    override_changed = 0
    try:
        ensure_log_table(conn)

        errors = check_deletions_clean(conn, remap)
        if errors:
            raise RuntimeError("deletions pre-check failed:\n" + "\n".join(f"  - {e}" for e in errors))

        override_changed = apply_row_overrides(conn, remap, applied_at)

        summary["messages"] = rewrite_messages(conn, mapping, applied_at)
        summary["sessions"] = rewrite_sessions(conn, mapping, applied_at)
        summary["read_cursors"] = rewrite_read_cursors(conn, mapping, applied_at)
        groups_changed, groups_warnings, skipped_group_keys = rewrite_groups(conn, mapping, applied_at)
        summary["groups"] = groups_changed
        summary["group_members"] = rewrite_group_members(conn, mapping, applied_at, skipped_group_keys)

        deletions_summary = apply_deletions(conn, remap, applied_at)

        if dry_run:
            conn.execute("ROLLBACK")
            print("[dry-run] no changes committed")
        else:
            conn.execute("COMMIT")
            print("changes committed")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    print(f"\nrow_overrides applied: {override_changed}")
    print("\nper-table rows changed:")
    for table, count in summary.items():
        print(f"  {table:<15} {count}")
    print(f"\ndeletions: sessions={deletions_summary['sessions']} read_cursors={deletions_summary['read_cursors']}")
    if groups_warnings:
        print("\nwarnings:")
        for w in groups_warnings:
            print(f"  - {w}")

    return summary


# ---------- post-migration self-check (--apply only) ----------

def self_check(db_path: str, remap: dict) -> list[str]:
    """Runs against the committed DB on a fresh connection (self-check
    failure must NOT roll back already-committed data -- the contract's
    stance is that a loud failure after commit beats a silent rollback that
    hides the acceptance gap)."""
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA foreign_keys = ON;")
    problems = []

    from_keys = {(m["from"]["name"], m["from"]["project"]) for m in remap["mappings"]}
    canonical_keys = {(c["name"], c["project"]) for c in remap["canonical"]}

    # 1. no remaining rows equal to any mapping 'from' key, in any of the four tables
    for name, project in from_keys:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE (sender_project=? AND sender=?) OR (recipient_project=? AND recipient=?)",
            (project, name, project, name),
        ).fetchone()[0]
        if cnt:
            problems.append(f"messages: {cnt} row(s) still reference from-key {name}@{project!r}")
        if conn.execute("SELECT 1 FROM sessions WHERE project=? AND name=?", (project, name)).fetchone():
            problems.append(f"sessions: from-key {name}@{project!r} still present")
        if conn.execute("SELECT 1 FROM read_cursors WHERE project=? AND member_name=?", (project, name)).fetchone():
            problems.append(f"read_cursors: from-key {name}@{project!r} still present")
        cnt = conn.execute(
            "SELECT COUNT(*) FROM group_members WHERE (group_project=? AND group_name=?) OR (member_project=? AND member_name=?)",
            (project, name, project, name),
        ).fetchone()[0]
        if cnt:
            problems.append(f"group_members: {cnt} row(s) still reference from-key {name}@{project!r}")
        if conn.execute("SELECT 1 FROM groups WHERE project=? AND name=?", (project, name)).fetchone():
            problems.append(f"groups: from-key {name}@{project!r} still present")

    # 2. foreign key integrity
    fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if fk_violations:
        problems.append(f"foreign_key_check found {len(fk_violations)} violation(s): {fk_violations}")

    # 3. reachability: every message endpoint must land in the canonical set
    for id_, sp, s, rp, r in conn.execute(
        "SELECT id, sender_project, sender, recipient_project, recipient FROM messages"
    ):
        if (s, sp) not in canonical_keys:
            problems.append(f"message {id_}: sender endpoint {s}@{sp!r} not reachable via canonical set")
        if (r, rp) not in canonical_keys:
            problems.append(f"message {id_}: recipient endpoint {r}@{rp!r} not reachable via canonical set")

    conn.close()
    return problems


# ---------- report (read-only) ----------

def run_report(conn: sqlite3.Connection, remap: dict, db_path: str):
    print(f"source_db: {db_path}")
    print(
        f"schema: {remap['schema']}  canonical: {len(remap['canonical'])}  "
        f"mappings: {len(remap['mappings'])}  deletions: {len(remap['deletions'])}  "
        f"row_overrides: {len(remap['row_overrides'])}"
    )

    print("\nmappings (predicted impact):")
    for m in remap["mappings"]:
        fn, fp = m["from"]["name"], m["from"]["project"]
        tn, tp = m["to"]["name"], m["to"]["project"]
        msg_cnt = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE (sender_project=? AND sender=?) OR (recipient_project=? AND recipient=?)",
            (fp, fn, fp, fn),
        ).fetchone()[0]
        sess_from = conn.execute("SELECT COUNT(*) FROM sessions WHERE project=? AND name=?", (fp, fn)).fetchone()[0]
        sess_collision = bool(sess_from) and conn.execute(
            "SELECT 1 FROM sessions WHERE project=? AND name=?", (tp, tn)
        ).fetchone() is not None
        rc_from = conn.execute("SELECT COUNT(*) FROM read_cursors WHERE project=? AND member_name=?", (fp, fn)).fetchone()[0]
        rc_collision = bool(rc_from) and conn.execute(
            "SELECT 1 FROM read_cursors WHERE project=? AND member_name=?", (tp, tn)
        ).fetchone() is not None
        gm_cnt = conn.execute(
            "SELECT COUNT(*) FROM group_members WHERE (group_project=? AND group_name=?) OR (member_project=? AND member_name=?)",
            (fp, fn, fp, fn),
        ).fetchone()[0]
        print(
            f"  {fn}@{fp!r} -> {tn}@{tp!r}  messages={msg_cnt} "
            f"sessions={sess_from}{' (collision)' if sess_collision else ''} "
            f"read_cursors={rc_from}{' (collision)' if rc_collision else ''} "
            f"group_members={gm_cnt}"
        )

    print("\ndeletions (pre-check):")
    for d in remap["deletions"]:
        name, project = d["key"]["name"], d["key"]["project"]
        msg_cnt = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE (sender_project=? AND sender=?) OR (recipient_project=? AND recipient=?)",
            (project, name, project, name),
        ).fetchone()[0]
        status = "OK (0 messages)" if msg_cnt == 0 else f"FAIL ({msg_cnt} message endpoint(s) reference this key)"
        print(f"  {name}@{project!r}: {status}")

    print("\nvalidation: PASS (schema / mapping function / closure checks passed)")


# ---------- CLI ----------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--report", action="store_true", help="read-only impact report (default)")
    mode.add_argument("--apply", action="store_true", help="apply the remap, mutating the DB, then self-check")
    mode.add_argument("--dry-run", action="store_true", help="run the remap, then roll back")
    parser.add_argument("--db", default=None, help="path to rooms.db (default: MEETING_HOME/db/rooms.db)")
    parser.add_argument("--remap", default=None, help="path to identity-remap.json (default: docs/contracts/identity-remap.json)")
    args = parser.parse_args()

    db_path = args.db or default_db_path()
    if not os.path.exists(db_path):
        print(f"error: DB not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    remap_path = args.remap or default_remap_path()
    if not os.path.exists(remap_path):
        print(f"error: remap file not found: {remap_path}", file=sys.stderr)
        sys.exit(1)
    remap = load_remap(remap_path)

    errors = validate_remap(remap)
    if errors:
        print("VALIDATION FAILED:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)

    if args.apply or args.dry_run:
        apply_or_dry_run(db_path, remap, dry_run=args.dry_run)
        if args.apply:
            problems = self_check(db_path, remap)
            if problems:
                print("\nSELF-CHECK FAILED (changes already committed):", file=sys.stderr)
                for p in problems:
                    print(f"  - {p}", file=sys.stderr)
                sys.exit(1)
            print("\nself-check: PASS")
    else:
        conn = sqlite3.connect(db_path, timeout=30)
        try:
            run_report(conn, remap, db_path)
        finally:
            conn.close()


if __name__ == "__main__":
    main()
