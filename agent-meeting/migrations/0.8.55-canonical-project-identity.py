#!/usr/bin/env python3
"""
One-time migration: fold split "project" identity values into a canonical
form across the central meeting DB (~/.agent-meeting/db/rooms.db, or
$MEETING_HOME/db/rooms.db).

WHY: "project" used to be derived from a local folder/worktree name, so the
same logical project ended up split across multiple values (e.g.
WoodorAudit / wda-v3 / WDAudit are the same project on different
machines/worktrees/versions). This script folds old values into canonical
ones per an EXPLICIT, Tommy-authored mapping. It never guesses the mapping.

Columns folded (all in one transaction for --apply/--dry-run):
  messages.sender_project / recipient_project
  sessions.project              (PK: project, name)
  read_cursors.project          (PK: project, member_name)
  groups.project                (PK: project, name)
  group_members.group_project   (FK -> groups.project, name)
  group_members.member_project  (part of PK)

Modes
  --report [--out FILE]     read-only evidence dump + JSON mapping skeleton
                             {"<old>": "<old>"} for Tommy to hand-edit.
                             Default mode if no mode flag given.
  --apply --map map.json    fold every key in the mapping; keys not in the
                             mapping are left untouched. Backs up the DB to
                             <db>.pre-0.8.55.bak first (skipped if present).
  --dry-run --map map.json  same fold, rolled back at the end. No mutation.

Examples
  python3 0.8.55-canonical-project-identity.py --report --out /tmp/map.json
  python3 0.8.55-canonical-project-identity.py --dry-run --map map.json
  python3 0.8.55-canonical-project-identity.py --apply --map map.json

Mapping file: {"<old_project>": "<canonical_project>"}. Any project value
not present as a key is preserved exactly as-is (never guessed).
"""

import argparse
import json
import os
import shutil
import sqlite3
import sys

def default_db_path() -> str:
    home = os.environ.get("MEETING_HOME") or os.path.expanduser("~/.agent-meeting")
    return os.path.join(home, "db", "rooms.db")

# ---------- report (read-only) ----------

def collect_distinct_projects(conn: sqlite3.Connection) -> list[str]:
    values: set[str] = set()
    for q in (
        "SELECT DISTINCT sender_project FROM messages",
        "SELECT DISTINCT recipient_project FROM messages",
        "SELECT DISTINCT project FROM sessions",
        "SELECT DISTINCT project FROM read_cursors",
        "SELECT DISTINCT project FROM groups",
        "SELECT DISTINCT group_project FROM group_members",
        "SELECT DISTINCT member_project FROM group_members",
    ):
        values.update(v for (v,) in conn.execute(q) if v is not None)
    return sorted(values)

def run_report(conn: sqlite3.Connection, out_path: str | None):
    projects = collect_distinct_projects(conn)
    header = f"{'PROJECT':<30} {'SESSIONS':>8} {'CWD/HOST PAIRS':>14} {'MESSAGES':>9}  HAS_SESSIONS"
    print(header)
    print("-" * len(header))

    skeleton, evidence = {}, []
    for proj in projects:
        session_rows = conn.execute(
            "SELECT cwd, host FROM sessions WHERE project = ?", (proj,)
        ).fetchall()
        pairs: dict[tuple, int] = {}
        for cwd, host in session_rows:
            pairs[(cwd, host)] = pairs.get((cwd, host), 0) + 1
        msg_count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE sender_project = ? OR recipient_project = ?",
            (proj, proj),
        ).fetchone()[0]
        has_sessions = "yes" if session_rows else "no"
        print(f"{proj:<30} {len(session_rows):>8} {len(pairs):>14} {msg_count:>9}  {has_sessions}")
        skeleton[proj] = proj
        if pairs:
            lines = [f"  [{proj}] distinct (cwd, host) pairs:"]
            lines += [f"    - cwd={c!r} host={h!r} n={n}" for (c, h), n in
                      sorted(pairs.items(), key=lambda kv: -kv[1])]
            evidence.append("\n".join(lines))

    if evidence:
        print("\n" + "\n".join(evidence))

    payload = json.dumps(skeleton, indent=2, ensure_ascii=False, sort_keys=True)
    if out_path:
        with open(out_path, "w") as f:
            f.write(payload + "\n")
        print(f"\nmapping skeleton written to {out_path}")
    else:
        print("\nmapping skeleton (edit RHS to canonical, then pass via --map):")
        print(payload)

