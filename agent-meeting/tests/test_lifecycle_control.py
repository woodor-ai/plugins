import asyncio
import importlib.util
import json
import os
import plistlib
import sys
import threading
import time
from types import SimpleNamespace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MYCODEX_SOURCE = ROOT.parent / "mycodex" / "src"
BROKER_PATH = (
    MYCODEX_SOURCE
    / "mycodex"
    / "codex_session_broker"
    / "broker_process.py"
)
sys.path.insert(0, str(MYCODEX_SOURCE))
sys.path.insert(0, str(ROOT / "src"))


def _load_broker(name):
    spec = importlib.util.spec_from_file_location(name, BROKER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_codex_ingress_pause_uses_matching_token():
    module = _load_broker("codex_broker_lifecycle_pause")
    broker = module.Broker()
    session = module.Session(
        launch_id="launch-1",
        name="worker",
        project="tools",
        cwd="/tmp/project",
        control_url="http://127.0.0.1:8765",
        thread_id="thread-1",
        cursor=0,
    )
    broker.sessions[session.launch_id] = session

    paused = asyncio.run(broker.pause_ingress(session.launch_id))
    assert session.ingress_paused is True
    assert paused["pause_token"] in session.ingress_pause_tokens

    resumed = asyncio.run(
        broker.resume_ingress(session.launch_id, paused["pause_token"])
    )
    assert resumed == {"ok": True, "ingress_paused": False}
    assert session.ingress_paused is False


def test_codex_sessions_status_reads_runtime_state():
    module = _load_broker("codex_broker_lifecycle_runtime_state")
    broker = module.Broker()
    session = module.Session(
        launch_id="launch-runtime",
        name="worker",
        project="tools",
        cwd="/tmp/project",
        control_url="http://127.0.0.1:8765",
        thread_id="thread-runtime",
        cursor=0,
    )
    session.proxy_port = 9999
    session.token_usage = {
        "last": {"totalTokens": 64000},
        "modelContextWindow": 128000,
    }
    broker.sessions[session.launch_id] = session

    async def app_call(method, params, timeout):
        assert method == "thread/read"
        assert params["threadId"] == "thread-runtime"
        return {"thread": {"status": {"type": "idle"}}}

    broker.app_call = app_call
    result = asyncio.run(broker.sessions_status())

    assert result[0]["runtime_state"] == "idle"
    assert result[0]["context_utilization_pct"] == 50.0
    assert result[0]["compactions"] == 0


def test_codex_compact_requires_pause_and_waits_for_new_compaction(monkeypatch):
    module = _load_broker("codex_broker_lifecycle_compact")
    broker = module.Broker()
    session = module.Session(
        launch_id="launch-compact",
        name="worker",
        project="tools",
        cwd="/tmp/project",
        control_url="http://127.0.0.1:8765",
        thread_id="thread-compact",
        cursor=0,
    )
    broker.sessions[session.launch_id] = session
    session.ingress_pause_tokens.add("pause-1")
    reads = iter(
        [
            {"thread": {"status": {"type": "idle"}, "turns": []}},
            {
                "thread": {
                    "status": {"type": "idle"},
                    "turns": [
                        {"items": [{"type": "contextCompaction"}]},
                    ],
                }
            },
        ]
    )

    async def app_call(method, params, timeout):
        if method == "thread/read":
            return next(reads)
        assert method == "thread/compact/start"
        return {}

    async def no_sleep(_delay):
        return None

    broker.app_call = app_call
    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    result = asyncio.run(
        broker.compact_session(session.launch_id, "pause-1")
    )

    assert result == {"ok": True, "compactions": 1}


def test_codex_handoff_waits_for_card_and_leaves_ingress_paused(
    tmp_path,
    monkeypatch,
):
    module = _load_broker("codex_broker_lifecycle_handoff")
    broker = module.Broker()
    session = module.Session(
        launch_id="launch-handoff",
        name="worker",
        project="tools",
        cwd=str(tmp_path),
        control_url="http://127.0.0.1:8765",
        thread_id="thread-handoff",
        cursor=0,
    )
    broker.sessions[session.launch_id] = session
    session.ingress_pause_tokens.add("pause-handoff")
    reads = iter(
        [
            {"thread": {"status": {"type": "idle"}, "turns": []}},
            {
                "thread": {
                    "status": {"type": "idle"},
                    "turns": [{"id": "turn-handoff", "items": []}],
                }
            },
        ]
    )

    async def app_call(method, params, timeout):
        if method == "thread/read":
            return next(reads)
        assert method == "turn/start"
        handoff = tmp_path / ".codex" / "handoff-pending.md"
        handoff.parent.mkdir()
        handoff.write_text("# Handoff\n", encoding="utf-8")
        return {"turn": {"id": "turn-handoff"}}

    async def no_sleep(_delay):
        return None

    broker.app_call = app_call
    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    result = asyncio.run(
        broker.handoff_session(session.launch_id, "pause-handoff")
    )

    assert result["ok"] is True
    assert result["ingress_paused"] is True
    assert session.ingress_paused is True


def test_controller_discovers_live_amclaude_descriptor(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_HOME", str(tmp_path))
    from agent_meeting.lifecycle_control import controller_process

    wrappers = tmp_path / "control" / "wrappers"
    wrappers.mkdir(parents=True)
    descriptor = {
        "wrapper": "amclaude",
        "platform": "claude",
        "name": "worker",
        "project": "tools",
        "identity": "worker@tools",
        "instance_id": "instance-1",
        "wrapper_pid": os.getpid(),
        "cwd": str(tmp_path),
        "status": "running",
        "capabilities": ["observe"],
    }
    (wrappers / "amclaude-instance-1.json").write_text(
        json.dumps(descriptor),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        controller_process,
        "_http_json",
        lambda *_args, **_kwargs: {"sessions": []},
    )
    monkeypatch.setattr(
        controller_process,
        "detect_claude_state",
        lambda _cwd, **_kwargs: {
            "state": "idle",
            "confidence": "high",
            "source": "test",
        },
    )

    sessions = controller_process.Controller().scan()

    assert len(sessions) == 1
    assert sessions[0]["identity"] == "worker@tools"
    assert sessions[0]["state"] == "idle"


def test_amclaude_descriptor_does_not_persist_arguments(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_HOME", str(tmp_path))
    from agent_meeting.launcher.amclaude_session import ClaudeSupervisor

    supervisor = ClaudeSupervisor(
        ["--dangerous-value", "secret"],
        name="worker",
        project="tools",
    )
    descriptor = supervisor.descriptor()

    assert descriptor["identity"] == "worker@tools"
    assert descriptor["launch_recipe"]["args_persisted"] is False
    assert "secret" not in json.dumps(descriptor)


def test_amclaude_builds_model_and_effort_into_child_command():
    from agent_meeting.launcher.amclaude_session import build_claude_launch_cmd

    assert build_claude_launch_cmd([]) == [
        "claude",
        "--model",
        "claude-opus-5",
        "--effort",
        "high",
    ]
    assert build_claude_launch_cmd(
        ["--verbose"],
        model="claude-sonnet-5",
        effort="max",
    ) == [
        "claude",
        "--model",
        "claude-sonnet-5",
        "--effort",
        "max",
        "--verbose",
    ]


def test_amclaude_help_exposes_model_and_effort_choices(capsys):
    from agent_meeting.launcher.amclaude_session import main

    assert main(["--amclaude-help"]) == 0

    output = capsys.readouterr().out
    assert (
        "--model {claude-fable-5,claude-opus-5,claude-sonnet-5}" in output
    )
    assert "--effort {ultracode,max,extra,high,medium}" in output
    assert "--am-msgd HOST[:PORT]" in output


def test_amclaude_consumes_and_normalizes_explicit_am_msgd(monkeypatch):
    from agent_meeting.launcher import amclaude_session

    captured = {}

    class FakeSupervisor:
        def __init__(self, claude_args, **kwargs):
            captured["claude_args"] = claude_args
            captured.update(kwargs)

        def run(self):
            return 0

    monkeypatch.setattr(amclaude_session.shutil, "which", lambda _name: "claude")
    monkeypatch.setattr(amclaude_session, "ClaudeSupervisor", FakeSupervisor)

    assert amclaude_session.main(
        ["worker", "--proj=tools", "--am-msgd=10.0.0.114", "--verbose"]
    ) == 0
    assert captured["claude_args"] == ["--verbose"]
    assert captured["control_url"] == "http://10.0.0.114:8765"


def test_amclaude_passes_explicit_am_msgd_to_child_environment(monkeypatch):
    from agent_meeting.launcher import amclaude_session

    captured = {}

    def popen(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return object()

    monkeypatch.setenv("KEEP", "yes")
    monkeypatch.setattr(amclaude_session.subprocess, "Popen", popen)
    supervisor = amclaude_session.ClaudeSupervisor(
        [],
        name="worker",
        project="tools",
        control_url="http://10.0.0.114:8765",
    )

    supervisor._spawn()

    assert captured["env"]["KEEP"] == "yes"
    assert captured["env"]["AM_MSGD_HOST"] == "http://10.0.0.114:8765"


def test_amclaude_windows_tty_lookup_is_safe(monkeypatch):
    from agent_meeting.launcher import amclaude_session

    monkeypatch.setattr(amclaude_session.os, "name", "nt")
    monkeypatch.delattr(amclaude_session.os, "ttyname", raising=False)

    assert amclaude_session._tty_name() is None


def test_amclaude_exit_sends_exactly_two_interrupts(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETING_HOME", str(tmp_path))
    from agent_meeting.launcher.amclaude_session import ClaudeSupervisor

    class FakeProcess:
        pid = 1234

        def __init__(self):
            self.signals = []

        def poll(self):
            return 0 if len(self.signals) >= 2 else None

        def send_signal(self, value):
            self.signals.append(value)

    supervisor = ClaudeSupervisor([], name="worker", project="tools")
    process = FakeProcess()
    supervisor.process = process

    assert supervisor.request_exit() is True
    assert len(process.signals) == 2


def test_public_session_never_exposes_wrapper_control_token():
    from agent_meeting.lifecycle_control.controller_process import _public_session

    public = _public_session(
        {
            "identity": "worker@tools",
            "control": {"host": "127.0.0.1", "port": 1234, "token": "secret"},
            "delivery_control": {
                "host": "127.0.0.1",
                "port": 4321,
                "token": "monitor-secret",
            },
        }
    )

    assert public["control"] == {"host": "127.0.0.1", "port": 1234}
    assert public["delivery_control"] == {
        "host": "127.0.0.1",
        "port": 4321,
    }
    assert "secret" not in json.dumps(public)


def test_claude_monitor_delivery_endpoint_pauses_and_resumes(tmp_path):
    from agent_meeting.lifecycle_control.claude_monitor_endpoint import (
        ClaudeMonitorControl,
    )
    from agent_meeting.lifecycle_control.terminals import WrapperTerminalAdapter

    endpoint = ClaudeMonitorControl(
        meeting_home=tmp_path,
        name="worker",
        project="tools",
        instance_id="monitor-1",
        cwd="/tmp/project",
    )
    endpoint.start()
    try:
        handle = endpoint.descriptor()["control"]
        endpoint.paused_ack_event.set()
        adapter = WrapperTerminalAdapter()

        assert adapter.pause_delivery(handle) is True
        assert endpoint.pause_event.is_set()
        assert adapter.resume_delivery(handle) is True
        assert not endpoint.pause_event.is_set()
    finally:
        endpoint.stop()

    assert not endpoint.descriptor_path.exists()


def test_subscription_backoff_acknowledges_pause_immediately():
    from agent_meeting.clients.hub_subscription_client import (
        HubSubscriptionClient,
    )

    pause = threading.Event()
    acknowledged = threading.Event()
    client = HubSubscriptionClient(
        self_name="worker",
        project=lambda: "tools",
        resolve_addr=lambda: None,
        read_token=lambda: "",
        on_text=lambda _message: None,
        pause_event=pause,
        paused_ack_event=acknowledged,
    )
    started = time.monotonic()
    waiter = threading.Thread(target=client._wait_for_retry, args=(5,))
    waiter.start()
    pause.set()
    waiter.join(timeout=1)

    assert acknowledged.is_set()
    assert time.monotonic() - started < 1


def test_controller_compact_pauses_and_resumes_codex_ingress(
    tmp_path,
    monkeypatch,
):
    from agent_meeting.lifecycle_control import controller_process

    monkeypatch.setenv("MEETING_HOME", str(tmp_path))
    controller = controller_process.Controller()
    monkeypatch.setattr(
        controller,
        "find_session",
        lambda _name, _project: {
            "wrapper": "amcodex",
            "identity": "worker@tools",
            "launch_id": "launch-compact",
            "state": "idle",
            "confidence": "high",
        },
    )
    calls = []

    def fake_http(url, *, method="GET", body=None, timeout=1):
        calls.append((url, method, body, timeout))
        if url.endswith("/pause"):
            return {"ok": True, "pause_token": "pause-1"}
        if url.endswith("/compact"):
            return {"ok": True, "compactions": 2}
        return {"ok": True, "ingress_paused": False}

    monkeypatch.setattr(controller_process, "_http_json", fake_http)
    result = controller.agent_action("worker", "tools", "compact")

    assert result == {"ok": True, "compactions": 2}
    assert [call[0].rsplit("/", 1)[-1] for call in calls] == [
        "pause",
        "compact",
        "resume",
    ]
    audit = [
        json.loads(line)
        for line in (tmp_path / "control" / "actions.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [item["event"] for item in audit] == ["requested", "completed"]


def test_controller_handoff_keeps_codex_draining(tmp_path, monkeypatch):
    from agent_meeting.lifecycle_control import controller_process

    monkeypatch.setenv("MEETING_HOME", str(tmp_path))
    controller = controller_process.Controller()
    monkeypatch.setattr(
        controller,
        "find_session",
        lambda _name, _project: {
            "wrapper": "amcodex",
            "identity": "worker@tools",
            "launch_id": "launch-handoff",
            "instance_id": "launch-handoff",
            "state": "idle",
            "confidence": "high",
        },
    )
    calls = []

    def fake_http(url, *, method="GET", body=None, timeout=1):
        calls.append(url)
        if url.endswith("/pause"):
            return {"ok": True, "pause_token": "pause-handoff"}
        if url.endswith("/handoff"):
            return {
                "ok": True,
                "handoff_path": "/tmp/project/.codex/handoff-pending.md",
                "ingress_paused": True,
            }
        raise AssertionError("handoff success must not resume the old session")

    monkeypatch.setattr(controller_process, "_http_json", fake_http)
    result = controller.agent_action("worker", "tools", "handoff")

    assert result["ingress_paused"] is True
    assert [url.rsplit("/", 1)[-1] for url in calls] == ["pause", "handoff"]
    draining = json.loads(
        (
            tmp_path / "control" / "draining" / "launch-handoff.json"
        ).read_text(encoding="utf-8")
    )
    assert draining["pause_token"] == "pause-handoff"


def test_macos_login_service_preserves_custom_meeting_home(
    tmp_path,
    monkeypatch,
):
    from agent_meeting.lifecycle_control import user_service
    from agent_meeting.operating_systems import user_service as service_core

    home = tmp_path / "user"
    meeting_home = tmp_path / "custom-meeting"
    commands = []
    monkeypatch.setattr(user_service.Path, "home", lambda: home)
    monkeypatch.setattr(
        service_core,
        "_run",
        lambda command: (
            commands.append(command)
            or SimpleNamespace(returncode=0, stderr="", stdout="")
        ),
    )

    service_core.restart(
        user_service._spec(meeting_home),
        system_name="Darwin",
        home=home,
    )

    plist_path = home / "Library" / "LaunchAgents" / "ai.woodor.am-ctld.plist"
    definition = plistlib.loads(plist_path.read_bytes())
    assert definition["ProgramArguments"] == [
        str(meeting_home / "bin" / "am-ctld"),
        "--meeting-home",
        str(meeting_home),
    ]
    assert commands[-1][:2] == ["launchctl", "bootstrap"]


def test_linux_restart_enables_user_service(tmp_path, monkeypatch):
    from agent_meeting.lifecycle_control import user_service
    from agent_meeting.operating_systems import user_service as service_core

    commands = []
    monkeypatch.setattr(
        service_core,
        "_run",
        lambda command: (
            commands.append(command)
            or SimpleNamespace(returncode=0, stderr="", stdout="")
        ),
    )

    service_core.restart(
        user_service._spec(tmp_path),
        system_name="Linux",
        home=tmp_path / "user",
    )

    assert ["systemctl", "--user", "enable", "woodor-am-ctld.service"] in commands
    assert commands[-1] == [
        "systemctl",
        "--user",
        "restart",
        "woodor-am-ctld.service",
    ]


def test_windows_login_task_preserves_custom_meeting_home(
    tmp_path,
    monkeypatch,
):
    from agent_meeting.lifecycle_control import user_service
    from agent_meeting.operating_systems import user_service as service_core

    commands = []
    meeting_home = tmp_path / "custom meeting"
    runtime_command = (
        meeting_home
        / "runtimes"
        / "0.18.20"
        / "venv"
        / "Scripts"
        / "am-ctld-service.exe"
    )
    runtime_command.parent.mkdir(parents=True)
    runtime_command.write_bytes(b"launcher")
    (meeting_home / "active-runtime.json").write_text(
        json.dumps(
            {"commands": {"am-ctld-service": str(runtime_command)}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(user_service.sys, "platform", "win32")
    monkeypatch.setattr(
        service_core,
        "_run",
        lambda command: (
            commands.append(command)
            or SimpleNamespace(returncode=0, stderr="", stdout="")
        ),
    )

    service_core.restart(
        user_service._spec(meeting_home),
        system_name="Windows",
    )

    task_command = commands[0][commands[0].index("/TR") + 1]
    assert str(runtime_command) in task_command
    assert (
        f'--service-log "{meeting_home / "control" / "am-ctld.log"}"'
        in task_command
    )
    assert f'--meeting-home "{meeting_home}"' in task_command
    assert commands[2][-1] == "/Enable"
    assert commands[3][:2] == ["schtasks", "/Run"]


def test_windows_user_service_stop_disables_logon_task(tmp_path, monkeypatch):
    from agent_meeting.lifecycle_control import user_service
    from agent_meeting.operating_systems import user_service as service_core

    commands = []
    monkeypatch.setattr(
        service_core,
        "_run",
        lambda command: (
            commands.append(command)
            or SimpleNamespace(returncode=0, stderr="", stdout="")
        ),
    )

    service_core.stop(
        user_service._spec(tmp_path),
        system_name="Windows",
    )

    assert commands[-1][-1] == "/Disable"


def test_lifecycle_rules_are_disabled_by_default(tmp_path):
    from agent_meeting.lifecycle_control.rules import (
        evaluate_session,
        load_rule_config,
    )

    config = load_rule_config(tmp_path / "missing.toml")
    decision = evaluate_session(
        {
            "wrapper": "amcodex",
            "state": "idle",
            "confidence": "high",
            "context_utilization_pct": 99,
            "compactions": 4,
        },
        config,
    )

    assert config["enabled"] is False
    assert decision is None


def test_lifecycle_rules_choose_compact_then_handoff(tmp_path):
    from agent_meeting.lifecycle_control.rules import (
        evaluate_session,
        load_rule_config,
    )

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[automation]
enabled = true

[codex]
compact_token_pct = 60
handoff_token_pct = 80
max_compactions = 2
""".strip(),
        encoding="utf-8",
    )
    config = load_rule_config(config_path)
    base = {
        "wrapper": "amcodex",
        "state": "idle",
        "confidence": "high",
        "compactions": 0,
    }

    assert evaluate_session(
        {**base, "context_utilization_pct": 65},
        config,
    ).command == "compact"
    assert evaluate_session(
        {**base, "context_utilization_pct": 85},
        config,
    ).command == "handoff"
    assert evaluate_session(
        {**base, "context_utilization_pct": 10, "compactions": 2},
        config,
    ).command == "handoff"


def test_claude_transcript_reports_utilization_and_compactions(
    tmp_path,
    monkeypatch,
):
    from agent_meeting.lifecycle_control import status_detectors

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    transcript = project_dir / "session-1.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "timestamp": "2026-07-30T09:00:00Z",
                        "message": {
                            "model": "claude-sonnet-4-6",
                            "stop_reason": "end_turn",
                            "usage": {
                                "input_tokens": 100,
                                "cache_read_input_tokens": 99_900,
                                "cache_creation_input_tokens": 0,
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "system",
                        "subtype": "compact_boundary",
                        "compactMetadata": {"postTokens": 20_000},
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "timestamp": "2026-07-30T09:01:00Z",
                        "message": {
                            "model": "claude-sonnet-4-6",
                            "stop_reason": "end_turn",
                            "usage": {
                                "input_tokens": 0,
                                "cache_read_input_tokens": 39_000,
                                "cache_creation_input_tokens": 1_000,
                            },
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        status_detectors,
        "_claude_project_dir",
        lambda _cwd: project_dir,
    )

    result = status_detectors.detect_claude_state("/tmp/project")

    assert result["state"] == "idle"
    assert result["compactions"] == 1
    assert result["context_tokens"] == 40_000
    assert result["context_window"] == 200_000
    assert result["context_utilization_pct"] == 20.0
    assert result["transcript_session_id"] == "session-1"


def test_lifecycle_rules_apply_claude_specific_thresholds(tmp_path):
    from agent_meeting.lifecycle_control.rules import (
        evaluate_session,
        load_rule_config,
    )

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[automation]
enabled = true
max_consecutive_failures = 4

[claude]
compact_token_pct = 45
handoff_token_pct = 70
max_compactions = 3
""".strip(),
        encoding="utf-8",
    )
    config = load_rule_config(config_path)
    decision = evaluate_session(
        {
            "wrapper": "amclaude",
            "state": "idle",
            "confidence": "high",
            "context_utilization_pct": 50,
            "compactions": 0,
        },
        config,
    )

    assert decision.command == "compact"
    assert config["max_consecutive_failures"] == 4


def test_lifecycle_rule_config_falls_back_for_invalid_numbers(tmp_path):
    from agent_meeting.lifecycle_control.rules import load_rule_config

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[automation]
enabled = "true"
action_cooldown_seconds = "later"
max_consecutive_failures = -1

[claude]
compact_token_pct = "many"
max_compactions = -2
context_limits = "invalid"
""".strip(),
        encoding="utf-8",
    )

    config = load_rule_config(config_path)

    assert config["enabled"] is False
    assert config["action_cooldown_seconds"] == 600
    assert config["max_consecutive_failures"] == 3
    assert config["claude"]["compact_token_pct"] == 60.0
    assert config["claude"]["max_compactions"] == 2
    assert config["claude"]["context_limits"] == {}


def test_action_state_persists_failure_limit_and_recovers_interrupted(tmp_path):
    from agent_meeting.lifecycle_control.action_state import ActionStateStore

    path = tmp_path / "action-state.json"
    store = ActionStateStore(path)
    store.transition(
        "instance-1",
        "worker@tools",
        "compact",
        "maintenance",
        automatic=True,
    )
    recovered = ActionStateStore(path)

    assert (
        recovered.automation_block_reason(
            "instance-1",
            "compact",
            cooldown_seconds=0,
            max_consecutive_failures=1,
        )
        is not None
    )
    record = recovered.payload["records"]["instance-1:compact"]
    assert record["status"] == "failed"
    assert record["error_type"] == "ControllerRestarted"


def test_controller_clear_codex_verifies_new_thread(tmp_path, monkeypatch):
    from agent_meeting.lifecycle_control import controller_process

    monkeypatch.setenv("MEETING_HOME", str(tmp_path))

    class Terminal:
        def capabilities(self, _handle):
            return SimpleNamespace(can_send_text=True)

        def send_text(self, _handle, text):
            assert text == "/clear"
            return True

    controller = controller_process.Controller()
    monkeypatch.setattr(
        controller,
        "find_session",
        lambda _name, _project: {
            "wrapper": "amcodex",
            "identity": "worker@tools",
            "instance_id": "launch-clear",
            "launch_id": "launch-clear",
            "thread_id": "thread-old",
            "terminal_handle": {"type": "tmux", "pane": "%1"},
            "state": "idle",
            "confidence": "high",
        },
    )
    monkeypatch.setattr(controller_process, "adapter_for_handle", lambda _h: Terminal())
    snapshots = iter(
        [
            {"thread_id": "thread-old", "runtime_state": "idle"},
            {"thread_id": "thread-new", "runtime_state": "idle"},
        ]
    )
    calls = []

    def fake_http(url, *, method="GET", body=None, timeout=1):
        calls.append(url)
        if url.endswith("/pause"):
            return {"ok": True, "pause_token": "pause-clear"}
        if url.endswith("/sessions"):
            return {"sessions": [{"launch_id": "launch-clear", **next(snapshots)}]}
        return {"ok": True, "ingress_paused": False}

    monkeypatch.setattr(controller_process, "_http_json", fake_http)

    result = controller.agent_action("worker", "tools", "clear")

    assert result["thread_id"] == "thread-new"
    assert [url.rsplit("/", 1)[-1] for url in calls] == [
        "pause",
        "sessions",
        "sessions",
        "resume",
    ]


def test_controller_compact_claude_pauses_verifies_and_resumes(
    tmp_path,
    monkeypatch,
):
    from agent_meeting.lifecycle_control import controller_process

    monkeypatch.setenv("MEETING_HOME", str(tmp_path))
    events = []

    class Terminal:
        def capabilities(self, _handle):
            return SimpleNamespace(can_send_text=True)

        def send_text(self, _handle, text):
            events.append(text)
            return True

    class Delivery:
        def pause_delivery(self, _handle):
            events.append("pause")
            return True

        def resume_delivery(self, _handle):
            events.append("resume")
            return True

    controller = controller_process.Controller()
    monkeypatch.setattr(
        controller,
        "find_session",
        lambda _name, _project: {
            "wrapper": "amclaude",
            "identity": "worker@tools",
            "instance_id": "claude-1",
            "cwd": str(tmp_path),
            "terminal_handle": {"type": "tmux", "pane": "%1"},
            "delivery_control": {"port": 1, "token": "secret"},
            "state": "idle",
            "confidence": "high",
        },
    )
    monkeypatch.setattr(controller_process, "adapter_for_handle", lambda _h: Terminal())
    monkeypatch.setattr(controller_process, "WrapperTerminalAdapter", Delivery)
    snapshots = iter(
        [
            {"state": "idle", "confidence": "high", "compactions": 0},
            {
                "state": "idle",
                "confidence": "high",
                "compactions": 1,
                "context_utilization_pct": 10,
            },
        ]
    )
    monkeypatch.setattr(
        controller_process,
        "detect_claude_state",
        lambda _cwd: next(snapshots),
    )

    result = controller.agent_action("worker", "tools", "compact")

    assert result["compactions"] == 1
    assert events == ["pause", "/compact", "resume"]


def test_controller_handoff_claude_leaves_delivery_paused(
    tmp_path,
    monkeypatch,
):
    from agent_meeting.lifecycle_control import controller_process

    monkeypatch.setenv("MEETING_HOME", str(tmp_path / "meeting"))
    events = []

    class Terminal:
        def capabilities(self, _handle):
            return SimpleNamespace(can_send_text=True)

        def send_text(self, _handle, text):
            events.append(text)
            handoff = tmp_path / ".claude" / "handoff-pending.md"
            handoff.parent.mkdir()
            handoff.write_text("# Handoff\n", encoding="utf-8")
            return True

    class Delivery:
        def pause_delivery(self, _handle):
            events.append("pause")
            return True

        def resume_delivery(self, _handle):
            events.append("resume")
            return True

    controller = controller_process.Controller()
    monkeypatch.setattr(
        controller,
        "find_session",
        lambda _name, _project: {
            "wrapper": "amclaude",
            "identity": "worker@tools",
            "instance_id": "claude-handoff",
            "cwd": str(tmp_path),
            "terminal_handle": {"type": "tmux", "pane": "%1"},
            "delivery_control": {"port": 1, "token": "secret"},
            "state": "idle",
            "confidence": "high",
        },
    )
    monkeypatch.setattr(
        controller_process,
        "adapter_for_handle",
        lambda _handle: Terminal(),
    )
    monkeypatch.setattr(
        controller_process,
        "WrapperTerminalAdapter",
        Delivery,
    )
    monkeypatch.setattr(
        controller_process,
        "detect_claude_state",
        lambda _cwd: {"state": "idle", "confidence": "high"},
    )

    result = controller.agent_action("worker", "tools", "handoff")

    assert result["delivery_paused"] is True
    assert events == ["pause", "/handoff"]
    draining = json.loads(
        (
            tmp_path
            / "meeting"
            / "control"
            / "draining"
            / "claude-handoff.json"
        ).read_text(encoding="utf-8")
    )
    assert draining["handoff_path"].endswith(
        ".claude/handoff-pending.md"
    )


def test_tmux_terminal_adapter_sends_literal_text_then_enter(monkeypatch):
    from agent_meeting.lifecycle_control.terminals.tmux import (
        TmuxTerminalAdapter,
    )
    from agent_meeting.lifecycle_control.terminals import tmux

    calls = []
    monkeypatch.setattr(tmux.shutil, "which", lambda _name: "/usr/bin/tmux")
    monkeypatch.setattr(
        tmux.subprocess,
        "run",
        lambda command, **_kwargs: (
            calls.append(command)
            or SimpleNamespace(returncode=0)
        ),
    )

    assert TmuxTerminalAdapter().send_text(
        {"type": "tmux", "pane": "%3"},
        "/compact",
    )
    assert calls == [
        ["/usr/bin/tmux", "send-keys", "-t", "%3", "-l", "/compact"],
        ["/usr/bin/tmux", "send-keys", "-t", "%3", "Enter"],
    ]


def test_iterm_adapter_targets_environment_session_id(monkeypatch):
    from agent_meeting.lifecycle_control.terminals.mac_iterm2 import (
        Iterm2TerminalAdapter,
    )
    from agent_meeting.lifecycle_control.terminals import mac_iterm2

    calls = []
    monkeypatch.setattr(mac_iterm2.sys, "platform", "darwin")
    monkeypatch.setattr(
        mac_iterm2.shutil,
        "which",
        lambda _name: "/usr/bin/osascript",
    )
    monkeypatch.setattr(
        mac_iterm2.subprocess,
        "run",
        lambda command, **_kwargs: (
            calls.append(command)
            or SimpleNamespace(returncode=0, stdout="ok\n")
        ),
    )

    adapter = Iterm2TerminalAdapter()
    handle = {"session_id": "w4t0p0:SESSION-UUID"}

    assert adapter.capabilities(handle).requires_user_permission is True
    assert adapter.send_text(handle, "/handoff") is True
    assert calls[0][-2:] == ["SESSION-UUID", "/handoff"]


def test_windows_terminal_without_owned_conpty_fails_closed():
    from agent_meeting.lifecycle_control.terminals import adapter_for_handle

    adapter = adapter_for_handle(
        {
            "type": "windows-terminal",
            "session_id": "wt-session",
            "conpty_owned": False,
        }
    )

    assert adapter.capabilities({}).can_send_text is False
    assert adapter.send_text({}, "/compact") is False
