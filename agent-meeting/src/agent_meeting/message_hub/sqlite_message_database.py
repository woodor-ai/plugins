"""SQLite schema, connections, and compatibility upgrades for am-msgd."""

import contextlib
import sqlite3
from pathlib import Path


_TABLE_STATEMENTS = (
    """
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
)
""",
    """
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
)
""",
    """
CREATE TABLE IF NOT EXISTS read_cursors (
  project     TEXT NOT NULL,
  member_name TEXT NOT NULL,
  cursor      INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL,
  PRIMARY KEY (project, member_name)
)
""",
    """
CREATE TABLE IF NOT EXISTS cursor_migrations (
  project      TEXT NOT NULL,
  member_name  TEXT NOT NULL,
  migration_id TEXT NOT NULL,
  applied_at   INTEGER NOT NULL,
  PRIMARY KEY (project, member_name, migration_id)
)
""",
    """
CREATE TABLE IF NOT EXISTS groups (
  project    TEXT NOT NULL,
  name       TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  creator    TEXT,
  charter    TEXT,
  PRIMARY KEY (project, name)
)
""",
    """
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
)
""",
)

_INDEX_STATEMENTS = (
    """
CREATE INDEX IF NOT EXISTS idx_messages_recipient
  ON messages(recipient_project, recipient, id)
""",
    """
CREATE INDEX IF NOT EXISTS idx_messages_sender_recipient
  ON messages(sender_project, sender, recipient_project, recipient, id)
""",
    """
CREATE INDEX IF NOT EXISTS idx_group_members_member
  ON group_members(member_project, member_name)
""",
)

SCHEMA = (
    "PRAGMA journal_mode = WAL;\n"
    "PRAGMA foreign_keys = ON;\n"
    + ";\n".join(
        statement.strip()
        for statement in (*_TABLE_STATEMENTS, *_INDEX_STATEMENTS)
    )
    + ";\n"
)

_EXPECTED_PRIMARY_KEYS = {
    "sessions": ("project", "name"),
    "messages": ("id",),
    "read_cursors": ("project", "member_name"),
    "cursor_migrations": ("project", "member_name", "migration_id"),
    "groups": ("project", "name"),
    "group_members": (
        "group_project",
        "group_name",
        "member_project",
        "member_name",
    ),
}

_EXPECTED_COLUMNS = {
    "sessions": {
        "project",
        "name",
        "cwd",
        "host",
        "os",
        "instance",
        "registered_at",
        "last_seen",
        "role",
        "client_version",
    },
    "messages": {
        "id",
        "sender_project",
        "sender",
        "recipient_project",
        "recipient",
        "kind",
        "body",
        "ask",
        "created_at",
    },
    "read_cursors": {"project", "member_name", "cursor", "updated_at"},
    "cursor_migrations": {
        "project",
        "member_name",
        "migration_id",
        "applied_at",
    },
    "groups": {"project", "name", "created_at", "creator", "charter"},
    "group_members": {
        "group_project",
        "group_name",
        "member_project",
        "member_name",
        "added_at",
        "joined_after_message_id",
    },
}


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


def _table_rows(
    connection: sqlite3.Connection,
    table: str,
) -> list[dict]:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if exists is None:
        return []
    return [
        dict(row)
        for row in connection.execute(f"SELECT * FROM {table}").fetchall()
    ]


