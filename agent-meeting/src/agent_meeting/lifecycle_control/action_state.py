"""Crash-safe lifecycle action state and automation failure protection."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path


ACTIVE_STATUSES = {
    "pausing_ingress",
    "maintenance",
    "verifying",
    "resuming_ingress",
    "exiting",
    "restarting",
}


class ActionStateStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.payload = self._load()
        self._recover_interrupted()

    def _load(self) -> dict:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload.get("records"), dict):
            payload["records"] = {}
        payload["schema_version"] = 1
        return payload

    @staticmethod
    def _key(instance_id: str, command: str) -> str:
        return f"{instance_id}:{command}"

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self.payload, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, self.path)

    def _recover_interrupted(self) -> None:
        changed = False
        now = int(time.time())
        for record in self.payload["records"].values():
            if record.get("status") not in ACTIVE_STATUSES:
                continue
            record.update(
                {
                    "status": "failed",
                    "failed_at": now,
                    "error_type": "ControllerRestarted",
                    "consecutive_failures": int(
                        record.get("consecutive_failures") or 0
                    )
                    + 1,
                }
            )
            changed = True
        if changed:
            self._write()

    def transition(
        self,
        instance_id: str,
        identity: str,
        command: str,
        status: str,
        *,
        automatic: bool,
    ) -> None:
        with self.lock:
            key = self._key(instance_id, command)
            previous = self.payload["records"].get(key) or {}
            now = int(time.time())
            self.payload["records"][key] = {
                **previous,
                "instance_id": instance_id,
                "identity": identity,
                "command": command,
                "status": status,
                "automatic": automatic,
                "updated_at": now,
                **({"started_at": now} if status in ACTIVE_STATUSES else {}),
            }
            self._write()

    def complete(
        self,
        instance_id: str,
        identity: str,
        command: str,
        *,
        automatic: bool,
    ) -> None:
        with self.lock:
            self.transition(
                instance_id,
                identity,
                command,
                "completed",
                automatic=automatic,
            )
            record = self.payload["records"][self._key(instance_id, command)]
            record["completed_at"] = int(time.time())
            record["consecutive_failures"] = 0
            record.pop("error_type", None)
            self._write()

    def fail(
        self,
        instance_id: str,
        identity: str,
        command: str,
        error_type: str,
        *,
        automatic: bool,
    ) -> None:
        with self.lock:
            key = self._key(instance_id, command)
            previous = self.payload["records"].get(key) or {}
            failures = int(previous.get("consecutive_failures") or 0) + 1
            self.transition(
                instance_id,
                identity,
                command,
                "failed",
                automatic=automatic,
            )
            record = self.payload["records"][key]
            record.update(
                {
                    "failed_at": int(time.time()),
                    "error_type": error_type,
                    "consecutive_failures": failures,
                }
            )
            self._write()

    def automation_block_reason(
        self,
        instance_id: str,
        command: str,
        *,
        cooldown_seconds: int,
        max_consecutive_failures: int,
    ) -> str | None:
        with self.lock:
            record = self.payload["records"].get(
                self._key(instance_id, command)
            )
            if not record:
                return None
            failures = int(record.get("consecutive_failures") or 0)
            if failures >= max_consecutive_failures:
                return (
                    f"{failures} consecutive failures reached configured "
                    f"maximum {max_consecutive_failures}"
                )
            updated_at = int(record.get("updated_at") or 0)
            remaining = updated_at + cooldown_seconds - int(time.time())
            if remaining > 0:
                return f"action cooldown has {remaining}s remaining"
            return None
