"""Opt-in live integration test for the shared official Codex app-server."""

import asyncio
import json
import os
import shutil
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
BROKER = ROOT / "codex" / "am_codexd.py"


def free_ports(count):
    sockets = []
    ports = []
    try:
        for _ in range(count):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
            ports.append(sock.getsockname()[1])
    finally:
        for sock in sockets:
            sock.close()
    return ports


def request(base, method, path, body=None, params=None, timeout=10):
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
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        payload = json.loads(exc.read().decode("utf-8"))
        payload["_status"] = exc.code
        return payload


def wait_for_health(base, process, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return None
        try:
            result = request(base, "GET", "/health", timeout=1)
            if result.get("ok"):
                return result
        except OSError:
            pass
        time.sleep(0.1)
    return None


def terminate(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


async def proxy_start_thread(proxy_url, cwd):
    websockets = pytest.importorskip("websockets")
    async with websockets.connect(proxy_url, max_size=None) as websocket:
        next_id = 1

        async def call(method, params=None):
            nonlocal next_id
            request_id = next_id
            next_id += 1
            payload = {"id": request_id, "method": method}
            if params is not None:
                payload["params"] = params
            await websocket.send(json.dumps(payload))
            while True:
                message = json.loads(await asyncio.wait_for(websocket.recv(), 15))
                if message.get("id") == request_id:
                    assert "error" not in message, message
                    return message.get("result") or {}

        await call(
            "initialize",
            {
                "clientInfo": {
                    "name": "agent_meeting_integration_test",
                    "title": "Agent Meeting Integration Test",
                    "version": "1",
                }
            },
        )
        await websocket.send(json.dumps({"method": "initialized", "params": {}}))
        result = await call(
            "thread/start",
            {
                "cwd": str(cwd),
                "sessionStartSource": "startup",
                "serviceName": "agent-meeting:integration-proxy",
            },
        )
        return result.get("thread") or {}


async def verify_experimental_context_gate(appserver_url):
    websockets = pytest.importorskip("websockets")

    async def call_with_capability(enabled):
        async with websockets.connect(appserver_url, max_size=None) as websocket:
            request_id = 1

            async def call(method, params=None):
                nonlocal request_id
                current_id = request_id
                request_id += 1
                payload = {"id": current_id, "method": method}
                if params is not None:
                    payload["params"] = params
                await websocket.send(json.dumps(payload))
                while True:
                    response = json.loads(
                        await asyncio.wait_for(websocket.recv(), 15)
                    )
                    if response.get("id") == current_id:
                        return response

            initialize = {
                "clientInfo": {
                    "name": "agent_meeting_capability_test",
                    "title": "Agent Meeting Capability Test",
                    "version": "1",
                }
            }
            if enabled:
                initialize["capabilities"] = {"experimentalApi": True}
            initialized = await call("initialize", initialize)
            assert "error" not in initialized, initialized
            await websocket.send(
                json.dumps({"method": "initialized", "params": {}})
            )
            return await call(
                "turn/start",
                {
                    "threadId": "agent-meeting-missing-thread",
                    "input": [{"type": "text", "text": "capability probe"}],
                    "additionalContext": {
                        "agent-meeting-runtime": {
                            "kind": "application",
                            "value": "probe",
                        }
                    },
                },
            )

    rejected = await call_with_capability(False)
    accepted_by_gate = await call_with_capability(True)
    rejected_text = json.dumps(rejected, ensure_ascii=False)
    accepted_text = json.dumps(accepted_by_gate, ensure_ascii=False)

    assert "experimentalApi" in rejected_text
    assert "experimentalApi" not in accepted_text


@pytest.mark.skipif(
    os.environ.get("RUN_CODEX_BROKER_INTEGRATION") != "1",
    reason="set RUN_CODEX_BROKER_INTEGRATION=1 for the live Codex integration",
)
@pytest.mark.skipif(shutil.which("codex") is None, reason="codex is not installed")
def test_two_sessions_share_one_appserver_and_stop_independently(tmp_path):
    pytest.importorskip("websockets")
    amctl_port, api_port, app_port = free_ports(3)
    meeting_home = tmp_path / "meeting-home"
    db_dir = meeting_home / "db"
    db_dir.mkdir(parents=True)
    (db_dir / "rooms.db").touch()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()

    env = os.environ.copy()
    env.update(
        {
            "MEETING_HOME": str(meeting_home),
            "CODEX_HOME": str(codex_home),
            "MEETING_BROKER_API_PORT": str(api_port),
            "MEETING_BROKER_APP_PORT_FIRST": str(app_port),
            "MEETING_BROKER_APP_PORT_LAST": str(app_port),
        }
    )
    amctl_process = subprocess.Popen(
        [
            sys.executable,
            str(AMCTL),
            "--bind",
            "127.0.0.1",
            "--port",
            str(amctl_port),
            "--no-mdns",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    broker_process = None
    try:
        amctl_base = f"http://127.0.0.1:{amctl_port}"
        assert wait_for_health(amctl_base, amctl_process, timeout=10)
        broker_process = subprocess.Popen(
            [sys.executable, str(BROKER)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        broker_base = f"http://127.0.0.1:{api_port}"
        health = wait_for_health(broker_base, broker_process)
        if not health:
            _, stderr = broker_process.communicate(timeout=5)
            pytest.fail(f"broker did not start: {stderr}")
        assert health["appserver_url"] == f"ws://127.0.0.1:{app_port}"
        asyncio.run(verify_experimental_context_gate(health["appserver_url"]))

        sessions = []
        for launch_id, name in (("launch-a", "alice"), ("launch-b", "bob")):
            session = request(
                broker_base,
                "POST",
                "/session/start",
                {
                    "launch_id": launch_id,
                    "name": name,
                    "project": "proj",
                    "cwd": str(tmp_path),
                    "control_url": amctl_base,
                },
                timeout=40,
            )
            assert not session.get("error"), session
            assert session["thread_id"] is None
            parsed_proxy = urllib.parse.urlparse(session["proxy_url"])
            assert parsed_proxy.path == ""
            assert parsed_proxy.query == ""
            sessions.append(session)

        shared = request(broker_base, "GET", "/health")
        assert shared["sessions"] == 2
        assert shared["appserver_url"] == health["appserver_url"]

        wrong_cwd = tmp_path / "first-launcher-cwd"
        wrong_cwd.mkdir()
        moved = asyncio.run(
            proxy_start_thread(sessions[1]["proxy_url"], wrong_cwd)
        )
        moved_thread = moved["id"]
        assert moved["cwd"] == str(tmp_path)
        active_session = request(
            broker_base,
            "GET",
            "/session",
            params={"launch_id": "launch-b"},
        )
        assert active_session["thread_id"] == moved_thread
        mapped = request(
            broker_base,
            "GET",
            "/identity",
            params={"thread_id": moved_thread},
        )
        assert mapped["identity"] == "bob@proj"

        stopped = request(
            broker_base,
            "POST",
            "/session/stop",
            {"launch_id": "launch-a"},
        )
        assert stopped["stopped"] is True
        assert request(broker_base, "GET", "/health")["sessions"] == 1
        assert request(
            broker_base,
            "GET",
            "/session",
            params={"launch_id": "launch-b"},
        )["active"] is True
    finally:
        if broker_process is not None and broker_process.poll() is None:
            try:
                request(
                    f"http://127.0.0.1:{api_port}",
                    "POST",
                    "/shutdown",
                    timeout=3,
                )
                broker_process.wait(timeout=12)
            except Exception:
                terminate(broker_process)
        terminate(amctl_process)