def _table_shape(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[set[str], tuple[str, ...]] | None:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    if not rows:
        return None
    columns = {row[1] for row in rows}
    primary_key = tuple(
        row[1]
        for row in sorted(rows, key=lambda row: row[5])
        if row[5]
    )
    return columns, primary_key


def _requires_composite_identity_upgrade(
    connection: sqlite3.Connection,
) -> bool:
    for table, expected_columns in _EXPECTED_COLUMNS.items():
        shape = _table_shape(connection, table)
        if shape is None:
            continue
        columns, primary_key = shape
        if (
            not expected_columns.issubset(columns)
            or primary_key != _EXPECTED_PRIMARY_KEYS[table]
        ):
            return True
    return False


def _row_value(row: dict, key: str, default=None):
    value = row.get(key, default)
    return default if value is None else value


def _project_value(value) -> str:
    normalized = str(value or "").strip()
    return normalized or "*"


def _unique_project(
    projects_by_name: dict[str, set[str]],
    name: str,
) -> str:
    projects = projects_by_name.get(name, set())
    return next(iter(projects)) if len(projects) == 1 else "*"


def _create_current_schema(connection: sqlite3.Connection) -> None:
    for statement in _TABLE_STATEMENTS:
        connection.execute(statement)
    for statement in _INDEX_STATEMENTS:
        connection.execute(statement)


def _upgrade_composite_identity_schema(
    connection: sqlite3.Connection,
) -> None:
    """Rebuild legacy single-name tables as composite project/name tables."""
    if not _requires_composite_identity_upgrade(connection):
        return

    legacy = {
        table: _table_rows(connection, table)
        for table in _EXPECTED_COLUMNS
    }

    sessions = []
    session_projects: dict[str, set[str]] = {}
    for row in legacy["sessions"]:
        name = str(_row_value(row, "name", ""))
        project = _project_value(row.get("project"))
        sessions.append(
            (
                project,
                name,
                row.get("cwd"),
                row.get("host"),
                row.get("os"),
                row.get("instance"),
                row.get("registered_at"),
                row.get("last_seen"),
                _row_value(row, "role", "worker"),
                row.get("client_version"),
            )
        )
        session_projects.setdefault(name, set()).add(project)

    groups = []
    group_projects: dict[str, set[str]] = {}
    for row in legacy["groups"]:
        name = str(_row_value(row, "name", ""))
        creator = row.get("creator")
        project = (
            _project_value(row.get("project"))
            if row.get("project")
            else _unique_project(session_projects, str(creator or ""))
        )
        groups.append(
            (
                project,
                name,
                _row_value(row, "created_at", 0),
                creator,
                row.get("charter"),
            )
        )
        group_projects.setdefault(name, set()).add(project)

    messages = []
    for row in legacy["messages"]:
        sender = str(_row_value(row, "sender", ""))
        recipient = str(_row_value(row, "recipient", ""))
        sender_project = (
            _project_value(row.get("sender_project"))
            if row.get("sender_project")
            else _unique_project(session_projects, sender)
        )
        recipient_project = (
            _project_value(row.get("recipient_project"))
            if row.get("recipient_project")
            else (
                _unique_project(group_projects, recipient)
                if recipient in group_projects
                else _unique_project(session_projects, recipient)
            )
        )
        messages.append(
            (
                row.get("id"),
                sender_project,
                sender,
                recipient_project,
                recipient,
                _row_value(row, "kind", "回应"),
                _row_value(row, "body", ""),
                row.get("ask"),
                _row_value(row, "created_at", 0),
            )
        )

    read_cursors = []
    for row in legacy["read_cursors"]:
        name = str(_row_value(row, "member_name", ""))
        project = (
            _project_value(row.get("project"))
            if row.get("project")
            else _unique_project(session_projects, name)
        )
        read_cursors.append(
            (
                project,
                name,
                _row_value(row, "cursor", 0),
                _row_value(row, "updated_at", 0),
            )
        )

    cursor_migrations = []
    for row in legacy["cursor_migrations"]:
        name = str(_row_value(row, "member_name", ""))
        project = (
            _project_value(row.get("project"))
            if row.get("project")
            else _unique_project(session_projects, name)
        )
        cursor_migrations.append(
            (
                project,
                name,
                str(_row_value(row, "migration_id", "")),
                _row_value(row, "applied_at", 0),
            )
        )

    group_members = []
    for row in legacy["group_members"]:
        group_name = str(_row_value(row, "group_name", ""))
        member_name = str(_row_value(row, "member_name", ""))
        group_project = (
            _project_value(row.get("group_project"))
            if row.get("group_project")
            else _unique_project(group_projects, group_name)
        )
        member_project = (
            _project_value(row.get("member_project"))
            if row.get("member_project")
            else _unique_project(session_projects, member_name)
        )
        group_members.append(
            (
                group_project,
                group_name,
                member_project,
                member_name,
                _row_value(row, "added_at", 0),
                _row_value(row, "joined_after_message_id", 0),
            )
        )

    foreign_keys_enabled = bool(
        connection.execute("PRAGMA foreign_keys").fetchone()[0]
    )
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("BEGIN IMMEDIATE")
    try:
        for table in (
            "group_members",
            "groups",
            "cursor_migrations",
            "read_cursors",
            "messages",
            "sessions",
        ):
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        _create_current_schema(connection)
        connection.executemany(
            "INSERT OR REPLACE INTO sessions"
            " (project, name, cwd, host, os, instance, registered_at,"
            " last_seen, role, client_version)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            sessions,
        )
        connection.executemany(
            "INSERT OR REPLACE INTO groups"
            " (project, name, created_at, creator, charter)"
            " VALUES (?, ?, ?, ?, ?)",
            groups,
        )
        connection.executemany(
            "INSERT OR REPLACE INTO messages"
            " (id, sender_project, sender, recipient_project, recipient,"
            " kind, body, ask, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            messages,
        )
        connection.executemany(
            "INSERT OR REPLACE INTO read_cursors"
            " (project, member_name, cursor, updated_at)"
            " VALUES (?, ?, ?, ?)",
            read_cursors,
        )
        connection.executemany(
            "INSERT OR REPLACE INTO cursor_migrations"
            " (project, member_name, migration_id, applied_at)"
            " VALUES (?, ?, ?, ?)",
            cursor_migrations,
        )
        connection.executemany(
            "INSERT OR REPLACE INTO group_members"
            " (group_project, group_name, member_project, member_name,"
            " added_at, joined_after_message_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            group_members,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        if foreign_keys_enabled:
            connection.execute("PRAGMA foreign_keys = ON")


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
        _upgrade_composite_identity_schema(connection)
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
