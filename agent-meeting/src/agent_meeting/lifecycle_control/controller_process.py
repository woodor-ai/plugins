"""Always-on local lifecycle inventory and control process."""

from __future__ import annotations

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import secrets
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agent_meeting.lifecycle_control.action_state import ActionStateStore
from agent_meeting.lifecycle_control.terminals import (
    WrapperTerminalAdapter,
    adapter_for_handle,
)
from agent_meeting.lifecycle_control.status_detectors import detect_claude_state
from agent_meeting.lifecycle_control.rules import evaluate_session, load_rule_config


API_HOST = "127.0.0.1"
API_PORT = int(os.environ.get("AM_CTLD_PORT", "8789"))
SCAN_INTERVAL_SECONDS = int(os.environ.get("AM_CTLD_SCAN_INTERVAL", "300"))
CODEXD_BASE_URL = (
    f"http://127.0.0.1:"
    f"{int(os.environ.get('MEETING_BROKER_API_PORT', '8788'))}"
)
CODEXD_SESSIONS_URL = f"{CODEXD_BASE_URL}/sessions"


def control_home() -> Path:
    meeting_home = Path(
        os.environ.get("MEETING_HOME") or (Path.home() / ".agent-meeting")
    )
    return meeting_home / "control"


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _http_json(
    url: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    timeout: float = 1.0,
) -> dict:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _public_session(session: dict) -> dict:
    result = dict(session)
    for field in ("control", "delivery_control"):
        control = result.get(field)
        if isinstance(control, dict):
            result[field] = {
                key: value for key, value in control.items() if key != "token"
            }
    return result


def _draining_path(instance_id: str) -> Path:
    return control_home() / "draining" / f"{instance_id}.json"


