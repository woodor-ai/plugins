"""In-memory state for one foreground mycodex session lease."""

from __future__ import annotations

import asyncio
from collections import OrderedDict


class SessionLease:
    def __init__(
        self,
        *,
        launch_id,
        name,
        project,
        cwd,
        control_url,
        thread_id,
        cursor,
        proxy_host: str = "127.0.0.1",
    ):
        self.launch_id = launch_id
        self.name = name
        self.project = project
        self.cwd = cwd
        self.control_url = control_url.rstrip("/")
        self.thread_id = thread_id
        self.cursor = int(cursor) if cursor is not None else None
        self.proxy_host = proxy_host
        self.pending = OrderedDict()
        self.awaiting_ack = None
        self.subscription_task = None
        self.proxy_server = None
        self.proxy_port = None
        self.proxy_clients = set()
        self.active = True
        self.central_registered = False
        self.central_error = None
        self.central_register_task = None
        self.delivery_lock = asyncio.Lock()

    @property
    def identity(self):
        return f"{self.name}@{self.project}"

    @property
    def proxy_url(self):
        if self.proxy_port is None:
            raise RuntimeError("session proxy is not running")
        return f"ws://{self.proxy_host}:{self.proxy_port}"
