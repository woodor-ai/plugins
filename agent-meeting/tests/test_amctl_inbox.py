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


def register(base, name, instance):
    result = request(
        base,
        "POST",
        "/register",
        {
            "project": "proj",
            "name": name,
            "cwd": "/tmp/proj",
            "instance": instance,
            "role": "worker",
        },
    )
    assert not result.get("error"), result


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
        params={"project": "proj", "name": "bob", "since": 0},
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
