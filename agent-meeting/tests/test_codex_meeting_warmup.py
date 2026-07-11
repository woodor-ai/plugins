"""
Regression tests for codex-meeting.py auto-warm (agent-meeting v0.8.40).

codex-meeting.py parses argv only inside main() (guarded by
`if __name__ == "__main__"`), so — unlike codex-bridge.py — it is safe to
import directly via importlib and exercise its functions in isolation.

No real codex app-server / websockets dependency is needed: the app-server
JSON-RPC connection is faked at the `websockets.connect` seam.
"""

import asyncio
import importlib.util
import json
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CODEX_MEETING_PY = REPO / "agent-meeting" / "codex" / "codex-meeting.py"


def _load_codex_meeting():
    spec = importlib.util.spec_from_file_location("am_codex_meeting_warmup", CODEX_MEETING_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeWS:
    """Minimal fake of a websockets client connection: records every sent
    JSON-RPC request and replies with a canned response (id filled in)."""

    def __init__(self, responses: dict):
        self.sent = []
        self.responses = responses
        self._queue = asyncio.Queue()

    async def send(self, data):
        req = json.loads(data)
        self.sent.append(req)
        resp = self.responses.get(req["method"])
        if resp is not None:
            payload = dict(resp)
            payload["id"] = req["id"]
            await self._queue.put(json.dumps(payload))

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._queue.get()

    async def close(self):
        pass


def _fake_websockets_module(responses: dict):
    fake_ws = _FakeWS(responses)

    async def _connect(*args, **kwargs):
        return fake_ws

    return types.SimpleNamespace(connect=_connect), fake_ws


# ---------------------------------------------------------------------------
# _build_codex_launch_cmd — pure function
# ---------------------------------------------------------------------------

def test_build_codex_launch_cmd_resumes_warmed_thread():
    mod = _load_codex_meeting()
    cmd = mod._build_codex_launch_cmd("ws://127.0.0.1:8790", "thr-abc")
    assert cmd == ["codex", "resume", "thr-abc", "--remote", "ws://127.0.0.1:8790"]


def test_build_codex_launch_cmd_falls_back_without_thread_id():
    mod = _load_codex_meeting()
    cmd = mod._build_codex_launch_cmd("ws://127.0.0.1:8790", None)
    assert cmd == ["codex", "--remote", "ws://127.0.0.1:8790"]


# ---------------------------------------------------------------------------
# _warm_up_thread_async — faked app-server protocol
# ---------------------------------------------------------------------------

def test_warm_up_thread_async_happy_path_returns_thread_id():
    mod = _load_codex_meeting()
    responses = {
        "initialize": {"result": {}},
        "thread/start": {"result": {"thread": {"id": "thr-123"}}},
        "turn/start": {"result": {"turn": {"id": "turn-456"}}},
    }
    fake_mod, fake_ws = _fake_websockets_module(responses)
    mod.websockets = fake_mod

    thread_id = asyncio.run(mod._warm_up_thread_async("ws://127.0.0.1:8790", "/some/cwd"))

    assert thread_id == "thr-123"
    methods = [c["method"] for c in fake_ws.sent]
    assert methods == ["initialize", "thread/start", "turn/start"]

    thread_start_params = fake_ws.sent[1]["params"]
    assert thread_start_params["cwd"] == "/some/cwd"
    assert thread_start_params["sessionStartSource"] == "startup"

    turn_start_params = fake_ws.sent[2]["params"]
    assert turn_start_params["threadId"] == "thr-123"
    assert turn_start_params["input"] == [{"type": "text", "text": mod._WARM_PROMPT}]


def test_warm_up_thread_async_no_thread_id_returns_none_and_skips_turn():
    mod = _load_codex_meeting()
    responses = {
        "initialize": {"result": {}},
        "thread/start": {"result": {"thread": {}}},  # no "id"
    }
    fake_mod, fake_ws = _fake_websockets_module(responses)
    mod.websockets = fake_mod

    result = asyncio.run(mod._warm_up_thread_async("ws://127.0.0.1:8790", "/cwd"))

    assert result is None
    assert [c["method"] for c in fake_ws.sent] == ["initialize", "thread/start"], (
        "turn/start must never fire without a real thread id"
    )


def test_warm_up_thread_async_no_turn_id_returns_none():
    mod = _load_codex_meeting()
    responses = {
        "initialize": {"result": {}},
        "thread/start": {"result": {"thread": {"id": "thr-1"}}},
        "turn/start": {"result": {"turn": {}}},  # no "id"
    }
    fake_mod, _fake_ws = _fake_websockets_module(responses)
    mod.websockets = fake_mod

    result = asyncio.run(mod._warm_up_thread_async("ws://127.0.0.1:8790", "/cwd"))
    assert result is None


# ---------------------------------------------------------------------------
# _run_warm_up — sync wrapper: never raises, never blocks the launch
# ---------------------------------------------------------------------------

def test_run_warm_up_returns_none_when_websockets_unavailable():
    mod = _load_codex_meeting()
    mod.websockets = None
    assert mod._run_warm_up("ws://127.0.0.1:8790", "/cwd") is None


def test_run_warm_up_swallows_exceptions_from_async_helper(monkeypatch):
    mod = _load_codex_meeting()

    async def _boom(ws_addr, cwd):
        raise RuntimeError("app-server unreachable")

    monkeypatch.setattr(mod, "_warm_up_thread_async", _boom)
    # must not raise, and must return None so the caller falls back to a plain launch
    assert mod._run_warm_up("ws://127.0.0.1:8790", "/cwd") is None


def test_run_warm_up_happy_path_returns_thread_id():
    mod = _load_codex_meeting()
    responses = {
        "initialize": {"result": {}},
        "thread/start": {"result": {"thread": {"id": "thr-999"}}},
        "turn/start": {"result": {"turn": {"id": "turn-1"}}},
    }
    fake_mod, _fake_ws = _fake_websockets_module(responses)
    mod.websockets = fake_mod

    assert mod._run_warm_up("ws://127.0.0.1:8790", "/cwd") == "thr-999"
