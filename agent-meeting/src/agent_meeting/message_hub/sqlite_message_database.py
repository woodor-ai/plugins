"""SQLite schema, connections, and compatibility upgrades for am-msgd."""

import contextlib
import sqlite3
from pathlib import Path


SCHEMA = """
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

CREATE INDEX IF NOT EXISTS idx_messages_recipient
  ON messages(recipient_project, recipient, id);
CREATE INDEX IF NOT EXISTS idx_messages_sender_recipient
  ON messages(sender_project, sender, recipient_project, recipient, id);

CREATE TABLE IF NOT EXISTS read_cursors (
  project     TEXT NOT NULL,
  member_name TEXT NOT NULL,
  cursor      INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL,
  PRIMARY KEY (project, member_name)
);

CREATE TABLE IF NOT EXISTS cursor_migrations (
  project      TEXT NOT NULL,
  member_name  TEXT NOT NULL,
  migration_id TEXT NOT NULL,
  applied_at   INTEGER NOT NULL,
  PRIMARY KEY (project, member_name, migration_id)
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
  joined_after_message_id INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (group_project, group_name, member_project, member_name),
  FOREIGN KEY (group_project, group_name)
    REFERENCES groups(project, name) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_group_members_member
  ON group_members(member_project, member_name);
"""


@contextlib.contextmanager
def open_message_database(database_path: str | Path):
    """Open one configured SQLite connection and always close it."""
    connection = sqlite3.connect(
        str(database_path),
        isolation_level=None,
        timeout=30,
    )
    connection.row_factory = sqlite3.Row
    connection.executescript(
        "PRAGMA journal_mode = WAL;"
        " PRAGMA foreign_keys = ON;"
        " PRAGMA wal_autocheckpoint = 100;"
    )
    try:
        yield connection
    finally:
        connection.close()


def _add_column_if_missing(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    declaration: str,
) -> None:
    columns = {
        row[1]
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        connection.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
        )


def prepare_message_database(database_path: str | Path) -> None:
    """Apply the idempotent schema and supported legacy-column upgrades."""
    with open_message_database(database_path) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.executescript(SCHEMA)
        _add_column_if_missing(connection, "groups", "charter", "TEXT")
        _add_column_if_missing(
            connection,
            "group_members",
            "joined_after_message_id",
            "INTEGER NOT NULL DEFAULT 0",
        )
        _add_column_if_missing(connection, "sessions", "instance", "TEXT")
        _add_column_if_missing(
            connection,
            "sessions",
            "client_version",
            "TEXT",
        )


def conversation_clause(
    first_project: str,
    first_name: str,
    second_project: str,
    second_name: str,
) -> tuple[str, list[str]]:
    """Build a composite-identity SQL clause for one direct conversation."""
    clause = (
        "((sender_project=? AND sender=? AND recipient_project=? AND recipient=?)"
        " OR (sender_project=? AND sender=? AND recipient_project=? AND recipient=?))"
    )
    parameters = [
        first_project,
        first_name,
        second_project,
        second_name,
        second_project,
        second_name,
        first_project,
        first_name,
    ]
    return clause, parameters


def is_group(connection: sqlite3.Connection, project: str, name: str) -> bool:
    """Return whether the addressed composite identity is a group."""
    return (
        connection.execute(
            "SELECT 1 FROM groups WHERE project=? AND name=?",
            (project, name),
        ).fetchone()
        is not None
    )
