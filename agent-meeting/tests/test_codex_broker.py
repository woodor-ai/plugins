import asyncio
import importlib.util
import time
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlparse

import pytest


ROOT = Path(__file__).resolve().parents[1]
BROKER_PATH = ROOT / "codex" / "codex-broker.py"
LAUNCHER_PATH = ROOT / "codex" / "codex-meeting.py"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_session(module):
    return module.Session(
        launch_id="launch-1",
        name="plugins",
        project="tools",
        cwd="/tmp/project",
        control_url="http://127.0.0.1:8765",
        thread_id="thread-1",
        cursor=100,
    )


def test_normal_messages_are_coalesced_in_global_id_order():
    module = load(BROKER_PATH, "codex_broker_batch")
    broker = module.Broker()
    session = make_session(module)
    session.pending = OrderedDict(
        [
            (
                101,
                {
                    "id": 101,
                    "sender_identity": "alice@one",
                    "kind": "请求",
                    "group": None,
                },
            ),
            (
                104,
                {
                    "id": 104,
                    "sender_identity": "bob@two",
                    "kind": "请求",
                    "group": "review",
                },
            ),
        ]
    )

    selected, text = broker.build_injection(session)

    assert selected == [101, 104]
    assert text.startswith(
        "[meeting self=plugins@tools messages=2 ids=101,104]"
    )
    assert "[peer=alice@one msg_id=101]" in text
    assert "[group=review peer=bob@two msg_id=104]" in text


def test_control_message_is_not_batched_with_normal_messages():
    module = load(BROKER_PATH, "codex_broker_control")
    broker = module.Broker()
    session = make_session(module)
    session.pending = OrderedDict(
        [
            (
                101,
                {
                    "id": 101,
                    "sender_identity": "director@tools",
                    "kind": "control:restart",
                    "created_at": int(time.time()),
                },
            ),
            (
                102,
                {
                    "id": 102,
                    "sender_identity": "alice@one",
                    "kind": "请求",
                },
            ),
        ]
    )

    selected, text = broker.build_injection(session)

    assert selected == [101]
    assert text.startswith("[control:restart from peer=director@tools]")


def test_stale_control_is_consumed_without_injection():
    module = load(BROKER_PATH, "codex_broker_stale")
    broker = module.Broker()
    session = make_session(module)
    session.pending[101] = {
        "id": 101,
        "sender_identity": "director@tools",
        "kind": "control:clear",
        "created_at": int(time.time()) - module.CONTROL_STALE_S - 1,
    }

    selected, text = broker.build_injection(session)

    assert selected == [101]
    assert text == ""


def test_non_mentioned_group_message_is_consumed_without_injection():
    module = load(BROKER_PATH, "codex_broker_mentions")
    broker = module.Broker()
    session = make_session(module)
    session.pending[101] = {
        "id": 101,
        "sender_identity": "director@tools",
        "kind": "request",
        "group": "review@tools",
        "deliver": False,
    }

    selected, text = broker.build_injection(session)

    assert selected == [101]
    assert text == ""


def test_global_identity_and_sender_keep_canonical_star_suffix():
    module = load(BROKER_PATH, "codex_broker_global_identity")
    session = module.Session(
        launch_id="launch-global",
        name="global-agent",
        project="*",
        cwd="/tmp",
        control_url="http://127.0.0.1:8765",
        thread_id="thread-global",
        cursor=0,
    )
    broker = module.Broker()
    session.pending[1] = {
        "id": 1,
        "sender": "global-peer",
        "sender_project": "*",
        "sender_identity": "global-peer",
        "kind": "request",
    }

    _, text = broker.build_injection(session)

    assert session.identity == "global-agent@*"
    assert "[peer=global-peer@* msg_id=1]" in text


def test_consuming_a_page_fetches_the_next_page(monkeypatch):
    module = load(BROKER_PATH, "codex_broker_paging")
    broker = module.Broker()
    session = make_session(module)
    session.pending[101] = {
        "id": 101,
        "sender_identity": "director@tools",
        "kind": "request",
        "deliver": False,
    }
    calls = []

    async def fake_fetch(target):
        calls.append(target.cursor)
        target.pending[102] = {
            "id": 102,
            "sender_identity": "alice@tools",
            "kind": "request",
        }

    monkeypatch.setattr(broker, "fetch_inbox", fake_fetch)
    asyncio.run(broker.try_inject(session))

    assert session.cursor == 101
    assert calls == [101]
    assert list(session.pending) == [102]


