"""Integration coverage for the am-msgd recipient-wide ordered inbox API."""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
AM_MSGD = ROOT / "bin" / "am-msgd"
AM = ROOT / "bin" / "am"


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def recv_exact(sock, size):
    chunks = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise RuntimeError("websocket closed before the full frame arrived")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_ws_json(sock):
    first, length = recv_exact(sock, 2)
    assert first & 0x0F == 0x1
    length &= 0x7F
    if length == 126:
        length = int.from_bytes(recv_exact(sock, 2), "big")
    elif length == 127:
        length = int.from_bytes(recv_exact(sock, 8), "big")
    return json.loads(recv_exact(sock, length).decode("utf-8"))


def request(base, method, path, body=None, params=None):
    url = base + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read().decode("utf-8"))


@pytest.fixture
def am_msgd(tmp_path):
    meeting_home = tmp_path / "meeting-home"
    db_dir = meeting_home / "db"
    db_dir.mkdir(parents=True)
    (db_dir / "rooms.db").touch()
    port = free_port()
    env = os.environ.copy()
    env["MEETING_HOME"] = str(meeting_home)
    process = subprocess.Popen(
        [
            os.environ.get("PYTHON", os.sys.executable),
            str(AM_MSGD),
            "serve",
            "--bind",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-mdns",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if request(base, "GET", "/health").get("ok"):
                break
        except OSError:
            time.sleep(0.05)
    else:
        process.terminate()
        _, stderr = process.communicate(timeout=5)
        pytest.fail(f"am-msgd did not start: {stderr}")
    try:
        yield base
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def register(base, name, instance, force=False, legacy_cursor=None):
    payload = {
        "project": "proj",
        "name": name,
        "cwd": "/tmp/proj",
        "instance": instance,
        "role": "worker",
        "force": force,
    }
    if legacy_cursor is not None:
        payload["legacy_cursor"] = legacy_cursor
    result = request(
        base,
        "POST",
        "/register",
        payload,
    )
    assert not result.get("error"), result
    return result


def send(base, sender, recipient, body):
    result = request(
        base,
        "POST",
        "/send",
        {
            "self_project": "proj",
            "self": sender,
            "peer_project": "proj",
            "peer": recipient,
            "body": body,
            "kind": "request",
        },
    )
    assert not result.get("error"), result
    return result["msg_id"]


def test_subscribe_uses_http_11_websocket_handshake(am_msgd):
    parsed = urllib.parse.urlparse(am_msgd)
    with socket.create_connection((parsed.hostname, parsed.port), timeout=5) as sock:
        sock.sendall(
            (
                "GET /subscribe HTTP/1.1\r\n"
                f"Host: {parsed.hostname}:{parsed.port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                "X-Meeting-Name: strict-client\r\n"
                "X-Meeting-Project: proj\r\n"
                "X-Meeting-Proto: 1\r\n"
                "\r\n"
            ).encode("ascii")
        )
        status_line = b""
        while not status_line.endswith(b"\r\n"):
            status_line += sock.recv(1)

    assert status_line == b"HTTP/1.1 101 Switching Protocols\r\n"


def test_notify_subscription_does_not_advance_central_cursor(am_msgd):
    register(am_msgd, "alice", "instance-alice")
    registration = register(am_msgd, "bob", "instance-bob")
    parsed = urllib.parse.urlparse(am_msgd)
    with socket.create_connection((parsed.hostname, parsed.port), timeout=5) as sock:
        sock.sendall(
            (
                "GET /subscribe HTTP/1.1\r\n"
                f"Host: {parsed.hostname}:{parsed.port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                "X-Meeting-Name: bob\r\n"
                "X-Meeting-Project: proj\r\n"
                "X-Meeting-Proto: 1\r\n"
                "X-Meeting-Mode: notify\r\n"
                "X-Meeting-Instance: instance-bob\r\n"
                "\r\n"
            ).encode("ascii")
        )
        response = b""
        while b"\r\n\r\n" not in response:
            response += sock.recv(1)
        assert response.startswith(b"HTTP/1.1 101 Switching Protocols\r\n")
        sync_frame = recv_ws_json(sock)
        message_id = send(am_msgd, "alice", "bob", "notify only")
        frame = recv_ws_json(sock)

    inbox = request(
        am_msgd,
        "GET",
        "/inbox",
        params={
            "project": "proj",
            "name": "bob",
            "instance": "instance-bob",
        },
    )

    assert sync_frame == {"type": "notify", "reason": "subscribed"}
    assert frame == {"type": "notify", "msg_id": message_id}
    assert inbox["cursor"] == registration["cursor"]
    assert [row["id"] for row in inbox["messages"]] == [message_id]


def test_delivery_subscription_rejects_stale_instance_without_advancing(am_msgd):
    register(am_msgd, "alice", "instance-alice")
    registration = register(am_msgd, "bob", "instance-current")
    message_id = send(am_msgd, "alice", "bob", "must remain unread")
    parsed = urllib.parse.urlparse(am_msgd)

    with socket.create_connection((parsed.hostname, parsed.port), timeout=5) as sock:
        sock.sendall(
            (
                "GET /subscribe HTTP/1.1\r\n"
                f"Host: {parsed.hostname}:{parsed.port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                "X-Meeting-Name: bob\r\n"
                "X-Meeting-Project: proj\r\n"
                "X-Meeting-Proto: 1\r\n"
                "X-Meeting-Instance: instance-stale\r\n"
                "\r\n"
            ).encode("ascii")
        )
        status_line = b""
        while not status_line.endswith(b"\r\n"):
            status_line += sock.recv(1)

    inbox = request(
        am_msgd,
        "GET",
        "/inbox",
        params={
            "project": "proj",
            "name": "bob",
            "instance": "instance-current",
        },
    )

    assert status_line == b"HTTP/1.1 409 Conflict\r\n"
    assert inbox["cursor"] == registration["cursor"]
    assert [row["id"] for row in inbox["messages"]] == [message_id]


def test_inbox_orders_direct_and_group_messages_by_global_id(am_msgd):
    for name in ("alice", "bob", "carol"):
        register(am_msgd, name, f"instance-{name}")
    created = request(
        am_msgd,
        "POST",
        "/group/create",
        {
            "project": "proj",
            "name": "review",
            "members": ["bob@proj", "carol@proj"],
            "creator": "alice",
        },
    )
    assert created.get("ok"), created

    direct_id = send(am_msgd, "alice", "bob", "direct")
    group_id = send(am_msgd, "alice", "review", "@carol directed")
    later_id = send(am_msgd, "carol", "bob", "later")

    inbox = request(
        am_msgd,
        "GET",
        "/inbox",
        params={
            "project": "proj",
            "name": "bob",
            "instance": "instance-bob",
        },
    )

    assert [row["id"] for row in inbox["messages"]] == [
        direct_id,
        group_id,
        later_id,
    ]
    assert inbox["messages"][0]["deliver"] is True
    assert inbox["messages"][1]["group"] == "review@proj"
    assert inbox["messages"][1]["deliver"] is False
    assert inbox["messages"][2]["sender_identity"] == "carol@proj"
    assert inbox["high_water_mark"] == later_id


def test_offline_message_survives_first_broker_local_start(am_msgd):
    register(am_msgd, "alice", "instance-alice")
    first = register(am_msgd, "bob", "instance-bob-old")
    request(
        am_msgd,
        "POST",
        "/unregister",
        {"project": "proj", "name": "bob", "instance": "instance-bob-old"},
    )

    message_id = send(am_msgd, "alice", "bob", "sent while offline")
    resumed = register(am_msgd, "bob", "instance-bob-new")
    inbox = request(
        am_msgd,
        "GET",
        "/inbox",
        params={
            "project": "proj",
            "name": "bob",
            "instance": "instance-bob-new",
        },
    )

    assert resumed["cursor"] == first["cursor"]
    assert [row["id"] for row in inbox["messages"]] == [message_id]
    assert inbox["cursor"] == first["cursor"]


def test_legacy_broker_cursor_migrates_central_cursor_exactly_once(am_msgd):
    register(am_msgd, "alice", "instance-alice")
    initial = register(am_msgd, "bob", "instance-bob")
    first_id = send(am_msgd, "alice", "bob", "sent but not injected")
    advanced = request(
        am_msgd,
        "POST",
        "/ack",
        {
            "project": "proj",
            "name": "bob",
            "instance": "instance-bob",
            "expected_cursor": initial["cursor"],
            "through": first_id,
        },
    )
    assert advanced["cursor"] == first_id

    migrated = register(
        am_msgd,
        "bob",
        "instance-bob",
        legacy_cursor=initial["cursor"],
    )
    replay = request(
        am_msgd,
        "GET",
        "/inbox",
        params={
            "project": "proj",
            "name": "bob",
            "instance": "instance-bob",
        },
    )
    assert migrated["cursor"] == initial["cursor"]
    assert [row["id"] for row in replay["messages"]] == [first_id]

    acknowledged = request(
        am_msgd,
        "POST",
        "/ack",
        {
            "project": "proj",
            "name": "bob",
            "instance": "instance-bob",
            "expected_cursor": initial["cursor"],
            "through": first_id,
        },
    )
    assert acknowledged["cursor"] == first_id
    repeated = register(
        am_msgd,
        "bob",
        "instance-bob",
        legacy_cursor=initial["cursor"],
    )
    assert repeated["cursor"] == first_id


def test_ack_requires_current_instance_and_expected_cursor(am_msgd):
    register(am_msgd, "alice", "instance-alice")
    registration = register(am_msgd, "bob", "instance-bob")
    message_id = send(am_msgd, "alice", "bob", "ack me")

    stale_instance = request(
        am_msgd,
        "POST",
        "/ack",
        {
            "project": "proj",
            "name": "bob",
            "instance": "wrong-instance",
            "expected_cursor": registration["cursor"],
            "through": message_id,
        },
    )
    acknowledged = request(
        am_msgd,
        "POST",
        "/ack",
        {
            "project": "proj",
            "name": "bob",
            "instance": "instance-bob",
            "expected_cursor": registration["cursor"],
            "through": message_id,
        },
    )
    stale_cursor = request(
        am_msgd,
        "POST",
        "/ack",
        {
            "project": "proj",
            "name": "bob",
            "instance": "instance-bob",
            "expected_cursor": registration["cursor"],
            "through": message_id,
        },
    )
    inbox = request(
        am_msgd,
        "GET",
        "/inbox",
        params={
            "project": "proj",
            "name": "bob",
            "instance": "instance-bob",
        },
    )

    assert stale_instance["code"] == "stale_instance"
    assert acknowledged == {"ok": True, "cursor": message_id}
    assert stale_cursor["code"] == "cursor_conflict"
    assert inbox["cursor"] == message_id
    assert inbox["messages"] == []


def test_ack_rejects_future_message_but_allows_pulled_group_after_membership_change(am_msgd):
    register(am_msgd, "alice", "instance-alice")
    registration = register(am_msgd, "bob", "instance-bob")
    register(am_msgd, "carol", "instance-carol")
    created = request(
        am_msgd,
        "POST",
        "/group/create",
        {
            "project": "proj",
            "name": "review",
            "members": ["bob@proj", "carol@proj"],
            "creator": "alice",
        },
    )
    assert created.get("ok"), created
    group_id = send(am_msgd, "alice", "review", "@bob please review")
    pulled = request(
        am_msgd,
        "GET",
        "/inbox",
        params={
            "project": "proj",
            "name": "bob",
            "instance": "instance-bob",
        },
    )
    assert [row["id"] for row in pulled["messages"]] == [group_id]
    removed = request(
        am_msgd,
        "POST",
        "/group/remove",
        {
            "group_project": "proj",
            "group": "review",
            "member_project": "proj",
            "member": "bob",
        },
    )
    assert removed.get("ok"), removed

    acknowledged = request(
        am_msgd,
        "POST",
        "/ack",
        {
            "project": "proj",
            "name": "bob",
            "instance": "instance-bob",
            "expected_cursor": registration["cursor"],
            "through": group_id,
        },
    )
    future = request(
        am_msgd,
        "POST",
        "/ack",
        {
            "project": "proj",
            "name": "bob",
            "instance": "instance-bob",
            "expected_cursor": group_id,
            "through": group_id + 1000,
        },
    )
    inbox = request(
        am_msgd,
        "GET",
        "/inbox",
        params={
            "project": "proj",
            "name": "bob",
            "instance": "instance-bob",
        },
    )

    assert acknowledged == {"ok": True, "cursor": group_id}
    assert future["code"] == "invalid_ack_target"
    assert inbox["cursor"] == group_id
    assert inbox["messages"] == []


def test_ack_allows_pulled_message_deleted_before_ack(am_msgd):
    register(am_msgd, "alice", "instance-alice")
    registration = register(am_msgd, "bob", "instance-bob")
    message_id = send(am_msgd, "alice", "bob", "delete after pull")
    pulled = request(
        am_msgd,
        "GET",
        "/inbox",
        params={
            "project": "proj",
            "name": "bob",
            "instance": "instance-bob",
        },
    )
    assert [row["id"] for row in pulled["messages"]] == [message_id]
    deleted = request(
        am_msgd,
        "DELETE",
        "/conversation",
        params={
            "self_project": "proj",
            "self": "bob",
            "peer_project": "proj",
            "peer": "alice",
        },
    )
    assert deleted.get("deleted"), deleted

    acknowledged = request(
        am_msgd,
        "POST",
        "/ack",
        {
            "project": "proj",
            "name": "bob",
            "instance": "instance-bob",
            "expected_cursor": registration["cursor"],
            "through": message_id,
        },
    )

    assert acknowledged == {"ok": True, "cursor": message_id}


def test_inbox_rejects_replaced_registration_instance(am_msgd):
    register(am_msgd, "bob", "instance-old")
    register(am_msgd, "bob", "instance-new", force=True)

    stale = request(
        am_msgd,
        "GET",
        "/inbox",
        params={
            "project": "proj",
            "name": "bob",
            "instance": "instance-old",
        },
    )

    assert stale["code"] == "stale_instance"


def test_exact_message_requires_participation(am_msgd):
    for name in ("alice", "bob", "carol"):
        register(am_msgd, name, f"instance-{name}")
    message_id = send(am_msgd, "alice", "bob", "precise body")

    visible = request(
        am_msgd,
        "GET",
        "/message",
        params={"project": "proj", "name": "bob", "id": message_id},
    )
    hidden = request(
        am_msgd,
        "GET",
        "/message",
        params={"project": "proj", "name": "carol", "id": message_id},
    )

    assert visible["id"] == message_id
    assert visible["body"] == "precise body"
    assert visible["sender_identity"] == "alice@proj"
    assert "not visible" in hidden["error"]


def test_unregister_only_deletes_the_matching_instance(am_msgd):
    register(am_msgd, "bob", "instance-a")

    wrong = request(
        am_msgd,
        "POST",
        "/unregister",
        {"project": "proj", "name": "bob", "instance": "instance-b"},
    )
    rows = request(am_msgd, "GET", "/list")
    right = request(
        am_msgd,
        "POST",
        "/unregister",
        {"project": "proj", "name": "bob", "instance": "instance-a"},
    )

    assert wrong["deleted"] is False
    assert any(row["name"] == "bob" and row["project"] == "proj" for row in rows)
    assert right["deleted"] is True


def test_private_send_requires_full_identity_but_group_short_name_is_allowed(am_msgd):
    for name in ("alice", "bob"):
        register(am_msgd, name, f"instance-{name}")
    created = request(
        am_msgd,
        "POST",
        "/group/create",
        {
            "project": "proj",
            "name": "review",
            "members": ["alice@proj", "bob@proj"],
            "creator": "alice",
        },
    )
    assert created.get("ok"), created
    env = os.environ.copy()
    env["MEETING_NO_TELEMETRY"] = "1"

    bare_private = subprocess.run(
        [
            sys.executable,
            str(AM),
            "send",
            "alice@proj",
            "bob",
            "unsafe",
            "--host",
            am_msgd,
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    full_private = subprocess.run(
        [
            sys.executable,
            str(AM),
            "send",
            "alice@proj",
            "bob@proj",
            "safe",
            "--host",
            am_msgd,
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    short_group = subprocess.run(
        [
            sys.executable,
            str(AM),
            "send",
            "alice@proj",
            "review",
            "group",
            "--host",
            am_msgd,
        ],
        env=env,
        capture_output=True,
        text=True,
    )

    assert bare_private.returncode != 0
    assert "must use its full identity: bob@proj" in bare_private.stderr
    assert full_private.returncode == 0, full_private.stderr
    assert short_group.returncode == 0, short_group.stderr
