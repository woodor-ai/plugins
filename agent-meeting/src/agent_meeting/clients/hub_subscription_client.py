"""RFC 6455 subscription client for message-hub notifications."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import select
import socket
import struct
import time


def websocket_key() -> tuple[str, str]:
    raw_key = base64.b64encode(os.urandom(16)).decode()
    accept = base64.b64encode(
        hashlib.sha1(
            (raw_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
        ).digest()
    ).decode()
    return raw_key, accept


def send_masked_frame(sock: socket.socket, opcode: int, payload: bytes) -> None:
    mask = os.urandom(4)
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    length = len(payload)
    if length < 126:
        header = struct.pack("!BB", 0x80 | opcode, 0x80 | length)
    elif length < 65536:
        header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, length)
    else:
        header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, length)
    sock.sendall(header + mask + masked)


def receive_exact(sock: socket.socket, size: int) -> bytes:
    buffer = b""
    while len(buffer) < size:
        chunk = sock.recv(size - len(buffer))
        if not chunk:
            raise IOError("EOF")
        buffer += chunk
    return buffer


def read_frame(sock: socket.socket) -> tuple[int, bytes]:
    header = receive_exact(sock, 2)
    first, second = header
    if not first & 0x80:
        raise IOError("fragmented frame not supported")

    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", receive_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", receive_exact(sock, 8))[0]

    mask_key = receive_exact(sock, 4) if masked else b""
    payload = receive_exact(sock, length) if length else b""
    if masked:
        payload = bytes(
            byte ^ mask_key[index % 4] for index, byte in enumerate(payload)
        )
    return opcode, payload


class HubSubscriptionClient:
    """Reconnectable message-hub subscription with ping/pong keepalive."""

    PING_INTERVAL = 5
    DEAD_TIMEOUT = 15
    BACKOFF_BASE = 1.0
    BACKOFF_MAX = 30.0
    BACKOFF_JITTER = 0.20

    def __init__(
        self,
        *,
        self_name,
        project,
        resolve_addr,
        read_token,
        on_text,
        instance=None,
        on_connect=None,
        pause_event=None,
        paused_ack_event=None,
        log=None,
    ):
        self.self_name = self_name
        self.project = project
        self.resolve_addr = resolve_addr
        self.read_token = read_token
        self.on_text = on_text
        self.instance = instance
        self.on_connect = on_connect
        self.pause_event = pause_event
        self.paused_ack_event = paused_ack_event
        self.log = log or (lambda message: None)

    def _connect(self):
        address = self.resolve_addr()
        if not address:
            return None
        ip, port = address

        try:
            sock = socket.create_connection((ip, port), timeout=10)
        except Exception as error:
            self.log(f"ws connect failed ({ip}:{port}): {error}")
            return None

        key, expected_accept = websocket_key()
        headers = [
            "GET /subscribe HTTP/1.1",
            f"Host: {ip}:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
            f"X-Meeting-Name: {self.self_name}",
            f"X-Meeting-Project: {self.project()}",
            "X-Meeting-Proto: 1",
        ]
        instance = self.instance() if callable(self.instance) else self.instance
        if instance:
            headers.append(f"X-Meeting-Instance: {instance}")
        token = self.read_token()
        if token:
            headers.append(f"Authorization: Bearer {token}")

        try:
            sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode())
            status_line = self._read_http_line(sock)
            response_headers = {}
            while True:
                header_line = self._read_http_line(sock)
                if not header_line:
                    break
                if ":" in header_line:
                    key_name, _, value = header_line.partition(":")
                    response_headers[key_name.strip().lower()] = value.strip()

            if "101" not in status_line:
                raise IOError(f"WS handshake rejected: {status_line}")
            received_accept = response_headers.get("sec-websocket-accept", "")
            if received_accept != expected_accept:
                raise IOError(
                    f"Sec-WebSocket-Accept mismatch: {received_accept!r}"
                )
            sock.settimeout(None)
            return sock
        except Exception as error:
            self.log(f"ws handshake failed: {error}")
            try:
                sock.close()
            except Exception:
                pass
            return None

    @staticmethod
    def _read_http_line(sock: socket.socket) -> str:
        line = b""
        while not line.endswith(b"\r\n"):
            char = sock.recv(1)
            if not char:
                raise IOError("connection closed during handshake")
            line += char
        return line.decode().strip()

    def _jitter_delay(self, backoff: float) -> float:
        jitter = random.uniform(
            1 - self.BACKOFF_JITTER,
            1 + self.BACKOFF_JITTER,
        )
        return min(backoff * jitter, self.BACKOFF_MAX)

    def _wait_for_retry(self, delay: float) -> None:
        if self.pause_event is None:
            time.sleep(delay)
            return
        if self.pause_event.wait(delay) and self.paused_ack_event is not None:
            self.paused_ack_event.set()

    def run_forever(self) -> None:
        backoff = self.BACKOFF_BASE
        while True:
            if self.pause_event is not None and self.pause_event.is_set():
                if self.paused_ack_event is not None:
                    self.paused_ack_event.set()
                time.sleep(0.1)
                continue
            sock = self._connect()
            if sock is None:
                delay = self._jitter_delay(backoff)
                self.log(f"reconnect in {delay:.1f}s")
                self._wait_for_retry(delay)
                backoff = min(backoff * 2, self.BACKOFF_MAX)
                continue

            backoff = self.BACKOFF_BASE
            if self.on_connect:
                self.on_connect()

            disconnected = False
            last_frame_time = time.time()
            last_ping_time = time.time()
            while not disconnected:
                if self.pause_event is not None and self.pause_event.is_set():
                    if self.paused_ack_event is not None:
                        self.paused_ack_event.set()
                    break
                try:
                    readable, _, _ = select.select([sock], [], [], 1.0)
                except Exception:
                    break

                now = time.time()
                if now - last_frame_time > self.DEAD_TIMEOUT:
                    self.log(
                        f"no central am-msgd frame for {self.DEAD_TIMEOUT}s, reconnecting"
                    )
                    break
                if now - last_ping_time >= self.PING_INTERVAL:
                    try:
                        send_masked_frame(sock, 0x9, b"ping")
                    except Exception:
                        break
                    last_ping_time = now
                if not readable:
                    continue

                try:
                    opcode, payload = read_frame(sock)
                except (KeyboardInterrupt, SystemExit):
                    raise
                except Exception as error:
                    self.log(f"ws read error: {type(error).__name__}: {error}")
                    break

                last_frame_time = time.time()
                if opcode == 0x1:
                    try:
                        self.on_text(json.loads(payload.decode("utf-8")))
                    except Exception:
                        continue
                elif opcode == 0x9:
                    try:
                        send_masked_frame(sock, 0xA, payload)
                    except Exception:
                        disconnected = True
                elif opcode == 0xA:
                    pass
                elif opcode == 0x8:
                    disconnected = True
                else:
                    try:
                        send_masked_frame(sock, 0x8, b"")
                    except Exception:
                        pass
                    disconnected = True

            try:
                sock.close()
            except Exception:
                pass
            if self.pause_event is not None and self.pause_event.is_set():
                if self.paused_ack_event is not None:
                    self.paused_ack_event.set()
                backoff = self.BACKOFF_BASE
                continue
            delay = self._jitter_delay(backoff)
            self.log(f"reconnecting in {delay:.1f}s")
            self._wait_for_retry(delay)
            backoff = min(backoff * 2, self.BACKOFF_MAX)
