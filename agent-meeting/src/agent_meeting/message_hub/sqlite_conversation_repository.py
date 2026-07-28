"""SQLite-backed direct and group message conversations for am-msgd."""

from __future__ import annotations

import time
from typing import Callable

from agent_meeting.message_hub.sqlite_message_database import (
    conversation_clause,
    is_group,
)
from agent_meeting.message_hub.websocket_subscriptions import parse_mentions


class SQLiteConversationRepository:
    def __init__(
        self,
        *,
        connect: Callable,
        format_identity: Callable[[str, str], str],
        fanout: Callable,
    ):
        self._connect = connect
        self._format_identity = format_identity
        self._fanout = fanout

    def current_turn(
        self,
        self_project: str,
        self_name: str,
        peer_project: str,
        peer_name: str,
    ) -> str:
        clause, parameters = conversation_clause(
            self_project,
            self_name,
            peer_project,
            peer_name,
        )
        with self._connect() as connection:
            row = connection.execute(
                "SELECT recipient, recipient_project FROM messages"
                f" WHERE {clause} ORDER BY id DESC LIMIT 1",
                parameters,
            ).fetchone()
        if row:
            return self._format_identity(
                row["recipient"],
                row["recipient_project"],
            )
        return self._format_identity(self_name, self_project)

    def render_conversation(
        self,
        self_project: str,
        self_name: str,
        peer_project: str,
        peer_name: str,
        limit: int,
    ) -> str:
        with self._connect() as connection:
            peer_is_group = is_group(
                connection,
                peer_project,
                peer_name,
            )
            if peer_is_group:
                messages = connection.execute(
                    "SELECT * FROM messages"
                    " WHERE recipient_project=? AND recipient=?"
                    " ORDER BY id DESC LIMIT ?",
                    (peer_project, peer_name, limit),
                ).fetchall()
                lines = [
                    f"# Group: {peer_name} (project={peer_project})\n---\n"
                ]
                for row in reversed(messages):
                    self._append_rendered_message(lines, row)
                return "\n".join(lines)

        clause, parameters = conversation_clause(
            self_project,
            self_name,
            peer_project,
            peer_name,
        )
        turn = self.current_turn(
            self_project,
            self_name,
            peer_project,
            peer_name,
        )
        with self._connect() as connection:
            messages = connection.execute(
                f"SELECT * FROM messages WHERE {clause}"
                " ORDER BY id DESC LIMIT ?",
                parameters + [limit],
            ).fetchall()
        lines = [
            "# Conversation: "
            f"{self._format_identity(self_name, self_project)} <-> "
            f"{self._format_identity(peer_name, peer_project)}",
            f"current turn: {turn}\n---\n",
        ]
        for row in reversed(messages):
            self._append_rendered_message(lines, row)
        return "\n".join(lines)

    def _append_rendered_message(self, lines: list[str], row) -> None:
        timestamp = time.strftime(
            "%Y-%m-%d %H:%M PDT",
            time.localtime(row["created_at"]),
        )
        sender_identity = self._format_identity(
            row["sender"],
            row["sender_project"],
        )
        lines.append(
            f"### [{sender_identity} @ {timestamp}] {row['kind']}"
        )
        lines.append(row["body"])
        if row["ask"]:
            lines.append(f"\n**Ask**: {row['ask']}")
        lines.append("")

    def read_conversation(
        self,
        self_project: str,
        self_name: str,
        peer_project: str,
        peer_name: str,
        limit: int,
        since: int,
    ) -> list[dict]:
        with self._connect() as connection:
            peer_is_group = is_group(
                connection,
                peer_project,
                peer_name,
            )
        if peer_is_group:
            clause = "recipient_project=? AND recipient=?"
            parameters = [peer_project, peer_name]
        else:
            clause, parameters = conversation_clause(
                self_project,
                self_name,
                peer_project,
                peer_name,
            )
        if since:
            clause += " AND id > ?"
            parameters.append(since)
        query = (
            f"SELECT * FROM messages WHERE {clause} ORDER BY id ASC"
        )
        if limit:
            query += f" LIMIT {int(limit)}"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [dict(row) for row in rows]

    def read_inbox(
        self,
        project: str,
        name: str,
        instance: str,
        limit: int,
    ) -> dict:
        limit = max(1, min(limit, 5000))
        with self._connect() as connection:
            registration = connection.execute(
                "SELECT instance FROM sessions"
                " WHERE project=? AND name=?",
                (project, name),
            ).fetchone()
            if (
                registration is None
                or not instance
                or (registration["instance"] or "") != instance
            ):
                return {
                    "error": "registration instance is no longer current",
                    "code": "stale_instance",
                }
            cursor_row = connection.execute(
                "SELECT cursor FROM read_cursors"
                " WHERE project=? AND member_name=?",
                (project, name),
            ).fetchone()
            if cursor_row is None:
                return {
                    "error": "recipient is not registered",
                    "code": "cursor_missing",
                }
            cursor = int(cursor_row["cursor"])
            high_water_mark = connection.execute(
                "SELECT COALESCE(MAX(id), 0) AS m FROM messages"
            ).fetchone()["m"]
            rows = connection.execute(
                "SELECT messages.* FROM messages"
                " WHERE id>?"
                " AND ("
                "  (recipient_project=? AND recipient=?)"
                "  OR EXISTS ("
                "   SELECT 1 FROM group_members gm"
                "   WHERE gm.member_project=? AND gm.member_name=?"
                "    AND gm.group_project=messages.recipient_project"
                "    AND gm.group_name=messages.recipient"
                "    AND messages.id>gm.joined_after_message_id"
                "  )"
                " )"
                " ORDER BY id ASC LIMIT ?",
                (cursor, project, name, project, name, limit),
            ).fetchall()
            output = [
                self._inbox_message(connection, row, name)
                for row in rows
            ]
        return {
            "messages": output,
            "cursor": cursor,
            "high_water_mark": high_water_mark,
        }

    def _inbox_message(self, connection, row, recipient_name: str) -> dict:
        item = dict(row)
        message_is_group = is_group(
            connection,
            item["recipient_project"],
            item["recipient"],
        )
        if message_is_group:
            member_rows = connection.execute(
                "SELECT member_name FROM group_members"
                " WHERE group_project=? AND group_name=?",
                (
                    item["recipient_project"],
                    item["recipient"],
                ),
            ).fetchall()
            member_names = {
                member["member_name"]
                for member in member_rows
            }
            mentions = parse_mentions(item["body"], member_names)
            item["deliver"] = (
                not mentions or recipient_name in mentions
            )
            item["group"] = self._format_identity(
                item["recipient"],
                item["recipient_project"],
            )
        else:
            item["deliver"] = True
            item["group"] = None
        item["sender_identity"] = self._format_identity(
            item["sender"],
            item["sender_project"],
        )
        item["recipient_identity"] = self._format_identity(
            item["recipient"],
            item["recipient_project"],
        )
        return item

    def read_message(
        self,
        project: str,
        name: str,
        message_id: int,
    ) -> dict:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM messages WHERE id=?",
                (message_id,),
            ).fetchone()
            if row is None:
                return {"error": f"message {message_id} not found"}
            item = dict(row)
            message_is_group = is_group(
                connection,
                item["recipient_project"],
                item["recipient"],
            )
            allowed = (
                (item["sender_project"], item["sender"])
                == (project, name)
                or (item["recipient_project"], item["recipient"])
                == (project, name)
            )
            if message_is_group and not allowed:
                allowed = (
                    connection.execute(
                        "SELECT 1 FROM group_members"
                        " WHERE group_project=? AND group_name=?"
                        " AND member_project=? AND member_name=?",
                        (
                            item["recipient_project"],
                            item["recipient"],
                            project,
                            name,
                        ),
                    ).fetchone()
                    is not None
                )
            if not allowed:
                return {
                    "error": (
                        f"message {message_id} is not visible to "
                        f"{name}@{project}"
                    )
                }
            item["group"] = (
                self._format_identity(
                    item["recipient"],
                    item["recipient_project"],
                )
                if message_is_group
                else None
            )
            item["sender_identity"] = self._format_identity(
                item["sender"],
                item["sender_project"],
            )
            item["recipient_identity"] = self._format_identity(
                item["recipient"],
                item["recipient_project"],
            )
            return item

    def send_message(
        self,
        self_project: str,
        self_name: str,
        peer_project: str,
        peer_name: str,
        body: str,
        kind: str,
        ask,
    ) -> dict:
        if not body:
            raise KeyError("body empty")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO messages"
                    " (sender_project, sender, recipient_project,"
                    " recipient, kind, body, ask, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self_project,
                        self_name,
                        peer_project,
                        peer_name,
                        kind,
                        body,
                        ask,
                        int(time.time()),
                    ),
                )
                message_id = connection.execute(
                    "SELECT last_insert_rowid()"
                ).fetchone()[0]
                peer_is_group = is_group(
                    connection,
                    peer_project,
                    peer_name,
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

        self._fanout(
            message_id,
            peer_project,
            peer_name,
            self_project,
            self_name,
            ask,
            body,
        )
        return {
            "msg_id": message_id,
            "turn": (
                None
                if peer_is_group
                else self._format_identity(peer_name, peer_project)
            ),
        }

    def delete_conversation(
        self,
        self_project: str,
        self_name: str,
        peer_project: str,
        peer_name: str,
    ) -> dict:
        clause, parameters = conversation_clause(
            self_project,
            self_name,
            peer_project,
            peer_name,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                count = connection.execute(
                    f"SELECT COUNT(*) c FROM messages WHERE {clause}",
                    parameters,
                ).fetchone()["c"]
                connection.execute(
                    f"DELETE FROM messages WHERE {clause}",
                    parameters,
                )
                connection.execute("COMMIT")
                return {"deleted": True, "msg_count": count}
            except Exception:
                connection.execute("ROLLBACK")
                raise