def _write_draining(instance_id: str, payload: dict) -> None:
    path = _draining_path(instance_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    os.replace(temporary, path)


def _read_draining(instance_id: str) -> dict:
    return _read_json(_draining_path(instance_id))


class Controller:
    def __init__(self):
        self.started_at = int(time.time())
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.sessions: list[dict] = []
        self.action_locks: dict[str, threading.Lock] = {}
        self.action_state = ActionStateStore(control_home() / "action-state.json")
        self.logger = logging.getLogger("am-ctld")

    def scan(self) -> list[dict]:
        sessions: list[dict] = []
        rule_config = load_rule_config(control_home() / "config.toml")
        try:
            result = _http_json(CODEXD_SESSIONS_URL)
            for item in result.get("sessions", []):
                sessions.append(
                    {
                        **item,
                        "wrapper": "amcodex",
                        "platform": "codex",
                        "instance_id": item.get("launch_id"),
                        "state": (
                            "paused"
                            if item.get("ingress_paused")
                            else item.get("runtime_state") or "unknown"
                        ),
                        "confidence": (
                            "high"
                            if item.get("runtime_state") not in {None, "unknown"}
                            else "low"
                        ),
                        "source": "am-codexd/thread-read",
                        "capabilities": [
                            "observe",
                            "pause_ingress",
                        ],
                    }
                )
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            pass

        wrapper_dir = control_home() / "wrappers"
        wrapper_dir.mkdir(parents=True, exist_ok=True)
        monitor_dir = control_home() / "monitors"
        monitor_dir.mkdir(parents=True, exist_ok=True)
        monitor_controls: dict[str, dict] = {}
        for monitor_path in monitor_dir.glob("*.json"):
            monitor = _read_json(monitor_path)
            if not monitor or not _pid_alive(monitor.get("monitor_pid")):
                try:
                    monitor_path.unlink()
                except OSError:
                    pass
                continue
            identity = monitor.get("identity")
            if identity:
                monitor_controls[identity] = monitor

        descriptors: dict[str, dict] = {}
        for descriptor_path in wrapper_dir.glob("*.json"):
            descriptor = _read_json(descriptor_path)
            if not descriptor:
                continue
            if not _pid_alive(descriptor.get("wrapper_pid")):
                try:
                    descriptor_path.unlink()
                except OSError:
                    pass
                continue
            if descriptor.get("wrapper") == "amclaude":
                descriptor = {
                    **descriptor,
                    **detect_claude_state(
                        descriptor.get("cwd") or "",
                        context_limits=rule_config["claude"][
                            "context_limits"
                        ],
                    ),
                }
                monitor = monitor_controls.get(descriptor.get("identity"))
                if monitor:
                    descriptor["delivery_control"] = monitor.get("control")
                    descriptor["delivery_paused"] = monitor.get(
                        "delivery_paused",
                        False,
                    )
                    descriptor["capabilities"] = sorted(
                        set(descriptor.get("capabilities") or [])
                        | {
                            "pause_delivery",
                            "resume_delivery",
                        }
                    )
            descriptor["descriptor_path"] = str(descriptor_path)
            instance_id = descriptor.get("instance_id")
            if instance_id:
                descriptors[instance_id] = descriptor

        merged: list[dict] = []
        for session in sessions:
            descriptor = descriptors.pop(session.get("instance_id"), None)
            combined = {**session, **(descriptor or {})}
            combined["capabilities"] = sorted(
                set(session.get("capabilities") or [])
                | set((descriptor or {}).get("capabilities") or [])
            )
            merged.append(combined)
        merged.extend(descriptors.values())
        sessions = merged
        for session in sessions:
            instance_id = str(session.get("instance_id") or "")
            if instance_id and _draining_path(instance_id).exists():
                session["state"] = "draining"

        with self.lock:
            previous = {
                item.get("instance_id"): item.get("state")
                for item in self.sessions
            }
            self.sessions = sessions
        current = {
            item.get("instance_id"): item.get("state")
            for item in sessions
        }
        if current != previous:
            self.logger.info(
                "session inventory changed: %s",
                json.dumps(current, ensure_ascii=False, sort_keys=True),
            )
        return sessions

    def find_session(self, name: str, project: str) -> dict:
        matches = [
            item
            for item in self.scan()
            if item.get("name") == name and item.get("project") == project
        ]
        if not matches:
            raise ValueError(f"agent {name}@{project} was not found on this machine")
        if len(matches) != 1:
            raise ValueError(
                f"agent {name}@{project} matches {len(matches)} local instances"
            )
        return matches[0]

    def _wrapper_request(self, session: dict, command: str) -> dict:
        control = session.get("control") or {}
        adapter = WrapperTerminalAdapter()
        if command == "exit":
            return {"ok": adapter.send_interrupt(control, count=2)}
        if command == "restart":
            return {"ok": adapter.restart_in_place(control)}
        raise ValueError("unsupported wrapper command")

    @staticmethod
    def _require_idle(session: dict, command: str) -> None:
        if session.get("state") != "idle" or session.get("confidence") != "high":
            raise ValueError(
                f"{command} requires an idle high-confidence session"
            )

    @staticmethod
    def _terminal_adapter(session: dict):
        handle = session.get("terminal_handle") or {}
        adapter = adapter_for_handle(handle)
        capabilities = adapter.capabilities(handle)
        if not capabilities.can_send_text:
            terminal_type = handle.get("type") or "unknown"
            raise ValueError(
                f"{terminal_type} cannot safely inject lifecycle commands; "
                "use tmux or grant iTerm2 Automation access"
            )
        return adapter, handle

    @staticmethod
    def _codex_session_snapshot(launch_id: str) -> dict:
        result = _http_json(CODEXD_SESSIONS_URL, timeout=5)
        for item in result.get("sessions") or []:
            if item.get("launch_id") == launch_id:
                return item
        return {}

    def _compact_amcodex(self, session: dict) -> dict:
        self._require_idle(session, "compact")
        launch_id = session.get("launch_id")
        paused = _http_json(
            f"{CODEXD_BASE_URL}/session/ingress/pause",
            method="POST",
            body={"launch_id": launch_id},
            timeout=5,
        )
        pause_token = paused["pause_token"]
        self.logger.info(
            "meeting ingress paused identity=%s instance=%s",
            session.get("identity"),
            launch_id,
        )
        try:
            return _http_json(
                f"{CODEXD_BASE_URL}/session/compact",
                method="POST",
                body={
                    "launch_id": launch_id,
                    "pause_token": pause_token,
                },
                timeout=75,
            )
        finally:
            _http_json(
                f"{CODEXD_BASE_URL}/session/ingress/resume",
                method="POST",
                body={
                    "launch_id": launch_id,
                    "pause_token": pause_token,
                },
                timeout=5,
            )
            self.logger.info(
                "meeting ingress resumed identity=%s instance=%s",
                session.get("identity"),
                launch_id,
            )

    def _clear_amcodex(self, session: dict) -> dict:
        self._require_idle(session, "clear")
        adapter, handle = self._terminal_adapter(session)
        launch_id = str(session.get("launch_id") or "")
        previous_thread_id = session.get("thread_id")
        if not previous_thread_id:
            raise ValueError("session thread is not ready")
        paused = _http_json(
            f"{CODEXD_BASE_URL}/session/ingress/pause",
            method="POST",
            body={"launch_id": launch_id},
            timeout=5,
        )
        pause_token = paused["pause_token"]
        try:
            if not adapter.send_text(handle, "/clear"):
                raise ValueError("could not send /clear to the Codex terminal")
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                current = self._codex_session_snapshot(launch_id)
                current_thread_id = current.get("thread_id")
                if (
                    current_thread_id
                    and current_thread_id != previous_thread_id
                    and current.get("runtime_state") == "idle"
                ):
                    return {
                        "ok": True,
                        "previous_thread_id": previous_thread_id,
                        "thread_id": current_thread_id,
                    }
                time.sleep(0.25)
            raise ValueError("Codex /clear did not create a new idle thread")
        finally:
            _http_json(
                f"{CODEXD_BASE_URL}/session/ingress/resume",
                method="POST",
                body={
                    "launch_id": launch_id,
                    "pause_token": pause_token,
                },
                timeout=5,
            )

    def _handoff_amcodex(self, session: dict) -> dict:
        self._require_idle(session, "handoff")
        launch_id = session.get("launch_id")
        paused = _http_json(
            f"{CODEXD_BASE_URL}/session/ingress/pause",
            method="POST",
            body={"launch_id": launch_id},
            timeout=5,
        )
        pause_token = paused["pause_token"]
        try:
            result = _http_json(
                f"{CODEXD_BASE_URL}/session/handoff",
                method="POST",
                body={
                    "launch_id": launch_id,
                    "pause_token": pause_token,
                },
                timeout=195,
            )
        except Exception:
            _http_json(
                f"{CODEXD_BASE_URL}/session/ingress/resume",
                method="POST",
                body={
                    "launch_id": launch_id,
                    "pause_token": pause_token,
                },
                timeout=5,
            )
            raise
        _write_draining(
            launch_id,
            {
                "launch_id": launch_id,
                "identity": session.get("identity"),
                "pause_token": pause_token,
                "handoff_path": result.get("handoff_path"),
            },
        )
        return result

    def _pause_claude_delivery(self, session: dict):
        delivery_control = session.get("delivery_control")
        if not delivery_control:
            raise ValueError(
                "Claude lifecycle action requires a live message monitor"
            )
        adapter = WrapperTerminalAdapter()
        if not adapter.pause_delivery(delivery_control):
            raise ValueError("could not pause Claude message delivery")
        return adapter, delivery_control

    def _resume_claude_after_restart(
        self,
        session: dict,
        previous_control: dict,
    ) -> None:
        adapter = WrapperTerminalAdapter()
        deadline = time.monotonic() + 30
        last_control = previous_control
        while time.monotonic() < deadline:
            try:
                if last_control and adapter.resume_delivery(last_control):
                    return
            except (OSError, ValueError, json.JSONDecodeError):
                pass
            for current in self.scan():
                if current.get("instance_id") != session.get("instance_id"):
                    continue
                last_control = current.get("delivery_control")
                break
            time.sleep(0.25)
        raise ValueError(
            "Claude restarted but no live message monitor could be resumed"
        )

    def _compact_amclaude(self, session: dict) -> dict:
        self._require_idle(session, "compact")
        terminal_adapter, handle = self._terminal_adapter(session)
        delivery_adapter, delivery_control = self._pause_claude_delivery(session)
        previous = detect_claude_state(session.get("cwd") or "")
        previous_compactions = int(previous.get("compactions") or 0)
        try:
            if not terminal_adapter.send_text(handle, "/compact"):
                raise ValueError("could not send /compact to the Claude terminal")
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                current = detect_claude_state(session.get("cwd") or "")
                if (
                    current.get("state") == "idle"
                    and current.get("confidence") == "high"
                    and int(current.get("compactions") or 0)
                    > previous_compactions
                ):
                    return {
                        "ok": True,
                        "compactions": current["compactions"],
                        "context_utilization_pct": current.get(
                            "context_utilization_pct"
                        ),
                    }
                time.sleep(0.25)
            raise ValueError("Claude /compact did not produce a compact boundary")
        finally:
            if not delivery_adapter.resume_delivery(delivery_control):
                raise ValueError(
                    "Claude compact finished but message delivery could not "
                    "be resumed"
                )

    def _handoff_amclaude(self, session: dict) -> dict:
        self._require_idle(session, "handoff")
        terminal_adapter, handle = self._terminal_adapter(session)
        delivery_adapter, delivery_control = self._pause_claude_delivery(session)
        instance_id = str(
            session.get("instance_id") or session.get("launch_id") or ""
        )
        handoff_path = Path(session.get("cwd") or ".") / ".claude" / (
            "handoff-pending.md"
        )
        previous_mtime = (
            handoff_path.stat().st_mtime_ns if handoff_path.exists() else 0
        )
        try:
            if not terminal_adapter.send_text(handle, "/handoff"):
                raise ValueError("could not send /handoff to the Claude terminal")
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                current = detect_claude_state(session.get("cwd") or "")
                card_ready = (
                    handoff_path.exists()
                    and handoff_path.stat().st_size > 0
                    and handoff_path.stat().st_mtime_ns > previous_mtime
                )
                if (
                    card_ready
                    and current.get("state") == "idle"
                    and current.get("confidence") == "high"
                ):
                    _write_draining(
                        instance_id,
                        {
                            "instance_id": instance_id,
                            "identity": session.get("identity"),
                            "platform": "claude",
                            "delivery_control": delivery_control,
                            "handoff_path": str(handoff_path),
                        },
                    )
                    return {
                        "ok": True,
                        "handoff_path": str(handoff_path),
                        "delivery_paused": True,
                    }
                time.sleep(0.25)
            raise ValueError("Claude handoff did not produce a new handoff card")
        except Exception:
            delivery_adapter.resume_delivery(delivery_control)
            raise

    def _resume_amcodex_after_restart(self, session: dict) -> None:
        launch_id = session.get("launch_id")
        draining = _read_draining(launch_id)
        pause_token = draining.get("pause_token")
        if not pause_token:
            return
        _http_json(
            f"{CODEXD_BASE_URL}/session/ingress/resume",
            method="POST",
            body={
                "launch_id": launch_id,
                "pause_token": pause_token,
            },
            timeout=5,
        )
        try:
            _draining_path(launch_id).unlink()
        except FileNotFoundError:
            pass

    def _execute_agent_action(
        self,
        session: dict,
        command: str,
    ) -> tuple[dict, dict]:
        if command == "status":
            return {"ok": True, "session": _public_session(session)}, session
        if command == "compact" and session.get("wrapper") == "amcodex":
            return self._compact_amcodex(session), session
        if command == "clear" and session.get("wrapper") == "amcodex":
            return self._clear_amcodex(session), session
        if command == "handoff" and session.get("wrapper") == "amcodex":
            return self._handoff_amcodex(session), session
        if command == "compact" and session.get("wrapper") == "amclaude":
            return self._compact_amclaude(session), session
        if command == "handoff" and session.get("wrapper") == "amclaude":
            return self._handoff_amclaude(session), session
        if command in {"exit", "restart"} and session.get("wrapper") in {
            "amclaude",
            "amcodex",
        }:
            if session.get("wrapper") == "amclaude":
                delivery_control = session.get("delivery_control")
                if not delivery_control:
                    raise ValueError(
                        "amclaude lifecycle action requires a live message monitor"
                    )
                adapter = WrapperTerminalAdapter()
                if not adapter.pause_delivery(delivery_control):
                    raise ValueError("could not pause Claude message delivery")
                try:
                    result = self._wrapper_request(session, command)
                except Exception:
                    adapter.resume_delivery(delivery_control)
                    raise
                if command == "restart" and result.get("ok"):
                    self._resume_claude_after_restart(
                        session,
                        delivery_control,
                    )
                elif command == "restart" or not result.get("ok"):
                    if not adapter.resume_delivery(delivery_control):
                        raise ValueError(
                            "Claude action completed but message delivery "
                            "could not be resumed"
                        )
                if result.get("ok") and command in {"exit", "restart"}:
                    try:
                        _draining_path(str(session.get("instance_id"))).unlink()
                    except FileNotFoundError:
                        pass
                return result, session
            result = self._wrapper_request(session, command)
            if result.get("ok") and command == "restart":
                self._resume_amcodex_after_restart(session)
            if result.get("ok") and command == "exit":
                try:
                    _draining_path(session.get("launch_id")).unlink()
                except FileNotFoundError:
                    pass
            return result, session
        raise ValueError(
            f"{command} is not implemented for {session.get('wrapper') or 'session'}"
        )

    def _audit_action(self, payload: dict) -> None:
        path = control_home() / "actions.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": int(time.time()),
            **payload,
        }
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        try:
            path.chmod(0o600)
        except OSError:
            pass

    def agent_action(
        self,
        name: str,
        project: str,
        command: str,
        *,
        automatic: bool = False,
    ) -> dict:
        identity = f"{name}@{project}"
        self.logger.info(
            "agent action requested identity=%s command=%s",
            identity,
            command,
        )
        self._audit_action(
            {
                "event": "requested",
                "identity": identity,
                "command": command,
                "automatic": automatic,
            }
        )
        session = self.find_session(name, project)
        if command == "status":
            result, _ = self._execute_agent_action(session, command)
            return result
        instance_id = str(
            session.get("instance_id") or session.get("launch_id") or ""
        )
        if not instance_id:
            raise ValueError("session has no stable instance id")
        with self.lock:
            action_lock = self.action_locks.setdefault(
                instance_id,
                threading.Lock(),
            )
        if not action_lock.acquire(blocking=False):
            raise ValueError("another lifecycle action is already in progress")
        state = (
            "exiting"
            if command == "exit"
            else "restarting"
            if command == "restart"
            else "maintenance"
        )
        try:
            self.action_state.transition(
                instance_id,
                identity,
                command,
                state,
                automatic=automatic,
            )
            try:
                result, session = self._execute_agent_action(session, command)
                if not result.get("ok"):
                    raise ValueError(f"{command} did not complete successfully")
            except Exception as error:
                self.action_state.fail(
                    instance_id,
                    identity,
                    command,
                    type(error).__name__,
                    automatic=automatic,
                )
                self._audit_action(
                    {
                        "event": "failed",
                        "identity": identity,
                        "command": command,
                        "error_type": type(error).__name__,
                        "instance_id": instance_id,
                        "automatic": automatic,
                    }
                )
                raise
        finally:
            action_lock.release()
        self.action_state.complete(
            instance_id,
            identity,
            command,
            automatic=automatic,
        )
        self._audit_action(
            {
                "event": "completed",
                "identity": identity,
                "command": command,
                "instance_id": session.get("instance_id"),
                "ok": bool(result.get("ok")),
                "automatic": automatic,
            }
        )
        return result

    def scan_loop(self) -> None:
        while not self.stop_event.is_set():
            sessions = self.scan()
            config = load_rule_config(control_home() / "config.toml")
            for session in sessions:
                decision = evaluate_session(session, config)
                if decision is None:
                    continue
                instance_id = str(session.get("instance_id") or "")
                block_reason = self.action_state.automation_block_reason(
                    instance_id,
                    decision.command,
                    cooldown_seconds=config["action_cooldown_seconds"],
                    max_consecutive_failures=config[
                        "max_consecutive_failures"
                    ],
                )
                if block_reason:
                    self.logger.info(
                        "lifecycle rule skipped identity=%s command=%s reason=%s",
                        session.get("identity"),
                        decision.command,
                        block_reason,
                    )
                    continue
                self.logger.info(
                    "lifecycle rule matched identity=%s command=%s reason=%s",
                    session.get("identity"),
                    decision.command,
                    decision.reason,
                )
                try:
                    self.agent_action(
                        str(session.get("name")),
                        str(session.get("project")),
                        decision.command,
                        automatic=True,
                    )
                except Exception as error:
                    self.logger.warning(
                        "lifecycle rule action failed identity=%s "
                        "command=%s error=%s",
                        session.get("identity"),
                        decision.command,
                        type(error).__name__,
                    )
            self.stop_event.wait(SCAN_INTERVAL_SECONDS)


