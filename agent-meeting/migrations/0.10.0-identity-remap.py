#!/usr/bin/env python3
"""
Produce the identity remap table (docs/contracts/identity-remap-schema.md
v1) for stage 0.2 of docs/contracts/0.10.0-composite-key-identity.md.

WHY: session identity is authoritatively the composite key (name, project).
Historically "project" was derived from cwd folder name, so the same real
identity split into multiple composite keys across machines/worktrees/typos
(drift). This script does NOT touch the database. It reads it read-only,
applies a hardcoded, Tommy-authored judgment table (auto-inferred drift +
manually confirmed identities), validates the hard constraints from the
schema doc, and emits a JSON remap file consumed by (a) this repo's next
migration script (rewrites messages/sessions/read_cursors/group_members)
and (b) AMBridge's local bucket migration on the phone.

Scope note: this stage only resolves identities that carry messages or have
live sessions rows. Pure orphan read_cursors residue with no sessions row
and no messages (vfy-*, askdemo, tc21-probe, zz-restart-test, etc.) is left
untouched here; it is scope for stage 4's prune pass, not stage 0.2.

Modes
  --report [--db PATH]        human-readable summary + validation, no file
                               written (default mode if no mode flag given).
  (no --report) [--out FILE]  validate, then write the remap JSON. Default
                               --out: docs/contracts/identity-remap.json

Any validation failure aborts with a non-zero exit and writes nothing.
"""

import argparse
import json
import os
import sqlite3
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCHEMA_VERSION = "agent-meeting/identity-remap@1"
GENERATOR_PATH = "agent-meeting/migrations/0.10.0-identity-remap.py"

def default_db_path() -> str:
    home = os.environ.get("MEETING_HOME") or os.path.expanduser("~/.agent-meeting")
    return os.path.join(home, "db", "rooms.db")

def default_out_path() -> str:
    return os.path.join(REPO_ROOT, "docs", "contracts", "identity-remap.json")

# ---------- hardcoded judgment table (Tommy-authored; not auto-inferred) ----------
# Each row: one old (name, project) folding into (name, canonical_project).

