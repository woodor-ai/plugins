"""Single-process management of multiple ``am-msgd`` HTTP listeners."""

from __future__ import annotations

import ipaddress
import socket
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from agent_meeting.message_hub import service_configuration


class _IPv6ThreadingHTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6


class ListenerManager:
    def __init__(
        self,
        *,
        handler_class,
        configuration_path: Path,
        configuration: service_configuration.MessageHubServiceConfiguration,
        plugin_version: str,
        publish_mdns,
    ):
        self.handler_class = handler_class
        self.configuration_path = configuration_path
        self.configuration = configuration
        self.plugin_version = plugin_version
        self.publish_mdns = publish_mdns
        self._listeners: dict[str, tuple[ThreadingHTTPServer, threading.Thread]] = {}
        self._errors: dict[str, str] = {}
        self._lock = threading.RLock()
        self._zeroconf = None
        self._service_info = None
        self._retry_stop = threading.Event()
        self._retry_thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self.configuration.port

    def _server_class(self, address: str):
        if ipaddress.ip_address(address).version == 6:
            return _IPv6ThreadingHTTPServer
        return ThreadingHTTPServer

    def _start_listener(self, address: str) -> None:
        if address in self._listeners:
            return
        server_class = self._server_class(address)
        server_class.allow_reuse_address = True
        server_class.daemon_threads = True
        server = server_class((address, self.port), self.handler_class)
        thread = threading.Thread(
            target=server.serve_forever,
            name=f"am-msgd-listener-{address}-{self.port}",
            daemon=True,
        )
        thread.start()
        self._listeners[address] = (server, thread)
        self._errors.pop(address, None)

    def start_all(self) -> None:
        with self._lock:
            ordered = sorted(
                self.configuration.binds,
                key=lambda item: not ipaddress.ip_address(item).is_loopback,
            )
            for address in ordered:
                try:
                    self._start_listener(address)
                except OSError as error:
                    self._errors[address] = str(error)
                    if ipaddress.ip_address(address).is_loopback:
                        self.shutdown()
                        raise
            self._reconcile_mdns()
            self._retry_thread = threading.Thread(
                target=self._retry_failed_listeners,
                name="am-msgd-listener-retry",
                daemon=True,
            )
            self._retry_thread.start()

    def add(self, address: str) -> dict:
        with self._lock:
            updated = service_configuration.with_added_bind(
                self.configuration,
                address,
            )
            address = service_configuration.normalize_ip(address)
            if updated is self.configuration:
                return self.snapshot()
            try:
                self._start_listener(address)
            except OSError as error:
                self._errors.pop(address, None)
                raise ValueError(
                    f"cannot bind {address}:{self.port}: {error}"
                ) from error
            try:
                service_configuration.write(self.configuration_path, updated)
            except Exception:
                self._stop_listener(address)
                raise
            self.configuration = updated
            self._reconcile_mdns()
            return self.snapshot()

    def _retry_failed_listeners(self) -> None:
        while not self._retry_stop.wait(5):
            with self._lock:
                pending = [
                    address
                    for address in self.configuration.binds
                    if address not in self._listeners
                ]
                for address in pending:
                    try:
                        self._start_listener(address)
                    except OSError as error:
                        self._errors[address] = str(error)
                self._reconcile_mdns()

    def remove(self, address: str) -> dict:
        with self._lock:
            updated = service_configuration.with_removed_bind(
                self.configuration,
                address,
            )
            address = service_configuration.normalize_ip(address)
            service_configuration.write(self.configuration_path, updated)
            self.configuration = updated
            self._stop_listener(address)
            self._errors.pop(address, None)
            self._reconcile_mdns()
            return self.snapshot()

    def set_local_only(self) -> dict:
        with self._lock:
            updated = service_configuration.local_only(self.configuration)
            service_configuration.write(self.configuration_path, updated)
            removed = set(self.configuration.binds) - set(updated.binds)
            self.configuration = updated
            for address in removed:
                self._stop_listener(address)
                self._errors.pop(address, None)
            self._reconcile_mdns()
            return self.snapshot()

    def _stop_listener(self, address: str) -> None:
        item = self._listeners.pop(address, None)
        if not item:
            return
        server, thread = item
        server.shutdown()
        server.server_close()
        if thread is not threading.current_thread():
            thread.join(timeout=2)

    def _has_lan_listener(self) -> bool:
        return any(
            not ipaddress.ip_address(address).is_loopback
            for address in self._listeners
        )

    def _reconcile_mdns(self) -> None:
        should_publish = (
            self.configuration.mdns == "auto"
            and self._has_lan_listener()
        )
        if should_publish and self._zeroconf is None:
            self._zeroconf, self._service_info = self.publish_mdns(
                self.port,
                self.plugin_version,
            )
        elif not should_publish and self._zeroconf is not None:
            try:
                self._zeroconf.unregister_service(self._service_info)
                self._zeroconf.close()
            finally:
                self._zeroconf = None
                self._service_info = None

    def snapshot(self) -> dict:
        with self._lock:
            configured = [
                self._format_listener(address)
                for address in self.configuration.binds
            ]
            active = [
                self._format_listener(address)
                for address in self.configuration.binds
                if address in self._listeners
            ]
            errors = {
                self._format_listener(address): message
                for address, message in self._errors.items()
            }
            return {
                "configured_listeners": configured,
                "active_listeners": active,
                "listener_errors": errors,
                "mdns": (
                    "advertising"
                    if self._zeroconf is not None
                    else "off"
                ),
            }

    def _format_listener(self, address: str) -> str:
        if ipaddress.ip_address(address).version == 6:
            return f"[{address}]:{self.port}"
        return f"{address}:{self.port}"

    def shutdown(self) -> None:
        with self._lock:
            self._retry_stop.set()
            if self._zeroconf is not None:
                try:
                    self._zeroconf.unregister_service(self._service_info)
                    self._zeroconf.close()
                except Exception:
                    pass
                self._zeroconf = None
                self._service_info = None
            for address in tuple(self._listeners):
                self._stop_listener(address)
