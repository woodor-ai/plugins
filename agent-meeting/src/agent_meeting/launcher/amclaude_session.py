"""Supervise one Claude Code CLI process in its original terminal.

The wrapper deliberately contains no subscription/API selection policy. It
keeps the original terminal attached, publishes a small local descriptor for
``am-ctld``, and supports status, two-interrupt exit, and in-place restart over
an authenticated loopback control socket.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import signal
import socketserver
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from agent_meeting.lifecycle_control.terminals import current_terminal_handle
from agent_meeting.messaging import project_identity


CLAUDE_MODELS = ("fable-5", "opus-5", "sonnet-5")
CLAUDE_EFFORTS = ("ultracode", "max", "extra", "high", "medium")


def build_claude_launch_cmd(
    claude_args: list[str],
    *,
    model: str = "opus-5",
    effort: str = "high",
) -> list[str]:
    """Build the managed Claude invocation with its session settings."""
    return ["claude", "--model", model, "--effort", effort, *claude_args]


def _meeting_home() -> Path:
    return Path(
        os.environ.get("MEETING_HOME") or (Path.home() / ".agent-meeting")
    )


def _atomic_json(path: Path, payload: dict) -> None:
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


def _tty_name() -> str | None:
    try:
        if sys.stdin.isatty():
            return os.ttyname(sys.stdin.fileno())
    except OSError:
        pass
    return None


class ClaudeSupervisor:
    def __init__(
        self,
        claude_args: list[str],
        *,
        name: str,
        project: str | None,
        model: str = "opus-5",
        effort: str = "high",
    ):
        self.claude_args = list(claude_args)
        self.name = name
        self.project = project
        self.model = model
        self.effort = effort
        self.instance_id = uuid.uuid4().hex
        self.auth_token = secrets.token_urlsafe(32)
        self.started_at = int(time.time())
        self.process: subprocess.Popen | None = None
        self.restart_requested = False
        self.stop_requested = False
        self.lock = threading.RLock()
        self.server: socketserver.ThreadingTCPServer | None = None
        self.server_thread: threading.Thread | None = None
        self.descriptor_path = (
            _meeting_home()
            / "control"
            / "wrappers"
            / f"amclaude-{self.instance_id}.json"
        )

    def descriptor(self) -> dict:
        with self.lock:
            process = self.process
            return {
                "schema_version": 1,
                "wrapper": "amclaude",
                "platform": "claude",
                "name": self.name,
                "project": self.project,
                "identity": (
                    f"{self.name}@{self.project}" if self.project else None
                ),
                "instance_id": self.instance_id,
                "wrapper_pid": os.getpid(),
                "child_pid": process.pid if process and process.poll() is None else None,
                "cwd": os.getcwd(),
                "tty": _tty_name(),
                "terminal_handle": {
                    **current_terminal_handle(),
                    "tty": _tty_name(),
                },
                "started_at": self.started_at,
                "status": (
                    "restarting"
                    if self.restart_requested
                    else "stopping"
                    if self.stop_requested
                    else "running"
                    if process and process.poll() is None
                    else "idle"
                ),
                "control": {
                    "host": "127.0.0.1",
                    "port": self.server.server_address[1] if self.server else None,
                    "token": self.auth_token,
                },
                "launch_recipe": {
                    "command": "claude",
                    "args_count": len(build_claude_launch_cmd(
                        self.claude_args,
                        model=self.model,
                        effort=self.effort,
                    )) - 1,
                    "args_sha256": hashlib.sha256(
                        "\0".join(
                            build_claude_launch_cmd(
                                self.claude_args,
                                model=self.model,
                                effort=self.effort,
                            )[1:]
                        ).encode("utf-8")
                    ).hexdigest(),
                    "args_persisted": False,
                },
                "capabilities": [
                    "observe",
                    "interrupt",
                    "exit",
                    "restart_same_terminal",
                    *(
                        ["send_text"]
                        if current_terminal_handle().get("type")
                        in {"tmux", "iterm2"}
                        else []
                    ),
                ],
            }

    def publish(self) -> None:
        _atomic_json(self.descriptor_path, self.descriptor())

    def interrupt(self, count: int = 2) -> bool:
        with self.lock:
            process = self.process
        if process is None or process.poll() is not None:
            return False
        delivered = False
        for index in range(max(1, count)):
            try:
                if os.name == "nt":
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    process.send_signal(signal.SIGINT)
                delivered = True
            except (OSError, ProcessLookupError):
                if not delivered:
                    return False
                break
            if index + 1 < count:
                time.sleep(0.2)
        return delivered

    def request_exit(self) -> bool:
        with self.lock:
            self.stop_requested = True
            self.restart_requested = False
            process = self.process
        self.publish()
        delivered = self.interrupt(2)
        if not delivered:
            return False
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process is None or process.poll() is not None:
                return True
            time.sleep(0.1)
        return False

    def request_restart(self) -> bool:
        with self.lock:
            self.restart_requested = True
            self.stop_requested = False
            previous = self.process
            previous_pid = previous.pid if previous is not None else None
        self.publish()
        delivered = self.interrupt(2)
        if not delivered:
            return False
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            with self.lock:
                current = self.process
            if (
                current is not None
                and current.pid != previous_pid
                and current.poll() is None
            ):
                return True
            time.sleep(0.1)
        return False

    def _serve(self) -> None:
        supervisor = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                try:
                    request = json.loads(self.rfile.readline().decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return
                if request.get("token") != supervisor.auth_token:
                    response = {"ok": False, "error": "unauthorized"}
                elif request.get("cmd") == "status":
                    response = {"ok": True, "session": supervisor.descriptor()}
                elif request.get("cmd") == "exit":
                    response = {"ok": supervisor.request_exit()}
                elif request.get("cmd") == "restart":
                    response = {"ok": supervisor.request_restart()}
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
        self.server_thread = threading.Thread(
            target=self.server.serve_forever,
            name=f"amclaude-control-{self.instance_id}",
            daemon=True,
        )
        self.server_thread.start()

    def _spawn(self) -> subprocess.Popen:
        command = build_claude_launch_cmd(
            self.claude_args,
            model=self.model,
            effort=self.effort,
        )
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        return subprocess.Popen(command, **kwargs)

    def run(self) -> int:
        self._serve()
        exit_code = 0
        try:
            while True:
                with self.lock:
                    self.restart_requested = False
                    self.process = self._spawn()
                self.publish()
                exit_code = self.process.wait()
                with self.lock:
                    self.process = None
                    should_restart = self.restart_requested and not self.stop_requested
                self.publish()
                if not should_restart:
                    break
        except KeyboardInterrupt:
            self.request_exit()
            with self.lock:
                process = self.process
            if process is not None:
                try:
                    exit_code = process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    exit_code = process.wait(timeout=5)
        finally:
            if self.server:
                self.server.shutdown()
                self.server.server_close()
            try:
                self.descriptor_path.unlink()
            except FileNotFoundError:
                pass
        return exit_code


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="amclaude",
        add_help=False,
        description=(
            "Run Claude Code under the agent-meeting lifecycle wrapper. "
            "Wrapper options precede `--`; remaining arguments go to claude."
        ),
    )
    parser.add_argument("name", nargs="?", default=None)
    parser.add_argument("--proj", default=None)
    parser.add_argument("--global", dest="is_global", action="store_true")
    parser.add_argument(
        "--model",
        choices=CLAUDE_MODELS,
        default="opus-5",
        help="Claude model variant (default: opus-5)",
    )
    parser.add_argument(
        "--effort",
        choices=CLAUDE_EFFORTS,
        default="high",
        help="reasoning effort (default: high)",
    )
    parser.add_argument(
        "--amclaude-help",
        action="store_true",
        help="show wrapper help; all other arguments are passed to claude",
    )
    known, claude_args = parser.parse_known_args(argv)
    if known.amclaude_help:
        parser.print_help()
        return 0
    if shutil.which("claude") is None:
        print("amclaude: `claude` was not found on PATH", file=sys.stderr)
        return 127
    if known.proj is not None and known.is_global:
        print("amclaude: --proj and --global are mutually exclusive", file=sys.stderr)
        return 2
    host = os.uname().nodename.split(".")[0] if hasattr(os, "uname") else "host"
    name = known.name or f"claude-{host}"[:20]
    project = "*" if known.is_global else known.proj
    if project is None:
        project = project_identity.resolve_authoritative_project(
            os.getcwd(),
            None,
            meeting_home=str(_meeting_home()),
        )
    elif project != "*":
        try:
            project = project_identity.validate_project(project)
        except ValueError as error:
            print(f"amclaude: {error}", file=sys.stderr)
            return 2
    try:
        return ClaudeSupervisor(
            claude_args,
            name=name,
            project=project,
            model=known.model,
            effort=known.effort,
        ).run()
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