JUDGMENT_TABLE = [
    {"name": "Tommy", "canonical_project": "*", "from_project": "AMBridge",
     "basis": "manual", "rule": None,
     "note": "Tommy 是人类用户经 AMBridge 中继，权威身份是全局 *，AMBridge 是中继方的项目名被误写为发件人项目"},
    {"name": "Tommy", "canonical_project": "*", "from_project": "Resources",
     "basis": "auto", "rule": "path-derived",
     "note": "来自 AMBridge.app/Contents/Resources 的目录名推导"},
    {"name": "Tommy", "canonical_project": "*", "from_project": "",
     "basis": "auto", "rule": "empty-value", "note": None},

    {"name": "atlas", "canonical_project": "Atlas", "from_project": "atlas-p3a",
     "basis": "auto", "rule": "doc-alias", "note": None},
    {"name": "atlas", "canonical_project": "Atlas", "from_project": "atlas-selfdesc",
     "basis": "auto", "rule": "doc-alias", "note": None},
    {"name": "atlas", "canonical_project": "Atlas", "from_project": "OMI",
     "basis": "auto", "rule": "path-derived",
     "note": "上级目录名 ~/AIAgent/OMI/Atlas"},

    {"name": "atlas-relay", "canonical_project": "Atlas", "from_project": "relay-p3a-s5-doc",
     "basis": "auto", "rule": "doc-alias", "note": None},
    {"name": "atlas-relay", "canonical_project": "Atlas", "from_project": "relay-p3a-doc-fix",
     "basis": "auto", "rule": "doc-alias", "note": None},
    {"name": "atlas-relay", "canonical_project": "Atlas", "from_project": "relay-arch-doc",
     "basis": "auto", "rule": "doc-alias", "note": None},
    {"name": "atlas-relay", "canonical_project": "Atlas",
     "from_project": "~/.claude/projects/-Users-tommyclaw-AIAgent-OMI-Atlas/memory",
     "basis": "auto", "rule": "path-derived", "note": None},

    {"name": "plugins", "canonical_project": "plugins", "from_project": "probeproj",
     "basis": "manual", "rule": None,
     "note": "探测残留身份，但收过 4 条真实消息，人工确认归入 plugins"},

    {"name": "plugins-win-codex", "canonical_project": "plugins", "from_project": "codex-plugins-dist",
     "basis": "manual", "rule": None,
     "note": "codex-plugins-dist 是 plugins 的分发仓目录名；该会话是 plugins 项目的 Windows codex 端，权威声明已为 plugins"},
    {"name": "plugins-win-codex", "canonical_project": "plugins", "from_project": "admin",
     "basis": "auto", "rule": "path-derived",
     "note": "Windows 用户名目录 C:\\Users\\admin 派生"},

    {"name": "wda-gm", "canonical_project": "wdaudit", "from_project": "provisional",
     "basis": "auto", "rule": "probe-residue", "note": None},
    {"name": "wda-gm", "canonical_project": "wdaudit", "from_project": "",
     "basis": "auto", "rule": "empty-value", "note": None},

    {"name": "wdav3-laptop", "canonical_project": "wdaudit", "from_project": "WoodorAudit",
     "basis": "manual", "rule": None,
     "note": "项目更名前的旧名，非路径派生，人工确认为同一项目"},
    {"name": "wdav3-laptop", "canonical_project": "wdaudit", "from_project": "~/AIAgent/wda-v3",
     "basis": "auto", "rule": "path-derived", "note": None},
    {"name": "wdav3-laptop", "canonical_project": "wdaudit", "from_project": "wda-v3",
     "basis": "auto", "rule": "path-derived", "note": "仓库目录末级名"},
    {"name": "wdav3-laptop", "canonical_project": "wdaudit",
     "from_project": "/private/tmp/claude-501/-Users-tommyclaw-AIAgent-wda-v3-agents-wdav3-laptop/f415cbd4-eec7-4036-9dc7-ae4b8d15d500/scratchpad",
     "basis": "auto", "rule": "path-derived", "note": None},

    {"name": "cx-test", "canonical_project": "meeting-test", "from_project": "admin",
     "basis": "manual", "rule": None,
     "note": "agent-meeting 插件的测试会话，原有 project 值分别源自 Windows 用户名目录、来路不明的缩写、以及被借用的真实项目名，均非合格权威身份；人工判定归入专用测试项目 meeting-test，便于方案阶段 4 按项目一次性清理，同时避免测试消息混入真实项目"},
    {"name": "cx-test", "canonical_project": "meeting-test", "from_project": "plugins",
     "basis": "manual", "rule": None,
     "note": "agent-meeting 插件的测试会话，原有 project 值分别源自 Windows 用户名目录、来路不明的缩写、以及被借用的真实项目名，均非合格权威身份；人工判定归入专用测试项目 meeting-test，便于方案阶段 4 按项目一次性清理，同时避免测试消息混入真实项目"},
    {"name": "cx-test", "canonical_project": "meeting-test", "from_project": "ft",
     "basis": "manual", "rule": None,
     "note": "agent-meeting 插件的测试会话，原有 project 值分别源自 Windows 用户名目录、来路不明的缩写、以及被借用的真实项目名，均非合格权威身份；人工判定归入专用测试项目 meeting-test，便于方案阶段 4 按项目一次性清理，同时避免测试消息混入真实项目"},

    {"name": "cx-slimtest", "canonical_project": "meeting-test", "from_project": "admin",
     "basis": "manual", "rule": None,
     "note": "agent-meeting 插件的测试会话，原有 project 值分别源自 Windows 用户名目录、来路不明的缩写、以及被借用的真实项目名，均非合格权威身份；人工判定归入专用测试项目 meeting-test，便于方案阶段 4 按项目一次性清理，同时避免测试消息混入真实项目"},

    {"name": "codex-omi", "canonical_project": "*", "from_project": "AIAgent",
     "basis": "manual", "rule": None,
     "note": "AIAgent 是 ~/AIAgent 根目录名派生；该会话在 AIAgent 根目录工作，不隶属任何单一项目，人工判定归入全局身份 *（库中已有多个普通会话使用 *，非 relay 专用）"},
    {"name": "probe-ctl", "canonical_project": "meeting-test", "from_project": "AIAgent",
     "basis": "manual", "rule": None,
     "note": "同为目录名派生；探测控制身份，属测试残留，归入专用测试项目 meeting-test"},
    {"name": "AMBridge", "canonical_project": "AMBridge", "from_project": "standard",
     "basis": "manual", "rule": None,
     "note": "standard 是 ~/AIAgent/standard 目录名派生；该会话是 AMBridge 项目的历史会话，人工判定归入 AMBridge"},
]

DELETIONS_NOTE = (
    "无消息承载，会话早已离线，project 值全为路径派生且无法追认真实项目，"
    "直接删除而非归并（归并会把三个可能不同的真实项目错误合成一个）"
)

