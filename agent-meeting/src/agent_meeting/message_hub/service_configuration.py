"""Persistent configuration for the local ``am-msgd`` service."""

from __future__ import annotations

import ipaddress
import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


DEFAULT_PORT = 8765
DEFAULT_BINDS = ("127.0.0.1",)
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class MessageHubServiceConfiguration:
    enabled: bool = True
    port: int = DEFAULT_PORT
    binds: tuple[str, ...] = DEFAULT_BINDS
    mdns: str = "auto"

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "enabled": self.enabled,
            "port": self.port,
            "binds": list(self.binds),
            "mdns": self.mdns,
        }


def default_path(meeting_home: Path) -> Path:
    return meeting_home / "am-msgd.json"


def normalize_ip(value: str) -> str:
    value = str(value).strip()
    try:
        return str(ipaddress.ip_address(value))
    except ValueError as error:
        raise ValueError(f"invalid bind IP: {value}") from error


def _normalize_binds(values) -> tuple[str, ...]:
    output: list[str] = []
    for value in values or DEFAULT_BINDS:
        normalized = normalize_ip(value)
        if normalized not in output:
            output.append(normalized)
    if not output:
        output.append(DEFAULT_BINDS[0])
    if "127.0.0.1" not in output and "0.0.0.0" not in output:
        raise ValueError(
            "127.0.0.1 must remain configured for local administration"
        )
    return tuple(output)


def from_dict(payload: dict) -> MessageHubServiceConfiguration:
    port = int(payload.get("port", DEFAULT_PORT))
    if not 1 <= port <= 65535:
        raise ValueError(f"invalid am-msgd port: {port}")
    mdns = str(payload.get("mdns", "auto"))
    if mdns not in {"auto", "off"}:
        raise ValueError(f"invalid am-msgd mdns mode: {mdns}")
    return MessageHubServiceConfiguration(
        enabled=bool(payload.get("enabled", True)),
        port=port,
        binds=_normalize_binds(payload.get("binds")),
        mdns=mdns,
    )


def load(path: Path, *, create: bool = False) -> MessageHubServiceConfiguration:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        configuration = MessageHubServiceConfiguration()
        if create:
            write(path, configuration)
        return configuration
    except (OSError, ValueError) as error:
        raise ValueError(f"cannot read am-msgd config {path}: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"am-msgd config must be a JSON object: {path}")
    return from_dict(payload)


def write(path: Path, configuration: MessageHubServiceConfiguration) -> None:
    configuration = from_dict(configuration.to_dict())
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                configuration.to_dict(),
                handle,
                indent=2,
                ensure_ascii=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def locked(path: Path):
    """Serialize stopped-daemon configuration edits."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def with_enabled(
    configuration: MessageHubServiceConfiguration,
    enabled: bool,
) -> MessageHubServiceConfiguration:
    return MessageHubServiceConfiguration(
        enabled=enabled,
        port=configuration.port,
        binds=configuration.binds,
        mdns=configuration.mdns,
    )


def with_added_bind(
    configuration: MessageHubServiceConfiguration,
    address: str,
) -> MessageHubServiceConfiguration:
    address = normalize_ip(address)
    if address in configuration.binds:
        return configuration
    if address != "0.0.0.0" and "0.0.0.0" in configuration.binds:
        return configuration
    if address == "0.0.0.0" and any(
        ipaddress.ip_address(item).version == 4
        for item in configuration.binds
    ):
        raise ValueError(
            "cannot add 0.0.0.0 without replacing active IPv4 listeners; "
            "edit the config and run `am-msgd restart`"
        )
    return MessageHubServiceConfiguration(
        enabled=configuration.enabled,
        port=configuration.port,
        binds=configuration.binds + (address,),
        mdns=configuration.mdns,
    )


def with_removed_bind(
    configuration: MessageHubServiceConfiguration,
    address: str,
) -> MessageHubServiceConfiguration:
    address = normalize_ip(address)
    binds = tuple(item for item in configuration.binds if item != address)
    if "127.0.0.1" not in binds and "0.0.0.0" not in binds:
        raise ValueError(
            "127.0.0.1 must remain configured for local administration"
        )
    return MessageHubServiceConfiguration(
        enabled=configuration.enabled,
        port=configuration.port,
        binds=binds,
        mdns=configuration.mdns,
    )


def local_only(
    configuration: MessageHubServiceConfiguration,
) -> MessageHubServiceConfiguration:
    binds = tuple(
        item
        for item in configuration.binds
        if ipaddress.ip_address(item).is_loopback
    )
    if not binds:
        binds = DEFAULT_BINDS
    return MessageHubServiceConfiguration(
        enabled=configuration.enabled,
        port=configuration.port,
        binds=binds,
        mdns=configuration.mdns,
    )
