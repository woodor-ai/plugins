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


def test_hub_discovery_prefers_current_msgd():
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


def test_am_msgd_host_environment_override_replaces_old_name(monkeypatch):
    from agent_meeting.commands import am_cli

    monkeypatch.setenv("AM_MSGD_HOST", "http://new-hub:9000/")
    monkeypatch.setenv("MEETING_HOST", "http://old-hub:8765")

    assert am_cli.discover_host() == "http://new-hub:9000"


def test_old_meeting_host_environment_name_is_ignored(monkeypatch):
    from agent_meeting.commands import am_cli

    monkeypatch.delenv("AM_MSGD_HOST", raising=False)
    monkeypatch.setenv("MEETING_HOST", "http://old-hub:8765")
    monkeypatch.setattr(am_cli, "_control_host", lambda: None)
    monkeypatch.setattr(am_cli, "_read_control_cache_fresh", lambda _ttl: None)
    monkeypatch.setattr(am_cli, "discover_msgd", lambda: [])

    assert am_cli.discover_host() is None


def test_public_msgd_discovery_falls_back_to_local_hub(monkeypatch):
    from agent_meeting.commands import am_cli

    monkeypatch.delenv("AM_MSGD_HOST", raising=False)
    monkeypatch.setattr(am_cli, "_control_host", lambda: None)
    monkeypatch.setattr(am_cli, "_discover_zeroconf", lambda: [])
    monkeypatch.setattr(am_cli, "_discover_msgd_raw", lambda: [])
    monkeypatch.setattr(am_cli, "_read_control_cache", lambda: None)
    monkeypatch.setattr(
        am_cli,
        "_healthy_local_control_url",
        lambda: "http://127.0.0.1:8765",
    )

    assert am_cli.discover_msgd() == [
        {
            "url": "http://127.0.0.1:8765",
            "host": "127.0.0.1",
            "ip": "127.0.0.1",
            "port": 8765,
            "version": "",
        }
    ]


def test_msgd_command_reads_health_for_explicit_host_version(
    monkeypatch,
    capsys,
):
    from agent_meeting.commands import am_cli

    url = "http://10.0.0.114:8765"
    monkeypatch.setenv("AM_MSGD_HOST", url)
    monkeypatch.setattr(
        am_cli,
        "discover_msgd",
        lambda: [am_cli._host_entry(url)],
    )
    monkeypatch.setattr(
        am_cli,
        "_http_once",
        lambda *_args: {
            "ok": True,
            "host": "OMI-MacDev.local",
            "version": "0.17.1",
        },
    )

    am_cli.cmd_msgd(SimpleNamespace(json=False))

    assert capsys.readouterr().out == (
        "am-msgd 1 (current)\n"
        "  host:    OMI-MacDev.local\n"
        "  ip:port: 10.0.0.114:8765\n"
        "  url:     http://10.0.0.114:8765\n"
        "  version: 0.17.1\n"
    )


def test_am_help_exposes_msgd_without_controls(monkeypatch, capsys):
    from agent_meeting.commands import am_cli

    monkeypatch.setattr("sys.argv", ["am", "--help"])
    with pytest.raises(SystemExit) as error:
        am_cli.main()

    assert error.value.code == 0
    output = capsys.readouterr().out
    assert "msgd" in output
    assert "controls" not in output


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