def _load_or_create_token() -> str:
    path = control_home() / "token"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    if not token:
        token = secrets.token_urlsafe(32)
        path.write_text(token + "\n", encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return token


def run() -> int:
    home = control_home()
    home.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("am-ctld")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(
            home / "am-ctld.log",
            maxBytes=2_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        )
        logger.addHandler(handler)
    token = _load_or_create_token()
    controller = Controller()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            return

        def send_json(self, status: int, payload: dict):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def authorized(self) -> bool:
            return self.headers.get("Authorization") == f"Bearer {token}"

        def read_body(self) -> dict:
            length = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(length).decode("utf-8") or "{}")

        def do_GET(self):
            if not self.authorized():
                return self.send_json(401, {"error": "unauthorized"})
            if self.path == "/health":
                return self.send_json(
                    200,
                    {
                        "ok": True,
                        "pid": os.getpid(),
                        "started_at": controller.started_at,
                    },
                )
            if self.path == "/sessions":
                return self.send_json(
                    200,
                    {
                        "sessions": [
                            _public_session(item)
                            for item in controller.scan()
                        ]
                    },
                )
            return self.send_json(404, {"error": "not found"})

        def do_POST(self):
            if not self.authorized():
                return self.send_json(401, {"error": "unauthorized"})
            try:
                if self.path == "/shutdown":
                    controller.stop_event.set()
                    threading.Thread(target=server.shutdown, daemon=True).start()
                    return self.send_json(200, {"ok": True})
                if self.path == "/agent/action":
                    body = self.read_body()
                    result = controller.agent_action(
                        str(body["name"]),
                        str(body["project"]),
                        str(body["cmd"]),
                    )
                    return self.send_json(200, result)
                return self.send_json(404, {"error": "not found"})
            except (KeyError, ValueError, OSError, json.JSONDecodeError) as error:
                return self.send_json(409, {"error": str(error)})

    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((API_HOST, API_PORT), Handler)
    pid_path = home / "am-ctld.pid"
    pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    logger.info("am-ctld started pid=%s", os.getpid())
    scanner = threading.Thread(
        target=controller.scan_loop,
        name="am-ctld-scan",
        daemon=True,
    )
    scanner.start()

    def stop_from_signal(_signum, _frame):
        controller.stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(signum, stop_from_signal)
        except (ValueError, OSError):
            pass
    try:
        server.serve_forever()
    finally:
        controller.stop_event.set()
        server.server_close()
        try:
            pid_path.unlink()
        except FileNotFoundError:
            pass
        logger.info("am-ctld stopped pid=%s", os.getpid())
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="am-ctld")
    parser.add_argument("--meeting-home", default=None)
    args = parser.parse_args(argv)
    if args.meeting_home:
        os.environ["MEETING_HOME"] = str(Path(args.meeting_home).resolve())
    try:
        return run()
    except OSError as error:
        print(f"am-ctld: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
