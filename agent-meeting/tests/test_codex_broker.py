import asyncio
import importlib.util
import json
import subprocess
import time
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlparse

import pytest


ROOT = Path(__file__).resolve().parents[1]
BROKER_PATH = ROOT / "codex" / "am_codexd.py"
LAUNCHER_PATH = ROOT / "codex" / "codex-meeting.py"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("path", [BROKER_PATH, LAUNCHER_PATH])
def test_codex_components_read_only_the_codex_manifest(path, tmp_path):
    module = load(path, f"codex_manifest_{path.stem}")
    plugin_root = tmp_path / "agent-meeting"
    for manifest_dir, version in (
        (".claude-plugin", "9.9.9"),
        (".codex-plugin", "0.15.0"),
    ):
        directory = plugin_root / manifest_dir
        directory.mkdir(parents=True)
        (directory / "plugin.json").write_text(
            json.dumps({"name": "agent-meeting", "version": version}),
            encoding="utf-8",
        )

    module.PLUGIN_ROOT = plugin_root

    assert module.installed_plugin_version() == "0.15.0"


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
    assert text == (
        "📬 New Message from alice@one [via woodor:agent-meeting]\n"
        "  Message ID: 101\n"
        "📬 New Message from bob@two in group review [via woodor:agent-meeting]\n"
        "  Message ID: 104\n"
        "Agent-meeting recipient: plugins@tools"
    )


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
    assert "📬 New Message from global-peer@* [via woodor:agent-meeting]" in text
    assert "Agent-meeting recipient: global-agent@*" in text


def test_global_identity_cannot_start_twice_on_same_broker():
    module = load(BROKER_PATH, "codex_broker_global_duplicate")
    broker = module.Broker()
    existing = module.Session(
        launch_id="launch-existing",
        name="global-agent",
        project="*",
        cwd="/tmp",
        control_url="http://127.0.0.1:8765",
        thread_id="thread-existing",
        cursor=0,
    )
    broker.sessions[existing.launch_id] = existing

    with pytest.raises(ValueError, match=r"global-agent@\*"):
        asyncio.run(
            broker.start_session(
                {
                    "launch_id": "launch-new",
                    "name": "global-agent",
                    "project": "*",
                    "cwd": "/tmp",
                    "control_url": "http://127.0.0.1:8765",
                }
            )
        )


def test_shutdown_refuses_active_sessions_and_closes_the_start_gate_atomically():
    module = load(BROKER_PATH, "am_codexd_atomic_shutdown")
    broker = module.Broker()

    async def run():
        session = make_session(module)
        broker.sessions[session.launch_id] = session
        with pytest.raises(ValueError, match="1 mycodex session"):
            await broker.request_shutdown()
        assert broker.accepting_sessions is True

        broker.sessions.clear()
        assert await broker.request_shutdown() == {"ok": True}
        assert broker.accepting_sessions is False
        with pytest.raises(ValueError, match="shutting down"):
            await broker.start_session({})

    asyncio.run(run())


def test_register_and_fetch_use_only_central_cursor(monkeypatch):
    module = load(BROKER_PATH, "codex_broker_central_cursor")
    broker = module.Broker()
    session = module.Session(
        launch_id="launch-central",
        name="worker",
        project="proj",
        cwd="/tmp",
        control_url="http://127.0.0.1:8765",
        thread_id=None,
        cursor=None,
    )
    requests = []

    def fake_http(method, base_url, path, body=None, params=None, timeout=20):
        requests.append((method, path, body, params, timeout))
        if path == "/register":
            return {"ok": True, "cursor": 50}
        if path == "/inbox":
            return {
                "cursor": 50,
                "messages": [
                    {
                        "id": 51,
                        "sender_identity": "director@proj",
                        "kind": "request",
                    }
                ],
            }
        raise AssertionError(path)

    monkeypatch.setattr(module, "http_json", fake_http)

    async def run():
        await broker.register_central(session, timeout=2)
        await broker.fetch_inbox(session)

    asyncio.run(run())

    assert session.cursor == 50
    assert list(session.pending) == [51]
    inbox_request = next(row for row in requests if row[1] == "/inbox")
    assert inbox_request[3] == {
        "project": "proj",
        "name": "worker",
        "instance": "launch-central",
        "limit": 500,
    }
    assert not hasattr(broker, "cursors")