DELETIONS = [
    {"name": "codex-Tommys-Laptop", "project": "~", "note": DELETIONS_NOTE},
    {"name": "codex-Tommys-Laptop", "project": "~/AIAgent/idea-bridge", "note": DELETIONS_NOTE},
    {"name": "codex-Tommys-Laptop",
     "project": "~/Documents/Codex/2026-07-15/users-tommy-downloads-yyht-2024-0757",
     "note": DELETIONS_NOTE},
]

# ---------- DB read-only counting ----------

def count_affected(conn: sqlite3.Connection, name: str, project: str) -> dict:
    """Endpoint-level row counts across the four tables the next migration
    step will rewrite. messages counts sender-side and recipient-side
    endpoint matches separately and sums them (a message can be touched on
    one or both sides)."""
    sender = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE sender_project = ? AND sender = ?", (project, name)
    ).fetchone()[0]
    recipient = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE recipient_project = ? AND recipient = ?", (project, name)
    ).fetchone()[0]
    sessions = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE project = ? AND name = ?", (project, name)
    ).fetchone()[0]
    read_cursors = conn.execute(
        "SELECT COUNT(*) FROM read_cursors WHERE project = ? AND member_name = ?", (project, name)
    ).fetchone()[0]
    group_members = conn.execute(
        "SELECT COUNT(*) FROM group_members WHERE member_project = ? AND member_name = ?", (project, name)
    ).fetchone()[0]
    return {
        "messages": sender + recipient,
        "sessions": sessions,
        "read_cursors": read_cursors,
        "group_members": group_members,
    }

def scan_sessions_keys(conn: sqlite3.Connection) -> set:
    return {(name, project) for project, name in conn.execute("SELECT project, name FROM sessions")}

def scan_message_endpoint_keys(conn: sqlite3.Connection) -> set:
    """Identity keys that appear as either endpoint of any message. A
    session having ended does not retire its identity -- the identity keeps
    existing as long as messages reference it, so this (not sessions alone)
    is the other half of "every identity key currently in the DB"."""
    keys = set()
    for sp, s in conn.execute("SELECT DISTINCT sender_project, sender FROM messages"):
        keys.add((s, sp))
    for rp, r in conn.execute("SELECT DISTINCT recipient_project, recipient FROM messages"):
        keys.add((r, rp))
    return keys

# ---------- build + validate ----------

class ValidationError(Exception):
    pass

def build_and_validate(conn: sqlite3.Connection) -> dict:
    errors = []

    from_keys = [(row["name"], row["from_project"]) for row in JUDGMENT_TABLE]
    from_set = set(from_keys)
    if len(from_keys) != len(from_set):
        seen = set()
        for k in from_keys:
            if k in seen:
                errors.append(f"duplicate mapping 'from' key: {k!r}")
            seen.add(k)

    canonical_explicit = {(row["name"], row["canonical_project"]) for row in JUDGMENT_TABLE}
    deletion_keys = {(d["name"], d["project"]) for d in DELETIONS}

    overlap = from_set & canonical_explicit
    if overlap:
        errors.append(f"identity mapping(s) with from == canonical (must be omitted): {sorted(overlap)}")
    overlap = from_set & deletion_keys
    if overlap:
        errors.append(f"key(s) present in both mappings and deletions: {sorted(overlap)}")
    for row in JUDGMENT_TABLE:
        to_key = (row["name"], row["canonical_project"])
        if to_key not in canonical_explicit:
            errors.append(f"mapping to {to_key!r} is not in the canonical set (unreachable)")
        if row["basis"] == "manual" and not row["note"]:
            errors.append(f"manual mapping {row['name']}@{row['from_project']!r} missing required note")
        if row["basis"] == "auto" and not row["rule"]:
            errors.append(f"auto mapping {row['name']}@{row['from_project']!r} missing required rule")

    # canonical = every identity key that is still around after folding this
    # table's mappings and deletions away, PLUS the judgment table's declared
    # targets (some, e.g. meeting-test, are human-invented values with no
    # rows of their own yet). "Still around" scans sessions (live) UNION
    # message endpoints (identities keep existing as long as messages
    # reference them, session or no session) -- an identity with only
    # read_cursors/group_members rows and no sessions row and no message is
    # a pure orphan and is deliberately NOT scanned here: those are stage-4
    # prune scope (test residue like vfy-*/askdemo/tc21-probe), and folding
    # them into canonical would retroactively bless drift values as
    # authoritative.
    all_identity_keys = scan_sessions_keys(conn) | scan_message_endpoint_keys(conn)
    canonical_extra = all_identity_keys - from_set - deletion_keys
    canonical_set = canonical_explicit | canonical_extra

    mappings = []
    for row in JUDGMENT_TABLE:
        affected = count_affected(conn, row["name"], row["from_project"])
        mappings.append({
            "from": {"name": row["name"], "project": row["from_project"]},
            "to": {"name": row["name"], "project": row["canonical_project"]},
            "basis": row["basis"],
            "rule": row["rule"],
            "note": row["note"],
            "affected": affected,
        })

    deletions = []
    for d in DELETIONS:
        affected = count_affected(conn, d["name"], d["project"])
        if affected["messages"] != 0:
            errors.append(
                f"deletion key {d['name']}@{d['project']!r} carries {affected['messages']} "
                f"message endpoint(s); must go through mappings, not deletions"
            )
        deletions.append({
            "key": {"name": d["name"], "project": d["project"]},
            "note": d["note"],
            "affected": affected,
        })

    if errors:
        raise ValidationError("\n".join(f"  - {e}" for e in errors))

    canonical = [{"name": n, "project": p} for n, p in sorted(canonical_set)]
    mappings.sort(key=lambda m: (m["from"]["name"], m["from"]["project"]))
    deletions.sort(key=lambda d: (d["key"]["name"], d["key"]["project"]))

    return {
        "schema": SCHEMA_VERSION,
        "generated_at": time.strftime("%Y-%m-%d %H:%M %Z"),
        "generator": GENERATOR_PATH,
        "source_db": None,  # filled in by caller with the actual --db path used
        "canonical": canonical,
        "mappings": mappings,
        "deletions": deletions,
        "row_overrides": [],
    }

