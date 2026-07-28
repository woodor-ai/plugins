"""Manage the macOS launchd lifecycle for the central message hub."""

from __future__ import annotations

import errno
import fcntl
import os
import plistlib
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class LegacyMessageHubLaunchAgent:
    label: str
    plist_path: Path


def build_message_hub_launch_agent(
    *,
    label: str,
    message_hub_command: Path,
    log_path: Path,
    port: int = 8765,
) -> bytes:
    definition = {
        "Label": label,
        "ProgramArguments": [
            str(message_hub_command),
            "--port",
            str(port),
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
        "ProcessType": "Background",
    }
    return plistlib.dumps(definition)


def remove_legacy_message_hub_launch_agents(
    jobs: tuple[LegacyMessageHubLaunchAgent, ...],
    *,
    user_id: int | None = None,
) -> None:
    """Unload and remove exact service names from earlier layouts."""
    user_id = os.getuid() if user_id is None else user_id
    for job in jobs:
        subprocess.run(
            [
                "launchctl",
                "bootout",
                f"gui/{user_id}/{job.label}",
            ],
            capture_output=True,
        )
        try:
            job.plist_path.unlink()
        except FileNotFoundError:
            pass


def wait_until_message_hub_stopped(
    *,
    service_target: str,
    old_health: dict,
    health_probe: Callable[..., dict],
    health_instance_id: Callable[[dict], str],
    total: float = 10.0,
    interval: float = 0.25,
) -> bool:
    """Wait until launchd drops the job and the old endpoint disappears."""
    old_instance_id = health_instance_id(old_health)
    deadline = time.monotonic() + max(0.0, total)
    consecutive_clear = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            listed = subprocess.run(
                ["launchctl", "print", service_target],
                capture_output=True,
                timeout=max(0.001, remaining),
            ).returncode == 0
        except subprocess.TimeoutExpired:
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        health = health_probe(timeout=min(1.0, remaining))
        if old_instance_id:
            old_instance_present = (
                bool(health)
                and health_instance_id(health) == old_instance_id
            )
        else:
            old_instance_present = bool(health)
        if not listed and not old_instance_present:
            consecutive_clear += 1
            if consecutive_clear >= 2:
                return True
        else:
            consecutive_clear = 0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(interval, remaining))


def wait_for_new_message_hub(
    *,
    expected_version: str,
    old_instance_id: str,
    health_probe: Callable[..., dict],
    health_instance_id: Callable[[dict], str],
    health_version_matches: Callable[[dict, str], bool],
    total: float = 8.0,
    interval: float = 0.25,
    stable_checks: int = 2,
) -> bool:
    """Wait for one new, correctly versioned instance to stay healthy."""
    candidate_id = ""
    consecutive = 0
    deadline = time.monotonic() + max(0.0, total)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        health = health_probe(timeout=min(1.0, remaining))
        instance_id = health_instance_id(health)
        valid = bool(
            instance_id
            and instance_id != old_instance_id
            and health_version_matches(health, expected_version)
        )
        if valid:
            if instance_id == candidate_id:
                consecutive += 1
            else:
                candidate_id = instance_id
                consecutive = 1
            if consecutive >= stable_checks:
                return True
        else:
            candidate_id = ""
            consecutive = 0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(interval, remaining))


def ensure_message_hub_launch_agent(
    *,
    plist_path: Path,
    lock_path: Path,
    label: str,
    message_hub_command: Path,
    log_path: Path,
    remove_legacy_jobs: Callable[[], None],
    install_locked: Callable[[bytes], None],
    log: Callable[[str], None],
    persistent_log: Callable[[str], None],
    lock_timeout: float = 30.0,
) -> None:
    """Serialize creation and health repair of the launchd service."""
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    remove_legacy_jobs()
    if not message_hub_command.exists():
        message = f"central am-msgd script missing: {message_hub_command}"
        log(message)
        persistent_log(message)
        return

    definition = build_message_hub_launch_agent(
        label=label,
        message_hub_command=message_hub_command,
        log_path=log_path,
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w", encoding="utf-8") as lock_file:
        deadline = time.monotonic() + lock_timeout
        while True:
            try:
                fcntl.flock(
                    lock_file,
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.monotonic() >= deadline:
                    message = (
                        "ensure_launchd: lock timeout "
                        f"({lock_timeout:g}s), skipping launchd operation"
                    )
                    log(message)
                    persistent_log(message)
                    return
                time.sleep(0.5)
        try:
            install_locked(definition)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def install_message_hub_launch_agent_locked(
    *,
    launch_agent_bytes: bytes,
    plist_path: Path,
    label: str,
    expected_version: str,
    health_probe: Callable[..., dict],
    health_instance_id: Callable[[dict], str],
    health_version_matches: Callable[[dict, str], bool],
    stop_bootstrap_message_hub: Callable[[], None],
    wait_until_stopped: Callable[[str, dict], bool],
    wait_for_new: Callable[[str, str], bool],
    log: Callable[[str], None],
    persistent_log: Callable[[str], None],
) -> str:
    """Install or repair one launchd service while the caller holds its lock.

    Returns a warning string only when launchd could not establish a healthy
    service. An empty string means the service is healthy.
    """
    old_bytes = plist_path.read_bytes() if plist_path.exists() else b""
    plist_changed = launch_agent_bytes != old_bytes
    if plist_changed:
        plist_path.write_bytes(launch_agent_bytes)

    user_id = os.getuid()
    domain_target = f"gui/{user_id}"
    service_target = f"{domain_target}/{label}"
    subprocess.run(
        ["launchctl", "enable", service_target],
        capture_output=True,
    )
    listed = subprocess.run(
        ["launchctl", "print", service_target],
        capture_output=True,
    ).returncode == 0

    old_health = health_probe()
    if listed and not plist_changed:
        if (
            health_version_matches(old_health, expected_version)
            and health_instance_id(old_health)
        ):
            log(f"launchd already manages {label} (healthy)")
            return ""
        if old_health:
            if health_version_matches(old_health, expected_version):
                persistent_log(
                    "launchd am-msgd has no instance_id, restarting"
                )
            else:
                persistent_log(
                    "launchd am-msgd version mismatch "
                    f"(running={old_health.get('version') or 'unknown'}, "
                    f"installed={expected_version}), restarting"
                )
        else:
            persistent_log(
                "launchd listed but /health unreachable, "
                "entering self-heal path"
            )

    if listed:
        subprocess.run(
            ["launchctl", "bootout", service_target],
            capture_output=True,
        )

    stop_bootstrap_message_hub()
    if not wait_until_stopped(service_target, old_health):
        warning = (
            "central am-msgd did not stop cleanly; "
            "refusing to start a second instance"
        )
        log(warning)
        persistent_log(f"FAIL: {warning}")
        return warning

    def bootstrap() -> bool:
        result = subprocess.run(
            [
                "launchctl",
                "bootstrap",
                domain_target,
                str(plist_path),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return True
        fallback = subprocess.run(
            ["launchctl", "load", "-w", str(plist_path)],
            capture_output=True,
            text=True,
        )
        return fallback.returncode == 0

    bootstrap()
    previous_instance_id = health_instance_id(old_health)
    if wait_for_new(expected_version, previous_instance_id):
        message = (
            f"launchd loaded {label}"
            "（auto-start on boot，KeepAlive on）"
        )
        log(message)
        persistent_log(message)
        return ""

    for attempt in range(1, 3):
        persistent_log(
            "post-bootstrap central am-msgd unhealthy, "
            f"self-heal retry #{attempt}"
        )
        retry_health = health_probe()
        subprocess.run(
            ["launchctl", "bootout", service_target],
            capture_output=True,
        )
        if not wait_until_stopped(service_target, retry_health):
            persistent_log(
                f"self-heal retry #{attempt}: previous instance did not stop"
            )
            continue
        bootstrap()
        if wait_for_new(
            expected_version,
            health_instance_id(retry_health),
        ):
            message = (
                f"launchd loaded {label} "
                f"(self-heal #{attempt} succeeded)"
            )
            log(message)
            persistent_log(message)
            return ""

    warning = (
        "central am-msgd failed to start automatically; "
        "run `meeting am-msgd restart` or check "
        "~/.agent-meeting/logs/bootstrap.log"
    )
    log(warning)
    persistent_log(f"FAIL: {warning}")
    return warning


def control_message_hub_launch_agent(
    action: str,
    *,
    label: str,
    plist_path: Path,
    port: int,
    host_is_enabled: bool,
    health_probe: Callable[[], dict | None],
) -> None:
    """Implement ``meeting am-msgd`` status/stop/restart on macOS."""
    user_id = os.getuid()
    domain = f"gui/{user_id}"
    target = f"{domain}/{label}"

    if action == "status":
        result = subprocess.run(
            ["launchctl", "print", target],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(
                f"central am-msgd: not registered with launchd ({label})"
            )
            print(
                f"plist on disk: {'yes' if plist_path.exists() else 'no'}"
            )
            return
        keys = ("state =", "pid =", "program =", "path =")
        for line in result.stdout.splitlines():
            line = line.strip()
            if any(key in line for key in keys):
                print(line)
        if host_is_enabled and health_probe() is None:
            print(
                "\nThis machine is configured as control but central "
                f"am-msgd is not listening on :{port}. "
                "Fix: run 'meeting am-msgd restart' if launchctl "
                "self-heal did not fire, or reopen the Claude session "
                "to trigger auto-launch. "
                "See ~/.agent-meeting/logs/bootstrap.log"
            )
        return

    if action == "stop":
        result = subprocess.run(
            ["launchctl", "bootout", target],
            capture_output=True,
            text=True,
        )
        already_stopped = (
            "Could not find" in (result.stderr or "")
            or "No such process" in (result.stderr or "")
        )
        if result.returncode != 0 and not already_stopped:
            print(
                f"stop failed: {result.stderr.strip()}",
                file=sys.stderr,
            )
            raise SystemExit(1)
        for _attempt in range(20):
            time.sleep(0.2)
            query = subprocess.run(
                ["launchctl", "print", target],
                capture_output=True,
            )
            if query.returncode != 0:
                break
        print(f"central am-msgd stopped: {label}")
        print(
            "(note: next Claude SessionStart with is_host=true will "
            "reinstall + restart it)"
        )
        return

    if action == "restart":
        loaded = subprocess.run(
            ["launchctl", "print", target],
            capture_output=True,
        ).returncode == 0
        if loaded:
            result = subprocess.run(
                ["launchctl", "kickstart", "-k", target],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                print(f"central am-msgd restarted: {label}")
                return
            print(
                f"kickstart failed: {result.stderr.strip()}",
                file=sys.stderr,
            )
            raise SystemExit(1)
        if not plist_path.exists():
            print(f"plist missing: {plist_path}", file=sys.stderr)
            print(
                "Run a Claude SessionStart with is_host=true to install it "
                "first.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        result = subprocess.run(
            [
                "launchctl",
                "bootstrap",
                domain,
                str(plist_path),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print(f"central am-msgd started: {label}")
            return
        print(
            f"bootstrap failed: {result.stderr.strip()}",
            file=sys.stderr,
        )
        raise SystemExit(1)