def test_proxy_thread_mapping_updates_identity_lookup():
    module = load(BROKER_PATH, "codex_broker_mapping")
    broker = module.Broker()
    session = make_session(module)
    broker.sessions[session.launch_id] = session
    broker.thread_to_launch[session.thread_id] = session.launch_id

    asyncio.run(broker.update_thread(session, "thread-2"))
    result = asyncio.run(broker.identity_for_thread("thread-2"))

    assert result["identity"] == "plugins@tools"
    assert result["control_url"] == "http://127.0.0.1:8765"
    assert asyncio.run(broker.identity_for_thread("thread-1")) == {}


@pytest.mark.parametrize(
    "method",
    ["thread/start", "thread/resume", "thread/fork"],
)
def test_proxy_forces_session_cwd_on_thread_lifecycle_requests(method):
    module = load(BROKER_PATH, f"codex_broker_cwd_{method.replace('/', '_')}")
    broker = module.Broker()
    session = make_session(module)

    scoped = broker.scope_client_request(
        session,
        {
            "id": 7,
            "method": method,
            "params": {"cwd": "/wrong/first-launcher", "model": "gpt-5"},
        },
    )

    assert scoped["params"]["cwd"] == "/tmp/project"
    assert scoped["params"]["model"] == "gpt-5"


def test_proxy_leaves_non_thread_request_cwd_unchanged():
    module = load(BROKER_PATH, "codex_broker_turn_cwd")
    broker = module.Broker()
    session = make_session(module)
    request = {
        "id": 8,
        "method": "turn/start",
        "params": {"threadId": "thread-1", "cwd": "/intentional/override"},
    }

    assert broker.scope_client_request(session, request) is request


def test_launcher_always_connects_through_session_proxy():
    module = load(LAUNCHER_PATH, "codex_meeting_launcher")

    command = module.build_codex_launch_cmd("ws://127.0.0.1:49152")

    assert command == [
        "codex",
        "--remote",
        "ws://127.0.0.1:49152",
    ]


def test_session_proxy_url_is_a_codex_compatible_host_port():
    module = load(BROKER_PATH, "codex_broker_proxy_url")
    session = make_session(module)
    session.proxy_port = 49152

    parsed = urlparse(session.proxy_url)

    assert session.proxy_url == "ws://127.0.0.1:49152"
    assert parsed.path == ""
    assert parsed.query == ""


def test_launcher_refuses_to_restart_outdated_broker_with_active_sessions(
    monkeypatch,
):
    module = load(LAUNCHER_PATH, "codex_meeting_active_upgrade")
    monkeypatch.setattr(module, "installed_plugin_version", lambda: "0.13.1")
    monkeypatch.setattr(
        module,
        "broker_status",
        lambda: {"ok": True, "version": "0.13.0", "sessions": 2},
    )

    with pytest.raises(RuntimeError, match="exit the 2 active"):
        module.ensure_broker()


def test_launcher_restarts_idle_outdated_broker(monkeypatch):
    module = load(LAUNCHER_PATH, "codex_meeting_idle_upgrade")
    statuses = iter(
        [
            {"ok": True, "version": "0.13.0", "sessions": 0},
            {},
            {
                "ok": True,
                "version": "0.13.1",
                "sessions": 0,
                "appserver_url": "ws://127.0.0.1:8792",
            },
        ]
    )
    requests = []
    spawned = []

    class Process:
        @staticmethod
        def poll():
            return None

    def fake_request(method, path, body=None, params=None, timeout=45):
        requests.append((method, path))
        if method == "GET":
            return {
                "ok": True,
                "version": "0.13.1",
                "sessions": 0,
                "appserver_url": "ws://127.0.0.1:8792",
            }
        return {"ok": True}

    monkeypatch.setattr(module, "installed_plugin_version", lambda: "0.13.1")
    monkeypatch.setattr(module, "broker_status", lambda: next(statuses))
    monkeypatch.setattr(module, "broker_request", fake_request)
    monkeypatch.setattr(
        module,
        "spawn_detached",
        lambda command, log_path: spawned.append(command) or Process(),
    )

    module.ensure_broker()

    assert ("POST", "/shutdown") in requests
    assert spawned == [[module.venv_python(), str(module.BROKER_SCRIPT)]]
