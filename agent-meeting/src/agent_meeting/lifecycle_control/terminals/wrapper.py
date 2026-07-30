"""Control an amclaude/amcodex foreground wrapper over loopback IPC."""

from __future__ import annotations

import json
import socket

from .base import TerminalAdapter, TerminalCapabilities


class WrapperTerminalAdapter(TerminalAdapter):
    def capabilities(self, handle: dict) -> TerminalCapabilities:
        available = bool(handle.get("port") and handle.get("token"))
        return TerminalCapabilities(
            can_interrupt=available,
            can_restart_in_place=available,
        )

    def _request(self, handle: dict, command: str) -> dict:
        port = handle.get("port")
        token = handle.get("token")
        if not port or not token:
            raise ValueError("wrapper control handle is incomplete")
        request = json.dumps({"cmd": command, "token": token}) + "\n"
        with socket.create_connection(("127.0.0.1", int(port)), timeout=3) as client:
            client.sendall(request.encode("utf-8"))
            response = client.makefile("rb").readline()
        return json.loads(response.decode("utf-8"))

    def send_interrupt(self, handle: dict, count: int = 2) -> bool:
        # The wrapper contract defines exit as exactly two interrupts. Counts
        # other than two are intentionally not exposed by its public protocol.
        if count != 2:
            raise ValueError("wrapper interrupt count must be exactly 2")
        return bool(self._request(handle, "exit").get("ok"))

    def restart_in_place(self, handle: dict) -> bool:
        return bool(self._request(handle, "restart").get("ok"))

    def pause_delivery(self, handle: dict) -> bool:
        return bool(self._request(handle, "pause").get("ok"))

    def resume_delivery(self, handle: dict) -> bool:
        return bool(self._request(handle, "resume").get("ok"))
