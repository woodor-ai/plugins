"""Integration coverage for the amctl recipient-wide ordered inbox API."""

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
AMCTL = ROOT / "bin" / "amctl"
MEETING = ROOT / "bin" / "meeting"


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
def amctl(tmp_path):
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
            str(AMCTL),
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
        pytest.fail(f"amctl did not start: {stderr}")
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


def test_subscribe_uses_http_11_websocket_handshake(amctl):
    parsed = urllib.parse.urlparse(amctl)
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


def test_notify_subscription_does_not_advance_central_cursor(amctl):
    register(amctl, "alice", "instance-alice")
    registration = register(amctl, "bob", "instance-bob")
    parsed = urllib.parse.urlparse(amctl)
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
        message_id = send(amctl, "alice", "bob", "notify only")
        frame = recv_ws_json(sock)

    inbox = request(
        amctl,
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


def test_delivery_subscription_rejects_stale_instance_without_advancing(amctl):
    register(amctl, "alice", "instance-alice")
    registration = register(amctl, "bob", "instance-current")
    message_id = send(amctl, "alice", "bob", "must remain unread")
    parsed = urllib.parse.urlparse(amctl)

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
        amctl,
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


def test_inbox_orders_direct_and_group_messages_by_global_id(amctl):
    for name in ("alice", "bob", "carol"):
        register(amctl, name, f"instance-{name}")
    created = request(
        amctl,
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

    direct_id = send(amctl, "alice", "bob", "direct")
    group_id = send(amctl, "alice", "review", "@carol directed")
    later_id = send(amctl, "carol", "bob", "later")

    inbox = request(
        amctl,
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


def test_offline_message_survives_first_broker_local_start(amctl):
    register(amctl, "alice", "instance-alice")
    first = register(amctl, "bob", "instance-bob-old")
    request(
        amctl,
        "POST",
        "/unregister",
        {"project": "proj", "name": "bob", "instance": "instance-bob-old"},
    )

    message_id = send(amctl, "alice", "bob", "sent while offline")
    resumed = register(amctl, "bob", "instance-bob-new")
    inbox = request(
        amctl,
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


def test_legacy_broker_cursor_migrates_central_cursor_exactly_once(amctl):
    register(amctl, "alice", "instance-alice")
    initial = register(amctl, "bob", "instance-bob")
    first_id = send(amctl, "alice", "bob", "sent but not injected")
    advanced = request(
        amctl,
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
        amctl,
        "bob",
        "instance-bob",
        legacy_cursor=initial["cursor"],
    )
    replay = request(
        amctl,
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
        amctl,
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
        amctl,
        "bob",
        "instance-bob",
        legacy_cursor=initial["cursor"],
    )
    assert repeated["cursor"] == first_id


def test_ack_requires_current_instance_and_expected_cursor(amctl):
    register(amctl, "alice", "instance-alice")
    registration = register(amctl, "bob", "instance-bob")
    message_id = send(amctl, "alice", "bob", "ack me")

    stale_instance = request(
        amctl,
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
        amctl,
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
        amctl,
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
        amctl,
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


def test_ack_rejects_future_message_but_allows_pulled_group_after_membership_change(amctl):
    register(amctl, "alice", "instance-alice")
    registration = register(amctl, "bob", "instance-bob")
    register(amctl, "carol", "instance-carol")
    created = request(
        amctl,
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
    group_id = send(amctl, "alice", "review", "@bob please review")
    pulled = request(
        amctl,
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
        amctl,
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
        amctl,
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
        amctl,
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
        amctl,
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


def test_ack_allows_pulled_message_deleted_before_ack(amctl):
    register(amctl, "alice", "instance-alice")
    registration = register(amctl, "bob", "instance-bob")
    message_id = send(amctl, "alice", "bob", "delete after pull")
    pulled = request(
        amctl,
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
        amctl,
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
        amctl,
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


def test_inbox_rejects_replaced_registration_instance(amctl):
    register(amctl, "bob", "instance-old")
    register(amctl, "bob", "instance-new", force=True)

    stale = request(
        amctl,
        "GET",
        "/inbox",
        params={
            "project": "proj",
            "name": "bob",
            "instance": "instance-old",
        },
    )

    assert stale["code"] == "stale_instance"


def test_exact_message_requires_participation(amctl):
    for name in ("alice", "bob", "carol"):
        register(amctl, name, f"instance-{name}")
    message_id = send(amctl, "alice", "bob", "precise body")

    visible = request(
        amctl,
        "GET",
        "/message",
        params={"project": "proj", "name": "bob", "id": message_id},
    )
    hidden = request(
        amctl,
        "GET",
        "/message",
        params={"project": "proj", "name": "carol", "id": message_id},
    )

    assert visible["id"] == message_id
    assert visible["body"] == "precise body"
    assert visible["sender_identity"] == "alice@proj"
    assert "not visible" in hidden["error"]


def test_unregister_only_deletes_the_matching_instance(amctl):
    register(amctl, "bob", "instance-a")

    wrong = request(
        amctl,
        "POST",
        "/unregister",
        {"project": "proj", "name": "bob", "instance": "instance-b"},
    )
    rows = request(amctl, "GET", "/list")
    right = request(
        amctl,
        "POST",
        "/unregister",
        {"project": "proj", "name": "bob", "instance": "instance-a"},
    )

    assert wrong["deleted"] is False
    assert any(row["name"] == "bob" and row["project"] == "proj" for row in rows)
    assert right["deleted"] is True


def test_private_send_requires_full_identity_but_group_short_name_is_allowed(amctl):
    for name in ("alice", "bob"):
        register(amctl, name, f"instance-{name}")
    created = request(
        amctl,
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
            str(MEETING),
            "send",
            "alice@proj",
            "bob",
            "unsafe",
            "--host",
            amctl,
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    full_private = subprocess.run(
        [
            sys.executable,
            str(MEETING),
            "send",
            "alice@proj",
            "bob@proj",
            "safe",
            "--host",
            amctl,
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    short_group = subprocess.run(
        [
            sys.executable,
            str(MEETING),
            "send",
            "alice@proj",
            "review",
            "group",
            "--host",
            amctl,
        ],
        env=env,
        capture_output=True,
        text=True,
    )

    assert bare_private.returncode != 0
    assert "must use its full identity: bob@proj" in bare_private.stderr
    assert full_private.returncode == 0, full_private.stderr
    assert short_group.returncode == 0, short_group.stderr
