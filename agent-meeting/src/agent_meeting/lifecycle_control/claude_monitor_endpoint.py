"""Authenticated local pause/resume endpoint for one Claude message monitor."""

from __future__ import annotations

import json
import os
import secrets
import socketserver
import threading
from pathlib import Path


def _atomic_private_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    os.replace(temporary, path)


class ClaudeMonitorControl:
    def __init__(
        self,
        *,
        meeting_home: Path,
        name: str,
        project: str,
        instance_id: str,
        cwd: str,
    ):
        self.name = name
        self.project = project
        self.instance_id = instance_id
        self.cwd = cwd
        self.auth_token = secrets.token_urlsafe(32)
        self.pause_event = threading.Event()
        self.paused_ack_event = threading.Event()
        self.server: socketserver.ThreadingTCPServer | None = None
        self.thread: threading.Thread | None = None
        self.descriptor_path = (
            meeting_home
            / "control"
            / "monitors"
            / f"claude-{instance_id}.json"
        )

    def descriptor(self) -> dict:
        return {
            "schema_version": 1,
            "platform": "claude",
            "name": self.name,
            "project": self.project,
            "identity": f"{self.name}@{self.project}",
            "instance_id": self.instance_id,
            "monitor_pid": os.getpid(),
            "cwd": self.cwd,
            "delivery_paused": self.pause_event.is_set(),
            "control": {
                "transport": "tcp",
                "host": "127.0.0.1",
                "port": (
                    self.server.server_address[1]
                    if self.server is not None
                    else None
                ),
                "token": self.auth_token,
            },
            "capabilities": [
                "observe_delivery",
                "pause_delivery",
                "resume_delivery",
            ],
        }

    def publish(self) -> None:
        _atomic_private_json(self.descriptor_path, self.descriptor())

    def start(self) -> None:
        endpoint = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                try:
                    request = json.loads(self.rfile.readline().decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    request = {}
                if request.get("token") != endpoint.auth_token:
                    response = {"ok": False, "error": "unauthorized"}
                elif request.get("cmd") == "status":
                    response = {"ok": True, "monitor": endpoint.descriptor()}
                elif request.get("cmd") == "pause":
                    endpoint.pause_event.set()
                    paused = endpoint.paused_ack_event.wait(timeout=3)
                    endpoint.publish()
                    response = {
                        "ok": paused,
                        "delivery_paused": paused,
                        **(
                            {}
                            if paused
                            else {"error": "subscription did not pause in time"}
                        ),
                    }
                elif request.get("cmd") == "resume":
                    endpoint.pause_event.clear()
                    endpoint.paused_ack_event.clear()
                    endpoint.publish()
                    response = {"ok": True, "delivery_paused": False}
                else:
                    response = {"ok": False, "error": "unsupported command"}
                self.wfile.write(
                    (json.dumps(response, ensure_ascii=False) + "\n").encode(
                        "utf-8"
                    )
                )

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = False
            daemon_threads = True

        self.server = Server(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name=f"claude-monitor-control-{self.instance_id}",
            daemon=True,
        )
        self.thread.start()
        self.publish()

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        try:
            self.descriptor_path.unlink()
        except FileNotFoundError:
            pass