# ---------- report (human-readable, read-only) ----------

def run_report(conn: sqlite3.Connection, db_path: str):
    try:
        remap = build_and_validate(conn)
    except ValidationError as e:
        print("VALIDATION FAILED:", file=sys.stderr)
        print(str(e), file=sys.stderr)
        sys.exit(1)

    print(f"source_db: {db_path}")
    print(f"canonical keys: {len(remap['canonical'])}")
    for c in remap["canonical"]:
        print(f"  {c['name']}@{c['project']!r}")

    print(f"\nmappings: {len(remap['mappings'])}")
    for m in remap["mappings"]:
        a = m["affected"]
        print(
            f"  {m['from']['name']}@{m['from']['project']!r} -> "
            f"{m['to']['name']}@{m['to']['project']!r}  "
            f"[{m['basis']}{'/' + m['rule'] if m['rule'] else ''}]  "
            f"affected: messages={a['messages']} sessions={a['sessions']} "
            f"read_cursors={a['read_cursors']} group_members={a['group_members']}"
        )
        if m["note"]:
            print(f"      note: {m['note']}")

    print(f"\ndeletions: {len(remap['deletions'])}")
    for d in remap["deletions"]:
        a = d["affected"]
        print(
            f"  {d['key']['name']}@{d['key']['project']!r}  "
            f"affected: messages={a['messages']} sessions={a['sessions']} "
            f"read_cursors={a['read_cursors']} group_members={a['group_members']}"
        )

    print("\nvalidation: PASS")

# ---------- CLI ----------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--report", action="store_true", help="read-only human-readable summary, no file written")
    parser.add_argument("--db", default=None, help="path to rooms.db (default: MEETING_HOME/db/rooms.db)")
    parser.add_argument("--out", default=None, help="output path for the remap JSON (non-report mode only)")
    args = parser.parse_args()

    db_path = args.db or default_db_path()
    if not os.path.exists(db_path):
        print(f"error: DB not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(db_path, timeout=30)
    try:
        if args.report:
            run_report(conn, db_path)
            return

        try:
            remap = build_and_validate(conn)
        except ValidationError as e:
            print("VALIDATION FAILED, no file written:", file=sys.stderr)
            print(str(e), file=sys.stderr)
            sys.exit(1)
    finally:
        conn.close()

    remap["source_db"] = db_path
    out_path = args.out or default_out_path()
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(remap, f, indent=2, ensure_ascii=False, sort_keys=False)
        f.write("\n")
    print(f"remap written: {out_path}")
    print(f"  canonical: {len(remap['canonical'])}  mappings: {len(remap['mappings'])}  deletions: {len(remap['deletions'])}")

if __name__ == "__main__":
    main()