def test_central_outage_does_not_block_local_session_start(monkeypatch):
    module = load(BROKER_PATH, "codex_broker_central_outage")
    broker = module.Broker()

    async def fake_start_proxy(session):
        session.proxy_port = 49152

    async def unavailable(*_args, **_kwargs):
        raise OSError("connection refused")

    async def background_retry(_session):
        await asyncio.Event().wait()

    monkeypatch.setattr(broker, "start_session_proxy", fake_start_proxy)
    monkeypatch.setattr(broker, "register_central", unavailable)
    monkeypatch.setattr(broker, "subscribe", background_retry)

    session = asyncio.run(
        broker.start_session(
            {
                "launch_id": "launch-offline",
                "name": "worker",
                "project": "proj",
                "cwd": "/tmp",
                "control_url": "http://127.0.0.1:8765",
            }
        )
    )

    assert session["proxy_url"] == "ws://127.0.0.1:49152"
    assert broker.sessions["launch-offline"].central_registered is False
    assert "connection refused" in broker.sessions["launch-offline"].central_error


def test_register_offers_read_only_legacy_success_cursor(tmp_path, monkeypatch):
    module = load(BROKER_PATH, "codex_broker_legacy_cursor")
    legacy_state = tmp_path / "broker-state.json"
    legacy_state.write_text(
        json.dumps({"cursors": {"plugins@tools": 87}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "LEGACY_STATE_FILE", legacy_state)
    broker = module.Broker()
    session = make_session(module)
    session.cursor = None
    payloads = []

    async def fake_to_thread(_function, *args, **_kwargs):
        payloads.append(args[3])
        return {"ok": True, "cursor": 87}

    monkeypatch.setattr(module.asyncio, "to_thread", fake_to_thread)

    asyncio.run(broker.register_central(session))

    assert payloads[0]["legacy_cursor"] == 87
    assert session.cursor == 87
    assert broker.legacy_cursors == {"plugins@tools": 87}


def test_fetch_and_ack_are_serialized_per_session(monkeypatch):
    module = load(BROKER_PATH, "codex_broker_delivery_serialization")
    broker = module.Broker()
    session = make_session(module)
    fetch_started = asyncio.Event()
    release_fetch = asyncio.Event()
    events = []

    async def fake_to_thread(_function, *args, **_kwargs):
        path = args[2]
        if path == "/inbox":
            events.append("fetch-start")
            fetch_started.set()
            await release_fetch.wait()
            events.append("fetch-return")
            return {
                "cursor": 100,
                "messages": [{"id": 101, "kind": "request"}],
            }
        if path == "/ack":
            events.append("ack")
            return {"ok": True, "cursor": 101}
        raise AssertionError(path)

    async def scenario():
        monkeypatch.setattr(module.asyncio, "to_thread", fake_to_thread)
        fetch_task = asyncio.create_task(broker.fetch_inbox(session))
        await fetch_started.wait()
        ack_task = asyncio.create_task(broker.acknowledge(session, [101]))
        await asyncio.sleep(0)
        assert events == ["fetch-start"]
        release_fetch.set()
        await asyncio.gather(fetch_task, ack_task)

    asyncio.run(scenario())

    assert events == ["fetch-start", "fetch-return", "ack"]
    assert session.cursor == 101


def test_stale_inbox_cursor_cannot_roll_back_session(monkeypatch):
    module = load(BROKER_PATH, "codex_broker_stale_inbox")
    broker = module.Broker()
    session = make_session(module)
    session.cursor = 101

    async def fake_to_thread(_function, *_args, **_kwargs):
        return {
            "cursor": 100,
            "messages": [{"id": 101, "kind": "request"}],
        }

    monkeypatch.setattr(module.asyncio, "to_thread", fake_to_thread)

    with pytest.raises(RuntimeError, match="moved backwards"):
        asyncio.run(broker.fetch_inbox(session))

    assert session.cursor == 101
    assert session.pending == {}


def test_unregister_runs_when_registration_completion_was_not_observed(monkeypatch):
    module = load(BROKER_PATH, "codex_broker_unregister_race")
    broker = module.Broker()
    session = make_session(module)
    session.central_registered = False
    calls = []

    async def fake_to_thread(function, *args, **kwargs):
        calls.append((function, args, kwargs))
        return {"ok": True, "deleted": True}

    monkeypatch.setattr(module.asyncio, "to_thread", fake_to_thread)

    asyncio.run(broker.unregister_central(session))

    assert len(calls) == 1
    _function, args, _kwargs = calls[0]
    assert args[0:3] == ("POST", session.control_url, "/unregister")
    assert args[3]["instance"] == session.launch_id


def test_stop_waits_for_inflight_registration_before_unregistering(monkeypatch):
    module = load(BROKER_PATH, "codex_broker_register_stop_race")
    broker = module.Broker()
    session = make_session(module)
    broker.sessions[session.launch_id] = session
    events = []

    async def scenario():
        register_started = asyncio.Event()
        release_register = asyncio.Event()

        async def fake_to_thread(_function, *args, **_kwargs):
            path = args[2]
            if path == "/register":
                register_started.set()
                await release_register.wait()
                events.append("register")
                return {"ok": True, "cursor": 100}
            if path == "/unregister":
                events.append("unregister")
                return {"ok": True, "deleted": True}
            raise AssertionError(path)

        monkeypatch.setattr(module.asyncio, "to_thread", fake_to_thread)
        session.subscription_task = asyncio.create_task(
            broker.register_central(session)
        )
        await register_started.wait()
        stop_task = asyncio.create_task(broker.stop_session(session.launch_id))
        await asyncio.sleep(0)
        assert not stop_task.done()
        release_register.set()
        await stop_task

    asyncio.run(scenario())

    assert events == ["register", "unregister"]


def test_app_call_opts_into_experimental_api(monkeypatch):
    module = load(BROKER_PATH, "codex_broker_experimental_api")
    broker = module.Broker()
    broker.appserver.port = 8792
    sent = []

    class FakeWebSocket:
        async def send(self, raw):
            sent.append(json.loads(raw))

        async def recv(self):
            request = sent[-1]
            return json.dumps({"id": request["id"], "result": {}})

    class FakeConnection:
        async def __aenter__(self):
            return FakeWebSocket()

        async def __aexit__(self, *_args):
            return False

    class FakeWebSockets:
        @staticmethod
        def connect(*_args, **_kwargs):
            return FakeConnection()

    monkeypatch.setattr(module, "websockets", FakeWebSockets)

    asyncio.run(
        broker.app_call(
            "thread/read",
            {"threadId": "thread-1", "includeTurns": False},
        )
    )

    assert sent[0]["method"] == "initialize"
    assert sent[0]["params"]["capabilities"] == {"experimentalApi": True}


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
    acknowledgements = []

    async def fake_ack(target, message_ids):
        acknowledgements.append((target.cursor, list(message_ids)))
        target.cursor = max(message_ids)

    async def fake_fetch(target):
        calls.append(target.cursor)
        target.pending[102] = {
            "id": 102,
            "sender_identity": "alice@tools",
            "kind": "request",
        }

    monkeypatch.setattr(broker, "acknowledge", fake_ack)
    monkeypatch.setattr(broker, "fetch_inbox", fake_fetch)
    asyncio.run(broker.try_inject(session))

    assert session.cursor == 101
    assert acknowledgements == [(100, [101])]
    assert calls == [101]
    assert list(session.pending) == [102]


def test_broker_injected_turn_carries_runtime_context(monkeypatch):
    module = load(BROKER_PATH, "codex_broker_injected_turn_context")
    broker = module.Broker()
    session = make_session(module)
    session.pending[101] = {
        "id": 101,
        "sender_identity": "alice@tools",
        "kind": "request",
    }
    calls = []
    acknowledgements = []

    async def fake_app_call(method, params=None, timeout=30):
        calls.append((method, params, timeout))
        if method == "thread/read":
            return {"thread": {"status": {"type": "idle"}}}
        return {}

    async def fake_fetch(_target):
        return None

    async def fake_ack(target, message_ids):
        acknowledgements.append((target.cursor, list(message_ids)))
        target.cursor = max(message_ids)

    monkeypatch.setattr(broker, "app_call", fake_app_call)
    monkeypatch.setattr(broker, "acknowledge", fake_ack)
    monkeypatch.setattr(broker, "fetch_inbox", fake_fetch)

    asyncio.run(broker.try_inject(session))

    turn_params = next(params for method, params, _timeout in calls if method == "turn/start")
    runtime = turn_params["additionalContext"]["agent-meeting-runtime"]
    assert runtime["kind"] == "application"
    assert "Agent-meeting recipient: plugins@tools" in runtime["value"]
    assert acknowledgements == [(100, [101])]


def test_failed_injection_does_not_ack(monkeypatch):
    module = load(BROKER_PATH, "codex_broker_failed_injection")
    broker = module.Broker()
    session = make_session(module)
    session.pending[101] = {
        "id": 101,
        "sender_identity": "alice@tools",
        "kind": "request",
    }
    acknowledgements = []

    async def fake_app_call(method, params=None, timeout=30):
        if method == "thread/read":
            return {"thread": {"status": {"type": "idle"}}}
        raise RuntimeError("experimental request rejected")

    async def fake_ack(_target, message_ids):
        acknowledgements.append(list(message_ids))

    monkeypatch.setattr(broker, "app_call", fake_app_call)
    monkeypatch.setattr(broker, "acknowledge", fake_ack)

    asyncio.run(broker.try_inject(session))

    assert acknowledgements == []
    assert list(session.pending) == [101]
    assert session.awaiting_ack is None


def test_silent_consumption_acks_without_starting_turn(monkeypatch):
    module = load(BROKER_PATH, "codex_broker_silent_ack")
    broker = module.Broker()
    session = make_session(module)
    session.pending[101] = {
        "id": 101,
        "sender_identity": "alice@tools",
        "kind": "request",
        "deliver": False,
    }
    acknowledgements = []

    async def forbidden_app_call(*_args, **_kwargs):
        raise AssertionError("silent consumption must not call the app-server")

    async def fake_ack(target, message_ids):
        acknowledgements.append((target.cursor, list(message_ids)))
        target.cursor = max(message_ids)

    async def fake_fetch(_target):
        return None

    monkeypatch.setattr(broker, "app_call", forbidden_app_call)
    monkeypatch.setattr(broker, "acknowledge", fake_ack)
    monkeypatch.setattr(broker, "fetch_inbox", fake_fetch)

    asyncio.run(broker.try_inject(session))

    assert acknowledgements == [(100, [101])]
    assert session.pending == {}


def test_lost_ack_response_is_reconciled_without_reinjecting(monkeypatch):
    module = load(BROKER_PATH, "codex_broker_lost_ack_response")
    broker = module.Broker()
    session = make_session(module)
    session.pending[101] = {
        "id": 101,
        "sender_identity": "alice@tools",
        "kind": "request",
    }
    session.awaiting_ack = [101]
    requests = []

    def fake_http(method, base_url, path, body=None, params=None, timeout=20):
        requests.append((method, path, body))
        if path == "/ack":
            return {
                "error": "cursor changed from 100 to 101",
                "code": "cursor_conflict",
                "cursor": 101,
            }
        if path == "/inbox":
            return {"cursor": 101, "messages": []}
        raise AssertionError(path)

    async def forbidden_app_call(*_args, **_kwargs):
        raise AssertionError("a committed batch must not be injected again")

    monkeypatch.setattr(module, "http_json", fake_http)
    monkeypatch.setattr(broker, "app_call", forbidden_app_call)

    asyncio.run(broker.try_inject(session))

    assert session.cursor == 101
    assert session.awaiting_ack is None
    assert session.pending == {}
    assert [path for _method, path, _body in requests] == ["/ack", "/inbox"]


def test_reconnect_reconciles_ack_committed_before_response(monkeypatch):
    module = load(BROKER_PATH, "codex_broker_reconnect_ack_reconcile")
    broker = module.Broker()
    session = make_session(module)
    session.pending[101] = {
        "id": 101,
        "sender_identity": "alice@tools",
        "kind": "request",
    }
    session.awaiting_ack = [101]

    def fake_http(method, base_url, path, body=None, params=None, timeout=20):
        assert path == "/register"
        return {"ok": True, "cursor": 101}

    monkeypatch.setattr(module, "http_json", fake_http)

    asyncio.run(broker.register_central(session))

    assert session.cursor == 101
    assert session.awaiting_ack is None
    assert session.pending == {}


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
    assert (
        "Agent-meeting recipient: plugins@tools"
        in scoped["params"]["developerInstructions"]
    )
    assert (
        "Agent-meeting control: http://127.0.0.1:8765"
        in scoped["params"]["developerInstructions"]
    )
    assert "--host http://127.0.0.1:8765" in scoped["params"]["developerInstructions"]
    assert "place the `--host` option immediately after `group`" in (
        scoped["params"]["developerInstructions"]
    )


def test_proxy_preserves_existing_thread_developer_instructions():
    module = load(BROKER_PATH, "codex_broker_developer_instructions")
    broker = module.Broker()
    session = make_session(module)

    scoped = broker.scope_client_request(
        session,
        {
            "id": 7,
            "method": "thread/start",
            "params": {"developerInstructions": "Keep the existing rule."},
        },
    )

    instructions = scoped["params"]["developerInstructions"]
    assert instructions.startswith("Keep the existing rule.\n\n")
    assert "Agent-meeting recipient: plugins@tools" in instructions


def test_proxy_adds_turn_runtime_context_without_changing_cwd():
    module = load(BROKER_PATH, "codex_broker_turn_context")
    broker = module.Broker()
    session = make_session(module)
    request = {
        "id": 8,
        "method": "turn/start",
        "params": {
            "threadId": "thread-1",
            "cwd": "/intentional/override",
            "additionalContext": {
                "existing": {"kind": "application", "value": "keep me"}
            },
        },
    }

    scoped = broker.scope_client_request(session, request)

    assert scoped["params"]["cwd"] == "/intentional/override"
    assert scoped["params"]["additionalContext"]["existing"]["value"] == "keep me"
    runtime = scoped["params"]["additionalContext"]["agent-meeting-runtime"]
    assert runtime["kind"] == "application"
    assert "Agent-meeting recipient: plugins@tools" in runtime["value"]


def test_proxy_leaves_unrelated_request_unchanged():
    module = load(BROKER_PATH, "codex_broker_unrelated_request")
    broker = module.Broker()
    session = make_session(module)
    request = {"id": 9, "method": "thread/read", "params": {"threadId": "thread-1"}}

    assert broker.scope_client_request(session, request) is request


def test_launcher_always_connects_through_session_proxy():
    module = load(LAUNCHER_PATH, "codex_meeting_launcher")

    command = module.build_codex_launch_cmd("ws://127.0.0.1:49152")

    assert command == [
        "codex",
        "--remote",
        "ws://127.0.0.1:49152",
    ]


def test_no_codex_launcher_exits_when_background_registration_is_rejected(
    monkeypatch,
):
    module = load(LAUNCHER_PATH, "codex_meeting_no_codex_fatal")
    launcher = module.Launcher(
        "alice",
        "proj",
        "http://10.0.0.114:8765",
    )
    statuses = iter(
        [
            {"active": True},
            {
                "active": False,
                "central_error": "alice@proj is already registered",
            },
        ]
    )

    class NeverSignalled:
        @staticmethod
        def wait(_timeout):
            return False

    monkeypatch.setattr(
        module,
        "broker_request",
        lambda *_args, **_kwargs: next(statuses),
    )

    with pytest.raises(RuntimeError, match="already registered"):
        launcher.hold(NeverSignalled())


def test_launcher_does_not_export_meeting_identity_or_host(monkeypatch):
    module = load(LAUNCHER_PATH, "codex_meeting_no_environment")
    launcher = module.Launcher(
        "alice",
        "proj",
        "http://10.0.0.114:8765",
    )
    launcher.session = {
        "identity": "alice@proj",
        "proxy_url": "ws://127.0.0.1:49152",
    }
    observed = {}

    class FakePinner:
        def __init__(self, _title):
            pass

        def start(self):
            pass

        def stop(self):
            pass

    def fake_run(command):
        observed["command"] = command

    monkeypatch.setattr(module, "TitlePinner", FakePinner)
    monkeypatch.setattr(module.subprocess, "run", fake_run)

    launcher.run_codex()

    assert observed["command"] == [
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


def test_launcher_activates_daemon_before_requesting_a_session(monkeypatch):
    module = load(LAUNCHER_PATH, "codex_meeting_daemon_update")
    commands = []
    requests = []

    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda command, **_kwargs: (
            commands.append(command)
            or subprocess.CompletedProcess(command, 0, "am-codexd is already up to date\n", "")
        ),
    )
    monkeypatch.setattr(
        module,
        "broker_request",
        lambda method, path, **_kwargs: (
            requests.append((method, path))
            or {
                "ok": True,
                "version": module.installed_plugin_version(),
                "sessions": 0,
            }
        ),
    )

    module.ensure_daemon()

    assert commands == [
        [module.venv_python(), str(module.DAEMON_COMMAND), "update"]
    ]
    assert requests == [("GET", "/health")]


def test_packaged_launcher_executes_windows_console_entrypoint_directly(
    monkeypatch,
    tmp_path,
):
    module = load(LAUNCHER_PATH, "codex_meeting_packaged_daemon_update")
    module.__package__ = "mycodex.launcher"
    module.DAEMON_COMMAND = tmp_path / "am-codexd.exe"
    module.DAEMON_COMMAND.write_bytes(b"launcher")
    commands = []
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda command, **_kwargs: (
            commands.append(command)
            or subprocess.CompletedProcess(command, 0, "", "")
        ),
    )
    monkeypatch.setattr(
        module,
        "broker_request",
        lambda *_args, **_kwargs: {
            "ok": True,
            "version": module.installed_plugin_version(),
        },
    )

    module.ensure_daemon()

    assert commands == [[str(module.DAEMON_COMMAND), "update"]]


def test_launcher_surfaces_daemon_update_failure(monkeypatch):
    module = load(LAUNCHER_PATH, "codex_meeting_daemon_update_failure")

    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            1,
            "",
            "ERROR: cannot update while 2 mycodex sessions are active\n",
        ),
    )

    with pytest.raises(RuntimeError, match="2 mycodex sessions"):
        module.ensure_daemon()


def test_launcher_rejects_a_healthy_daemon_from_the_wrong_version(monkeypatch):
    module = load(LAUNCHER_PATH, "codex_meeting_daemon_version_mismatch")

    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "", ""),
    )
    monkeypatch.setattr(
        module,
        "broker_request",
        lambda *_args, **_kwargs: {
            "ok": True,
            "version": "0.13.9",
            "sessions": 0,
        },
    )
    monkeypatch.setattr(module, "installed_plugin_version", lambda: "0.14.0")

    with pytest.raises(RuntimeError, match="running 0.13.9, installed 0.14.0"):
        module.ensure_daemon()