# ---------- fold (apply / dry-run) ----------

def fold_messages(conn, mapping) -> int:
    changed = 0
    for old, canon in mapping.items():
        if old == canon:
            continue
        changed += conn.execute(
            "UPDATE messages SET sender_project = ? WHERE sender_project = ?", (canon, old)
        ).rowcount
        changed += conn.execute(
            "UPDATE messages SET recipient_project = ? WHERE recipient_project = ?", (canon, old)
        ).rowcount
    return changed

def fold_sessions(conn, mapping) -> int:
    """PK (project, name). On collision, keep the row with the greater
    last_seen (the live/most-recent one) and delete the other."""
    changed = 0
    for old, canon in mapping.items():
        if old == canon:
            continue
        for name, old_last_seen in conn.execute(
            "SELECT name, last_seen FROM sessions WHERE project = ?", (old,)
        ).fetchall():
            existing = conn.execute(
                "SELECT last_seen FROM sessions WHERE project = ? AND name = ?", (canon, name)
            ).fetchone()
            if existing is None:
                conn.execute(
                    "UPDATE sessions SET project = ? WHERE project = ? AND name = ?", (canon, old, name)
                )
            else:
                old_ts = old_last_seen if old_last_seen is not None else -1
                canon_ts = existing[0] if existing[0] is not None else -1
                if old_ts >= canon_ts:
                    conn.execute("DELETE FROM sessions WHERE project = ? AND name = ?", (canon, name))
                    conn.execute(
                        "UPDATE sessions SET project = ? WHERE project = ? AND name = ?", (canon, old, name)
                    )
                else:
                    conn.execute("DELETE FROM sessions WHERE project = ? AND name = ?", (old, name))
            changed += 1
    return changed

def fold_read_cursors(conn, mapping) -> int:
    """PK (project, member_name). On collision, merge by MAX(cursor) so a
    fold never moves a cursor backwards (avoids replaying old backlog)."""
    changed = 0
    for old, canon in mapping.items():
        if old == canon:
            continue
        for member, old_cursor, old_updated in conn.execute(
            "SELECT member_name, cursor, updated_at FROM read_cursors WHERE project = ?", (old,)
        ).fetchall():
            existing = conn.execute(
                "SELECT cursor, updated_at FROM read_cursors WHERE project = ? AND member_name = ?",
                (canon, member),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "UPDATE read_cursors SET project = ? WHERE project = ? AND member_name = ?",
                    (canon, old, member),
                )
            else:
                conn.execute(
                    "UPDATE read_cursors SET cursor = ?, updated_at = ? WHERE project = ? AND member_name = ?",
                    (max(old_cursor, existing[0]), max(old_updated, existing[1]), canon, member),
                )
                conn.execute(
                    "DELETE FROM read_cursors WHERE project = ? AND member_name = ?", (old, member)
                )
            changed += 1
    return changed

def fold_groups(conn, mapping, warnings: list[str]) -> int:
    """Fold groups.project and group_members.group_project together so the
    FK (group_project, group_name) -> groups(project, name) stays consistent.
    On a groups PK collision, skip that group (genuine ambiguity) and warn
    instead of auto-merging."""
    changed = 0
    for old, canon in mapping.items():
        if old == canon:
            continue
        for (name,) in conn.execute("SELECT name FROM groups WHERE project = ?", (old,)).fetchall():
            if conn.execute(
                "SELECT 1 FROM groups WHERE project = ? AND name = ?", (canon, name)
            ).fetchone():
                warnings.append(
                    f"groups: skipped fold '{old}'->'{canon}' for group '{name}' "
                    f"(both projects already have a group named '{name}'; needs manual merge)"
                )
                continue
            conn.execute("UPDATE groups SET project = ? WHERE project = ? AND name = ?", (canon, old, name))
            conn.execute(
                "UPDATE group_members SET group_project = ? WHERE group_project = ? AND group_name = ?",
                (canon, old, name),
            )
            changed += 1
    return changed

