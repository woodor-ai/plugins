import importlib
import io
import sqlite3
import struct
import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PLUGIN_ROOT / "src"


def _import(module_name: str):
    sys.path.insert(0, str(SOURCE_ROOT))
    try:
        return importlib.import_module(module_name)
    finally:
        sys.path.remove(str(SOURCE_ROOT))


def test_mdns_address_selection_prefers_private_lan_and_rejects_cgnat():
    module = _import("agent_meeting.message_hub.mdns_hub_advertiser")

    selected = module.select_advertise_address(
        {
            "127.0.0.1",
            "100.82.70.77",
            "203.0.113.7",
            "192.168.50.4",
        }
    )

    assert selected == "192.168.50.4"


def test_mdns_address_selection_honors_explicit_override(monkeypatch):
    module = _import("agent_meeting.message_hub.mdns_hub_advertiser")
    monkeypatch.setenv("MEETING_ADVERTISE_IP", "10.44.0.9")

    assert module.select_advertise_address({"192.168.50.4"}) == "10.44.0.9"


def test_repository_preparation_upgrades_legacy_columns(tmp_path):
    module = _import("agent_meeting.message_hub.sqlite_message_database")
    database = tmp_path / "rooms.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE sessions (
          project TEXT NOT NULL,
          name TEXT NOT NULL,
          PRIMARY KEY (project, name)
        );
        CREATE TABLE groups (
          project TEXT NOT NULL,
          name TEXT NOT NULL,
          PRIMARY KEY (project, name)
        );
        """
    )
    connection.close()

    module.prepare_message_database(database)

    connection = sqlite3.connect(database)
    session_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(sessions)")
    }
    group_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(groups)")
    }
    group_member_columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(group_members)")
    }
    connection.close()
    assert {"instance", "client_version"} <= session_columns
    assert "charter" in group_columns
    assert "joined_after_message_id" in group_member_columns


def test_repository_conversation_clause_uses_both_composite_identities():
    module = _import("agent_meeting.message_hub.sqlite_message_database")

    clause, parameters = module.conversation_clause(
        "project-a",
        "alice",
        "project-b",
        "bob",
    )

    assert "sender_project=?" in clause
    assert "recipient_project=?" in clause
    assert parameters == [
        "project-a",
        "alice",
        "project-b",
        "bob",
        "project-b",
        "bob",
        "project-a",
        "alice",
    ]


def test_group_repository_owns_group_transactions(tmp_path):
    database_module = _import(
        "agent_meeting.message_hub.sqlite_message_database"
    )
    group_module = _import(
        "agent_meeting.message_hub.sqlite_group_repository"
    )
    database = tmp_path / "rooms.db"
    database_module.prepare_message_database(database)
    repository = group_module.SQLiteGroupRepository(
        connect=lambda: database_module.open_message_database(database),
        format_identity=lambda name, project: f"{name}@{project}",
    )

    created = repository.create(
        "tools",
        "reviewers",
        ["alice@client", "bob"],
        "owner",
    )
    charter = repository.set_charter(
        "tools",
        "reviewers",
        "Review changes",
    )
    renamed = repository.rename("tools", "reviewers", "approvers")

    assert created["members"] == ["alice@client", "bob@tools"]
    assert charter["charter"] == "Review changes"
    assert renamed["new"] == "approvers"
    assert repository.members("tools", "approvers") == [
        "alice@client",
        "bob@tools",
    ]
    assert repository.list_groups("client", "alice") == [
        {
            "project": "tools",
            "name": "approvers",
            "member_count": 2,
        }
    ]
    assert repository.purge("tools", "approvers") == {
        "ok": True,
        "purged": 0,
    }


def test_session_repository_owns_identity_and_cursor_transactions(tmp_path):
    database_module = _import(
        "agent_meeting.message_hub.sqlite_message_database"
    )
    session_module = _import(
        "agent_meeting.message_hub.sqlite_session_repository"
    )
    database = tmp_path / "rooms.db"
    database_module.prepare_message_database(database)
    repository = session_module.SQLiteSessionRepository(
        connect=lambda: database_module.open_message_database(database),
        online_threshold=12,
    )

    registered = repository.register(
        "tools",
        "alice",
        "/work/tools",
        False,
        instance="instance-a",
    )
    renamed = repository.rename("tools", "alice", "reviewer")

    assert registered["cursor"] == 0
    assert renamed["new"] == "reviewer"
    assert repository.resolve_candidates("reviewer") == [
        {
            "project": "tools",
            "name": "reviewer",
            "kind": "session",
        }
    ]
    assert repository.list_sessions()[0]["name"] == "reviewer"
    assert repository.unregister(
        "tools",
        "reviewer",
        "instance-a",
    )["deleted"] is True


def test_conversation_repository_owns_message_transactions(tmp_path):
    database_module = _import(
        "agent_meeting.message_hub.sqlite_message_database"
    )
    conversation_module = _import(
        "agent_meeting.message_hub.sqlite_conversation_repository"
    )
    database = tmp_path / "rooms.db"
    database_module.prepare_message_database(database)
    with database_module.open_message_database(database) as connection:
        connection.execute(
            "INSERT INTO sessions"
            " (project, name, instance, last_seen)"
            " VALUES ('tools', 'bob', 'instance-b', 1)"
        )
        connection.execute(
            "INSERT INTO read_cursors"
            " (project, member_name, cursor, updated_at)"
            " VALUES ('tools', 'bob', 0, 1)"
        )
    fanout_calls = []
    repository = conversation_module.SQLiteConversationRepository(
        connect=lambda: database_module.open_message_database(database),
        format_identity=lambda name, project: f"{name}@{project}",
        fanout=lambda *arguments: fanout_calls.append(arguments),
    )

    sent = repository.send_message(
        "tools",
        "alice",
        "tools",
        "bob",
        "Please review",
        "request",
        "Reply with findings",
    )
    inbox = repository.read_inbox(
        "tools",
        "bob",
        "instance-b",
        10,
    )

    assert sent == {"msg_id": 1, "turn": "bob@tools"}
    assert fanout_calls[0][0] == 1
    assert repository.current_turn(
        "tools",
        "alice",
        "tools",
        "bob",
    ) == "bob@tools"
    assert inbox["messages"][0]["body"] == "Please review"
    assert repository.read_message(
        "tools",
        "bob",
        1,
    )["sender_identity"] == "alice@tools"
    assert repository.delete_conversation(
        "tools",
        "alice",
        "tools",
        "bob",
    )["msg_count"] == 1


def test_websocket_subscription_parses_masked_client_frame():
    module = _import("agent_meeting.message_hub.websocket_subscriptions")
    payload = "你好".encode("utf-8")
    mask = b"\x01\x02\x03\x04"
    masked = bytes(
        byte ^ mask[index % 4]
        for index, byte in enumerate(payload)
    )
    stream = io.BytesIO(
        bytes((0x81, 0x80 | len(payload))) + mask + masked
    )

    opcode, decoded = module.read_frame(stream)

    assert opcode == 1
    assert decoded == payload


def test_websocket_subscription_writes_extended_text_frame():
    module = _import("agent_meeting.message_hub.websocket_subscriptions")

    class FlushableBytesIO(io.BytesIO):
        def flush(self):
            return None

    output = FlushableBytesIO()
    subscriber = module.Subscriber(
        "project",
        "agent",
        object(),
        output,
        cursor=7,
    )

    assert module.send_text(subscriber, "x" * 126)
    frame = output.getvalue()
    assert frame[:2] == b"\x81\x7e"
    assert struct.unpack("!H", frame[2:4])[0] == 126
    assert frame[4:] == b"x" * 126


def test_websocket_mentions_are_limited_to_group_members():
    module = _import("agent_meeting.message_hub.websocket_subscriptions")

    assert module.parse_mentions(
        "请 @alice 和 @mallory 看一下",
        {"alice", "bob"},
    ) == {"alice"}
