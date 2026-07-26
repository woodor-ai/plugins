import asyncio
import importlib.util
import time
from collections import OrderedDict
from pathlib import Path


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


def test_launcher_always_connects_through_session_proxy():
    module = load(LAUNCHER_PATH, "codex_meeting_launcher")

    command = module.build_codex_launch_cmd(
        "ws://127.0.0.1:8789/session/launch-1",
        "thread-1",
    )

    assert command == [
        "codex",
        "resume",
        "thread-1",
        "--remote",
        "ws://127.0.0.1:8789/session/launch-1",
    ]
