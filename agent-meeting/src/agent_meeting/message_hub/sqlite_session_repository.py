"""SQLite-backed session identity lifecycle for the central message hub."""

from __future__ import annotations

import time
from typing import Callable


class SQLiteSessionRepository:
    def __init__(
        self,
        *,
        connect: Callable,
        online_threshold: float,
    ):
        self._connect = connect
        self._online_threshold = online_threshold

    def resolve_candidates(self, raw_name: str) -> list[dict]:
        with self._connect() as connection:
            session_rows = connection.execute(
                "SELECT project FROM sessions WHERE name=?",
                (raw_name,),
            ).fetchall()
            group_rows = connection.execute(
                "SELECT project FROM groups WHERE name=?",
                (raw_name,),
            ).fetchall()
            candidates = [
                {
                    "project": row["project"],
                    "name": raw_name,
                    "kind": "session",
                }
                for row in session_rows
            ]
            candidates.extend(
                {
                    "project": row["project"],
                    "name": raw_name,
                    "kind": "group",
                }
                for row in group_rows
            )
            global_candidates = [
                candidate
                for candidate in candidates
                if candidate["project"] == "*"
            ]
            if global_candidates:
                return [global_candidates[0]]
            if not candidates:
                history_rows = connection.execute(
                    "SELECT DISTINCT sender_project AS project"
                    " FROM messages WHERE sender=?"
                    " UNION"
                    " SELECT DISTINCT recipient_project AS project"
                    " FROM messages WHERE recipient=?",
                    (raw_name, raw_name),
                ).fetchall()
                candidates = [
                    {
                        "project": row["project"],
                        "name": raw_name,
                        "kind": "historical",
                    }
                    for row in history_rows
                ]
        return candidates

    def list_sessions(self) -> list[dict]:
        now = time.time()
        with self._connect() as connection:
            message_counts = {
                (row["sender_project"], row["sender"]): row["cnt"]
                for row in connection.execute(
                    "SELECT sender_project, sender, COUNT(*) AS cnt"
                    " FROM messages GROUP BY sender_project, sender"
                )
            }
            sessions = connection.execute(
                "SELECT project, name, last_seen, cwd, role, host, os"
                " FROM sessions"
            ).fetchall()

        online_keys = set()
        empty_keys = set()
        metadata = {}
        for session in sessions:
            key = (session["project"], session["name"])
            metadata[key] = {
                "cwd": session["cwd"],
                "role": session["role"],
                "host": session["host"],
                "os": session["os"],
            }
            if now - (session["last_seen"] or 0) <= self._online_threshold:
                online_keys.add(key)
            else:
                empty_keys.add(key)

        historical_keys = (
            set(message_counts) - online_keys - empty_keys
        )
        output = []
        for status, keys in (
            ("online", online_keys),
            ("empty", empty_keys),
        ):
            for key in sorted(
                keys,
                key=lambda item: -message_counts.get(item, 0),
            ):
                item = metadata[key]
                output.append(
                    {
                        "status": status,
                        "project": key[0],
                        "name": key[1],
                        "msgs": message_counts.get(key, 0),
                        "role": item["role"] or "worker",
                        "cwd": item["cwd"],
                        "host": item["host"],
                        "os": item["os"],
                    }
                )
        for key in sorted(
            historical_keys,
            key=lambda item: -message_counts.get(item, 0),
        ):
            output.append(
                {
                    "status": "historical",
                    "project": key[0],
                    "name": key[1],
                    "msgs": message_counts.get(key, 0),
                    "role": "worker",
                    "cwd": None,
                    "host": None,
                    "os": None,
                }
            )
        return output

    def register(
        self,
        project: str,
        name: str,
        cwd,
        force: bool,
        *,
        role=None,
        host=None,
        os_label=None,
        instance=None,
        client_version=None,
        legacy_cursor=None,
    ) -> dict:
        instance = instance or None
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT last_seen, instance, host FROM sessions"
                    " WHERE project=? AND name=?",
                    (project, name),
                ).fetchone()
                if existing and not force:
                    fresh = (
                        now - (existing["last_seen"] or 0)
                        <= self._online_threshold
                    )
                    existing_instance = existing["instance"] or None
                    same_instance = (
                        instance is not None
                        and existing_instance is not None
                        and instance == existing_instance
                    )
                    if fresh and not same_instance:
                        connection.rollback()
                        return {
                            "error": (
                                f"'{name}@{project}' is already registered "
                                "by a live monitor on "
                                f"{existing['host'] or 'unknown host'} "
                                "(instance "
                                f"{existing_instance or 'unknown'}). "
                                "Refusing to take over silently. Stop it "
                                "first (`am stop <name>`), or pass "
                                "--force to override."
                            ),
                            "code": "name_taken",
                        }
                cursor_row = connection.execute(
                    "SELECT cursor FROM read_cursors"
                    " WHERE project=? AND member_name=?",
                    (project, name),
                ).fetchone()
                if cursor_row is None:
                    cursor = int(
                        connection.execute(
                            "SELECT COALESCE(MAX(id), 0) AS m"
                            " FROM messages"
                        ).fetchone()["m"]
                    )
                    connection.execute(
                        "INSERT INTO read_cursors"
                        " (project, member_name, cursor, updated_at)"
                        " VALUES (?, ?, ?, ?)",
                        (project, name, cursor, int(now)),
                    )
                else:
                    cursor = int(cursor_row["cursor"])

                if legacy_cursor is not None:
                    try:
                        legacy_cursor = max(0, int(legacy_cursor))
                    except (TypeError, ValueError):
                        connection.rollback()
                        return {
                            "error": (
                                "legacy_cursor must be a non-negative integer"
                            ),
                            "code": "invalid_legacy_cursor",
                        }
                    migration_id = "codex-broker-state-v1"
                    migrated = connection.execute(
                        "SELECT 1 FROM cursor_migrations"
                        " WHERE project=? AND member_name=?"
                        " AND migration_id=?",
                        (project, name, migration_id),
                    ).fetchone()
                    if migrated is None:
                        cursor = min(cursor, legacy_cursor)
                        connection.execute(
                            "UPDATE read_cursors SET cursor=?, updated_at=?"
                            " WHERE project=? AND member_name=?",
                            (cursor, int(now), project, name),
                        )
                        connection.execute(
                            "INSERT INTO cursor_migrations"
                            " (project, member_name, migration_id, applied_at)"
                            " VALUES (?, ?, ?, ?)",
                            (
                                project,
                                name,
                                migration_id,
                                int(now),
                            ),
                        )

                connection.execute(
                    "INSERT INTO sessions"
                    " (project, name, cwd, host, os, instance,"
                    " registered_at, last_seen, role, client_version)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?,"
                    " COALESCE(?, 'worker'), ?)"
                    " ON CONFLICT(project, name) DO UPDATE SET"
                    " cwd=excluded.cwd, host=excluded.host,"
                    " os=excluded.os, instance=excluded.instance,"
                    " registered_at=excluded.registered_at,"
                    " last_seen=excluded.last_seen,"
                    " role=COALESCE(?, role),"
                    " client_version=excluded.client_version",
                    (
                        project,
                        name,
                        cwd,
                        host,
                        os_label,
                        instance,
                        str(int(now)),
                        now,
                        role,
                        client_version,
                        role,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {
            "ok": True,
            "project": project,
            "name": name,
            "cursor": cursor,
        }

    def acknowledge(
        self,
        project: str,
        name: str,
        instance: str,
        expected_cursor: int,
        through: int,
    ) -> dict:
        now = int(time.time())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    "SELECT instance FROM sessions"
                    " WHERE project=? AND name=?",
                    (project, name),
                ).fetchone()
                current_instance = (
                    (session["instance"] or None)
                    if session is not None
                    else None
                )
                if not instance or current_instance != instance:
                    connection.rollback()
                    return {
                        "error": (
                            "registration instance is no longer current"
                        ),
                        "code": "stale_instance",
                    }
                cursor_row = connection.execute(
                    "SELECT cursor FROM read_cursors"
                    " WHERE project=? AND member_name=?",
                    (project, name),
                ).fetchone()
                if cursor_row is None:
                    connection.rollback()
                    return {
                        "error": "recipient cursor does not exist",
                        "code": "cursor_missing",
                    }
                current_cursor = int(cursor_row["cursor"])
                if current_cursor != expected_cursor:
                    connection.rollback()
                    return {
                        "error": (
                            f"cursor changed from {expected_cursor} "
                            f"to {current_cursor}"
                        ),
                        "code": "cursor_conflict",
                        "cursor": current_cursor,
                    }
                if through < expected_cursor:
                    connection.rollback()
                    return {
                        "error": "ack cursor cannot move backwards",
                        "code": "cursor_regression",
                    }
                if through > expected_cursor:
                    sequence_row = connection.execute(
                        "SELECT seq FROM sqlite_sequence"
                        " WHERE name='messages'"
                    ).fetchone()
                    max_allocated_id = (
                        int(sequence_row["seq"])
                        if sequence_row is not None
                        else 0
                    )
                    if through > max_allocated_id:
                        connection.rollback()
                        return {
                            "error": (
                                f"message {through} has not been allocated"
                            ),
                            "code": "invalid_ack_target",
                        }
                connection.execute(
                    "UPDATE read_cursors SET cursor=?, updated_at=?"
                    " WHERE project=? AND member_name=? AND cursor=?",
                    (
                        through,
                        now,
                        project,
                        name,
                        expected_cursor,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {"ok": True, "cursor": through}

    def prune(
        self,
        older_than_days: float,
        include_referenced: bool,
        apply: bool,
    ) -> dict:
        now = time.time()
        cutoff = min(
            now - older_than_days * 86400.0,
            now - self._online_threshold,
        )
        prune, skipped = [], []
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT project, name, cwd, host, os, role, last_seen"
                " FROM sessions WHERE COALESCE(last_seen, 0) < ?",
                (cutoff,),
            ).fetchall()
            for row in rows:
                message_count = connection.execute(
                    "SELECT COUNT(*) AS c FROM messages"
                    " WHERE (sender_project=? AND sender=?)"
                    " OR (recipient_project=? AND recipient=?)",
                    (
                        row["project"],
                        row["name"],
                        row["project"],
                        row["name"],
                    ),
                ).fetchone()["c"]
                item = {
                    "project": row["project"],
                    "name": row["name"],
                    "cwd": row["cwd"],
                    "host": row["host"],
                    "os": row["os"],
                    "role": row["role"] or "worker",
                    "last_seen": row["last_seen"] or 0,
                    "age_days": round(
                        (now - (row["last_seen"] or 0)) / 86400.0,
                        1,
                    ),
                    "msgs": message_count,
                }
                destination = (
                    skipped
                    if message_count and not include_referenced
                    else prune
                )
                destination.append(item)
            if apply:
                for item in prune:
                    connection.execute(
                        "DELETE FROM sessions"
                        " WHERE project=? AND name=?",
                        (item["project"], item["name"]),
                    )
        return {
            "applied": apply,
            "pruned": prune,
            "skipped_referenced": skipped,
        }

    def unregister(
        self,
        project: str,
        name: str,
        instance=None,
    ) -> dict:
        with self._connect() as connection:
            if instance:
                cursor = connection.execute(
                    "DELETE FROM sessions"
                    " WHERE project=? AND name=? AND instance=?",
                    (project, name, instance),
                )
            else:
                cursor = connection.execute(
                    "DELETE FROM sessions"
                    " WHERE project=? AND name=?",
                    (project, name),
                )
        return {
            "ok": True,
            "deleted": cursor.rowcount > 0,
            "project": project,
            "name": name,
            "instance": instance,
        }

    def rename(self, project: str, old: str, new: str) -> dict:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if not connection.execute(
                    "SELECT 1 FROM sessions"
                    " WHERE project=? AND name=?",
                    (project, old),
                ).fetchone():
                    connection.execute("ROLLBACK")
                    return {"error": f"no such session: {old}@{project}"}
                if connection.execute(
                    "SELECT 1 FROM sessions"
                    " WHERE project=? AND name=?",
                    (project, new),
                ).fetchone():
                    connection.execute("ROLLBACK")
                    return {
                        "error": f"name already taken: {new}@{project}"
                    }

                connection.execute(
                    "UPDATE sessions SET name=?"
                    " WHERE project=? AND name=?",
                    (new, project, old),
                )
                sent = connection.execute(
                    "UPDATE messages SET sender=?"
                    " WHERE sender_project=? AND sender=?",
                    (new, project, old),
                )
                received = connection.execute(
                    "UPDATE messages SET recipient=?"
                    " WHERE recipient_project=? AND recipient=?",
                    (new, project, old),
                )
                old_cursor = connection.execute(
                    "SELECT cursor, updated_at FROM read_cursors"
                    " WHERE project=? AND member_name=?",
                    (project, old),
                ).fetchone()
                if old_cursor is not None:
                    connection.execute(
                        "INSERT INTO read_cursors"
                        " (project, member_name, cursor, updated_at)"
                        " VALUES (?, ?, ?, ?)"
                        " ON CONFLICT(project, member_name) DO UPDATE SET"
                        " cursor=MAX(excluded.cursor,"
                        " read_cursors.cursor),"
                        " updated_at=MAX(excluded.updated_at,"
                        " read_cursors.updated_at)",
                        (
                            project,
                            new,
                            old_cursor["cursor"],
                            old_cursor["updated_at"],
                        ),
                    )
                    connection.execute(
                        "DELETE FROM read_cursors"
                        " WHERE project=? AND member_name=?",
                        (project, old),
                    )

                connection.execute("COMMIT")
                return {
                    "ok": True,
                    "project": project,
                    "old": old,
                    "new": new,
                    "messages_migrated": (
                        sent.rowcount + received.rowcount
                    ),
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise
