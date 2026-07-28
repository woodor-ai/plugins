"""SQLite-backed group lifecycle for the central message hub."""

from __future__ import annotations

import time
from typing import Callable


class SQLiteGroupRepository:
    def __init__(
        self,
        *,
        connect: Callable,
        format_identity: Callable[[str, str], str],
    ):
        self._connect = connect
        self._format_identity = format_identity

    def _missing(self, project: str, name: str) -> dict:
        return {
            "error": (
                f"group '{name}' does not exist in project '{project}'"
            )
        }

    def _exists(self, connection, project: str, name: str) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM groups WHERE project=? AND name=?",
                (project, name),
            ).fetchone()
            is not None
        )

    def create(self, project: str, name: str, members: list, creator):
        now = int(time.time())
        with self._connect() as connection:
            if connection.execute(
                "SELECT 1 FROM sessions WHERE project=? AND name=?",
                (project, name),
            ).fetchone():
                return {
                    "error": (
                        f"name '{name}' is already an active session name "
                        f"in project '{project}'"
                    )
                }
            if self._exists(connection, project, name):
                return {
                    "error": (
                        f"group '{name}' already exists in project "
                        f"'{project}'"
                    )
                }
            if connection.execute(
                "SELECT 1 FROM messages"
                " WHERE (sender_project=? AND sender=?)"
                " OR (recipient_project=? AND recipient=?) LIMIT 1",
                (project, name, project, name),
            ).fetchone():
                return {
                    "error": (
                        f"name '{name}' has existing message history "
                        "(cannot reuse as group name)"
                    )
                }

            resolved = []
            seen = set()
            for member in members:
                if "@" in member:
                    member_name, _, member_project = member.partition("@")
                else:
                    member_name, member_project = member, project
                key = (member_project, member_name)
                if key not in seen:
                    seen.add(key)
                    resolved.append(key)

            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO groups"
                    " (project, name, created_at, creator)"
                    " VALUES (?, ?, ?, ?)",
                    (project, name, now, creator),
                )
                max_id = connection.execute(
                    "SELECT COALESCE(MAX(id), 0) AS m FROM messages"
                ).fetchone()["m"]
                for member_project, member_name in resolved:
                    connection.execute(
                        "INSERT OR IGNORE INTO group_members"
                        " (group_project, group_name, member_project,"
                        " member_name, added_at, joined_after_message_id)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            project,
                            name,
                            member_project,
                            member_name,
                            now,
                            max_id,
                        ),
                    )
                    if not connection.execute(
                        "SELECT 1 FROM read_cursors"
                        " WHERE project=? AND member_name=?",
                        (member_project, member_name),
                    ).fetchone():
                        connection.execute(
                            "INSERT INTO read_cursors"
                            " (project, member_name, cursor, updated_at)"
                            " VALUES (?, ?, ?, ?)",
                            (
                                member_project,
                                member_name,
                                max_id,
                                now,
                            ),
                        )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return {
            "ok": True,
            "project": project,
            "name": name,
            "members": [
                self._format_identity(member_name, member_project)
                for member_project, member_name in resolved
            ],
        }

    def add(
        self,
        group_project: str,
        group: str,
        member_project: str,
        member: str,
    ):
        now = int(time.time())
        with self._connect() as connection:
            if not self._exists(connection, group_project, group):
                return self._missing(group_project, group)
            max_id = connection.execute(
                "SELECT COALESCE(MAX(id), 0) AS m FROM messages"
            ).fetchone()["m"]
            connection.execute(
                "INSERT OR IGNORE INTO group_members"
                " (group_project, group_name, member_project,"
                " member_name, added_at, joined_after_message_id)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    group_project,
                    group,
                    member_project,
                    member,
                    now,
                    max_id,
                ),
            )
            if not connection.execute(
                "SELECT 1 FROM read_cursors"
                " WHERE project=? AND member_name=?",
                (member_project, member),
            ).fetchone():
                connection.execute(
                    "INSERT INTO read_cursors"
                    " (project, member_name, cursor, updated_at)"
                    " VALUES (?, ?, ?, ?)",
                    (member_project, member, max_id, now),
                )
        return {
            "ok": True,
            "group_project": group_project,
            "group": group,
            "member_project": member_project,
            "member": member,
        }

    def remove(
        self,
        group_project: str,
        group: str,
        member_project: str,
        member: str,
    ):
        with self._connect() as connection:
            if not self._exists(connection, group_project, group):
                return self._missing(group_project, group)
            connection.execute(
                "DELETE FROM group_members"
                " WHERE group_project=? AND group_name=?"
                " AND member_project=? AND member_name=?",
                (
                    group_project,
                    group,
                    member_project,
                    member,
                ),
            )
        return {
            "ok": True,
            "group_project": group_project,
            "group": group,
            "member_project": member_project,
            "member": member,
        }

    def rename(self, project: str, old: str, new: str):
        with self._connect() as connection:
            connection.execute("PRAGMA defer_foreign_keys = ON")
            connection.execute("BEGIN IMMEDIATE")
            try:
                if not self._exists(connection, project, old):
                    connection.execute("ROLLBACK")
                    return self._missing(project, old)
                if self._exists(connection, project, new):
                    connection.execute("ROLLBACK")
                    return {
                        "error": (
                            f"group name '{new}' already taken in project "
                            f"'{project}'"
                        )
                    }
                connection.execute(
                    "UPDATE groups SET name=? WHERE project=? AND name=?",
                    (new, project, old),
                )
                connection.execute(
                    "UPDATE group_members SET group_name=?"
                    " WHERE group_project=? AND group_name=?",
                    (new, project, old),
                )
                result = connection.execute(
                    "UPDATE messages SET recipient=?"
                    " WHERE recipient_project=? AND recipient=?",
                    (new, project, old),
                )
                connection.execute("COMMIT")
                return {
                    "ok": True,
                    "project": project,
                    "old": old,
                    "new": new,
                    "messages_migrated": result.rowcount,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def list_groups(self, member_project, member):
        with self._connect() as connection:
            if member:
                rows = connection.execute(
                    "SELECT g.project, g.name,"
                    " COUNT(gm2.member_name) AS member_count"
                    " FROM groups g JOIN group_members gm"
                    " ON g.project=gm.group_project"
                    " AND g.name=gm.group_name"
                    " AND gm.member_project=? AND gm.member_name=?"
                    " LEFT JOIN group_members gm2"
                    " ON g.project=gm2.group_project"
                    " AND g.name=gm2.group_name"
                    " GROUP BY g.project, g.name",
                    (member_project or "", member),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT g.project, g.name,"
                    " COUNT(gm.member_name) AS member_count"
                    " FROM groups g LEFT JOIN group_members gm"
                    " ON g.project=gm.group_project"
                    " AND g.name=gm.group_name"
                    " GROUP BY g.project, g.name"
                ).fetchall()
        return [
            {
                "project": row["project"],
                "name": row["name"],
                "member_count": row["member_count"],
            }
            for row in rows
        ]

    def members(self, project: str, name: str):
        with self._connect() as connection:
            if not self._exists(connection, project, name):
                return self._missing(project, name)
            rows = connection.execute(
                "SELECT member_project, member_name FROM group_members"
                " WHERE group_project=? AND group_name=?"
                " ORDER BY member_project, member_name",
                (project, name),
            ).fetchall()
        return [
            self._format_identity(
                row["member_name"],
                row["member_project"],
            )
            for row in rows
        ]

    def get_charter(self, project: str, name: str):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT charter FROM groups WHERE project=? AND name=?",
                (project, name),
            ).fetchone()
        if row is None:
            return self._missing(project, name)
        return {"charter": row["charter"] or ""}

    def set_charter(self, project: str, name: str, charter):
        with self._connect() as connection:
            if not self._exists(connection, project, name):
                return self._missing(project, name)
            value = charter if charter else None
            connection.execute(
                "UPDATE groups SET charter=? WHERE project=? AND name=?",
                (value, project, name),
            )
        return {
            "ok": True,
            "project": project,
            "name": name,
            "charter": value or "",
        }

    def purge(self, project: str, name: str):
        with self._connect() as connection:
            if not self._exists(connection, project, name):
                return self._missing(project, name)
            connection.execute("BEGIN IMMEDIATE")
            try:
                count = connection.execute(
                    "SELECT COUNT(*) c FROM messages"
                    " WHERE recipient_project=? AND recipient=?",
                    (project, name),
                ).fetchone()["c"]
                connection.execute(
                    "DELETE FROM messages"
                    " WHERE recipient_project=? AND recipient=?",
                    (project, name),
                )
                connection.execute(
                    "DELETE FROM groups WHERE project=? AND name=?",
                    (project, name),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return {"ok": True, "purged": count}
