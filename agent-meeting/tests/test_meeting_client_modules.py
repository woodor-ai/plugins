import importlib.util
import json
import socket
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PLUGIN_ROOT / "src"


@pytest.fixture(autouse=True)
def add_source_root(monkeypatch):
    monkeypatch.syspath_prepend(str(SRC_ROOT))


def test_project_cache_is_explicitly_scoped_to_runtime_home(tmp_path):
    from agent_meeting.messaging import project_identity

    first_home = tmp_path / "first"
    second_home = tmp_path / "second"
    root = str(tmp_path / "repo")

    project_identity.proj_cache_set(
        root,
        "first-project",
        meeting_home=str(first_home),
    )

    assert (
        project_identity.proj_cache_get(
            root,
            meeting_home=str(first_home),
        )
        == "first-project"
    )
    assert (
        project_identity.proj_cache_get(
            root,
            meeting_home=str(second_home),
        )
        is None
    )


def test_compatibility_facade_honors_monkeypatched_meeting_home(tmp_path):
    facade_path = PLUGIN_ROOT / "bin" / "meeting_common.py"
    spec = importlib.util.spec_from_file_location(
        "meeting_common_isolated",
        facade_path,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    module.MEETING_HOME = str(tmp_path)
    module.proj_cache_set("/repo", "scoped-project")

    assert module.proj_cache_get("/repo") == "scoped-project"
    assert module.derive_project("/repo") == "scoped-project"


def test_hub_discovery_prefers_current_control():
    from agent_meeting.clients.hub_discovery import discover_control

    class Result:
        returncode = 0
        stdout = json.dumps(
            [
                {"ip": "10.0.0.1", "port": 8765},
                {
                    "ip": "10.0.0.2",
                    "port": 9876,
                    "host": "hub.local",
                    "is_current": True,
                },
            ]
        )

    assert discover_control(lambda *args: Result()) == {
        "ip": "10.0.0.2",
        "port": 9876,
        "host": "hub.local",
        "ip_port": "10.0.0.2:9876",
        "base_url": "http://10.0.0.2:9876",
    }


def test_subscription_frame_reader_accepts_masked_extended_frame():
    from agent_meeting.clients.hub_subscription_client import read_frame

    payload = b"x" * 130
    mask = b"\x01\x02\x03\x04"
    masked = bytes(
        byte ^ mask[index % 4] for index, byte in enumerate(payload)
    )
    wire = (
        bytes([0x81, 0x80 | 126])
        + struct.pack("!H", len(payload))
        + mask
        + masked
    )

    left, right = socket.socketpair()
    try:
        left.sendall(wire)
        assert read_frame(right) == (0x1, payload)
    finally:
        left.close()
        right.close()


def test_http_client_encodes_json_and_bearer_token():
    from agent_meeting.clients.hub_http_client import request_once

    captured = {}

    class Response:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b'{"ok": true}'

    class Opener:
        def open(self, request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return Response()

    result = request_once(
        "POST",
        "http://127.0.0.1:8765",
        "/send",
        {"name": "agent"},
        {"body": "hello"},
        auth_token="secret",
        opener=Opener(),
    )

    request = captured["request"]
    assert result == {"ok": True}
    assert request.full_url.endswith("/send?name=agent")
    assert request.get_header("Authorization") == "Bearer secret"
    assert json.loads(request.data.decode("utf-8")) == {"body": "hello"}
    assert captured["timeout"] == 10


def test_group_command_preserves_explicit_group_and_member_projects(
    tmp_path,
    monkeypatch,
    capsys,
):
    from agent_meeting.commands import group_commands

    monkeypatch.chdir(tmp_path)
    requests = []
    args = SimpleNamespace(
        host=None,
        group_cmd="add",
        group_name="reviewers@shared-tools",
        member="alice@client-project",
    )

    group_commands.run_group_command(
        args,
        resolve_host=lambda _explicit: "http://hub:8765",
        derive_project=lambda _cwd: "local-project",
        request=lambda *call_args, **call_kwargs: (
            requests.append((call_args, call_kwargs)) or {}
        ),
    )

    assert requests == [
        (
            ("POST", "http://hub:8765", "/group/add"),
            {
                "body": {
                    "group_project": "shared-tools",
                    "group": "reviewers",
                    "member_project": "client-project",
                    "member": "alice",
                }
            },
        )
    ]
    assert capsys.readouterr().out.strip() == (
        "added alice@client-project to reviewers@shared-tools"
    )


def test_conversation_send_requires_canonical_peer_resolution(
    tmp_path,
    monkeypatch,
    capsys,
):
    from agent_meeting.commands import conversation_commands

    monkeypatch.chdir(tmp_path)
    requests = []
    events = []
    services = conversation_commands.ConversationCommandServices(
        resolve_host=lambda _explicit: "http://hub:8765",
        parse_self=lambda raw, _cwd: ("tools", raw),
        resolve_peer=lambda host, peer, **options: (
            requests.append(("resolve", host, peer, options))
            or ("review", peer)
        ),
        request=lambda *call_args, **call_kwargs: (
            requests.append(("request", call_args, call_kwargs))
            or {"msg_id": 42, "turn": "reviewer@review"}
        ),
        record_event=events.append,
    )
    args = SimpleNamespace(
        host=None,
        self_arg="author",
        peer="reviewer",
        body="please review",
        body_file=None,
        kind="请求",
        ask="reply",
    )

    conversation_commands.send(args, services)

    assert requests[0] == (
        "resolve",
        "http://hub:8765",
        "reviewer",
        {"require_full_session": True},
    )
    assert requests[1][1] == (
        "POST",
        "http://hub:8765",
        "/send",
    )
    assert requests[1][2]["body"]["self_project"] == "tools"
    assert requests[1][2]["body"]["peer_project"] == "review"
    assert events == ["send"]
    assert capsys.readouterr().out.strip() == (
        "sent: msg_id=42 turn->reviewer@review"
    )