def fold_group_members_project(conn, mapping) -> int:
    """PK includes member_project. On collision (same group + canonical
    member already present), dedupe by dropping the old-side row."""
    changed = 0
    for old, canon in mapping.items():
        if old == canon:
            continue
        rows = conn.execute(
            "SELECT group_project, group_name, member_name FROM group_members WHERE member_project = ?",
            (old,),
        ).fetchall()
        for gproj, gname, member in rows:
            collision = conn.execute(
                "SELECT 1 FROM group_members WHERE group_project=? AND group_name=? "
                "AND member_project=? AND member_name=?",
                (gproj, gname, canon, member),
            ).fetchone()
            if collision:
                conn.execute(
                    "DELETE FROM group_members WHERE group_project=? AND group_name=? "
                    "AND member_project=? AND member_name=?",
                    (gproj, gname, old, member),
                )
            else:
                conn.execute(
                    "UPDATE group_members SET member_project=? WHERE group_project=? AND group_name=? "
                    "AND member_project=? AND member_name=?",
                    (canon, gproj, gname, old, member),
                )
            changed += 1
    return changed

def run_fold(conn, mapping) -> tuple[dict, list]:
    warnings: list[str] = []
    summary = {
        "messages": fold_messages(conn, mapping),
        "sessions": fold_sessions(conn, mapping),
        "read_cursors": fold_read_cursors(conn, mapping),
        "groups": fold_groups(conn, mapping, warnings),
        "group_members": fold_group_members_project(conn, mapping),
    }
    return summary, warnings

def apply_or_dry_run(db_path: str, mapping: dict, dry_run: bool):
    if not dry_run:
        bak_path = db_path + ".pre-0.8.55.bak"
        if os.path.exists(bak_path):
            print(f"backup already exists, skipping: {bak_path}")
        else:
            shutil.copy2(db_path, bak_path)
            print(f"backup written: {bak_path}")

    conn = sqlite3.connect(db_path, isolation_level=None, timeout=30)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("BEGIN IMMEDIATE")
    # Folding groups.project reprojects the groups row before its
    # group_members children are reprojected in the same statement; with FK
    # enforcement immediate, that parent UPDATE is rejected as an orphan.
    # Defer FK checks to COMMIT time, by which point both are consistent;
    # this auto-resets at COMMIT/ROLLBACK so it only scopes this transaction.
    conn.execute("PRAGMA defer_foreign_keys = ON")
    try:
        summary, warnings = run_fold(conn, mapping)
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

    print("\nper-table rows changed:")
    for table, count in summary.items():
        print(f"  {table:<15} {count}")
    if warnings:
        print("\nwarnings:")
        for w in warnings:
            print(f"  - {w}")

# ---------- CLI ----------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--report", action="store_true", help="read-only evidence report (default)")
    mode.add_argument("--apply", action="store_true", help="apply the fold, mutating the DB")
    mode.add_argument("--dry-run", action="store_true", help="run the fold, then roll back")
    parser.add_argument("--db", default=None, help="path to rooms.db (default: MEETING_HOME/db/rooms.db)")
    parser.add_argument("--map", default=None, help="path to mapping JSON ({old: canonical})")
    parser.add_argument("--out", default=None, help="--report only: write JSON mapping skeleton here")
    args = parser.parse_args()

    db_path = args.db or default_db_path()
    if not os.path.exists(db_path):
        print(f"error: DB not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    if args.apply or args.dry_run:
        if not args.map:
            print("error: --map <mapping.json> is required with --apply/--dry-run", file=sys.stderr)
            sys.exit(1)
        with open(args.map) as f:
            mapping = json.load(f)
        apply_or_dry_run(db_path, mapping, dry_run=args.dry_run)
    else:
        conn = sqlite3.connect(db_path, timeout=30)
        try:
            run_report(conn, args.out)
        finally:
            conn.close()

if __name__ == "__main__":
    main()
