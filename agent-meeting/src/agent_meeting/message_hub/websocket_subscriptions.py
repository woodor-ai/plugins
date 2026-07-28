"""WebSocket subscriber state and minimal RFC6455 frame operations."""

import re
import struct
import threading
import time


class Subscriber:
    """One direct or notification-only message-hub subscription."""

    __slots__ = (
        "project",
        "name",
        "sock",
        "wfile",
        "high_water_mark",
        "state",
        "send_lock",
        "last_pong",
        "mode",
    )

    def __init__(
        self,
        project: str,
        name: str,
        sock,
        wfile,
        cursor: int,
        mode: str = "delivery",
    ):
        self.project = project
        self.name = name
        self.sock = sock
        self.wfile = wfile
        self.high_water_mark = cursor
        self.state = "live" if mode == "notify" else "draining"
        self.send_lock = threading.Lock()
        self.last_pong = time.time()
        self.mode = mode

    @property
    def key(self) -> tuple[str, str]:
        return self.project, self.name


def send_text(subscriber: Subscriber, payload: str) -> bool:
    data = payload.encode("utf-8")
    length = len(data)
    if length < 126:
        header = struct.pack("!BB", 0x81, length)
    elif length < 65536:
        header = struct.pack("!BBH", 0x81, 126, length)
    else:
        header = struct.pack("!BBQ", 0x81, 127, length)
    try:
        subscriber.wfile.write(header + data)
        subscriber.wfile.flush()
        return True
    except Exception:
        return False


def send_ping(subscriber: Subscriber) -> bool:
    try:
        subscriber.wfile.write(b"\x89\x00")
        subscriber.wfile.flush()
        return True
    except Exception:
        return False


def send_close(subscriber: Subscriber) -> None:
    try:
        subscriber.wfile.write(b"\x88\x00")
        subscriber.wfile.flush()
    except Exception:
        pass


def read_frame(stream) -> tuple[int, bytes]:
    header = stream.read(2)
    if len(header) < 2:
        raise IOError("EOF reading frame header")
    first, second = header[0], header[1]
    final = (first & 0x80) != 0
    opcode = first & 0x0F
    masked = (second & 0x80) != 0
    length = second & 0x7F
    if not final:
        raise IOError("fragmented frame not supported")
    if length == 126:
        extended = stream.read(2)
        if len(extended) < 2:
            raise IOError("EOF reading 16-bit length")
        length = struct.unpack("!H", extended)[0]
    elif length == 127:
        extended = stream.read(8)
        if len(extended) < 8:
            raise IOError("EOF reading 64-bit length")
        length = struct.unpack("!Q", extended)[0]

    mask_key = b""
    if masked:
        mask_key = stream.read(4)
        if len(mask_key) < 4:
            raise IOError("EOF reading mask key")
    payload = stream.read(length) if length else b""
    if len(payload) < length:
        raise IOError("EOF reading payload")
    if masked:
        payload = bytes(
            byte ^ mask_key[index % 4]
            for index, byte in enumerate(payload)
        )
    return opcode, payload


_MENTION = re.compile(r"@([A-Za-z0-9-]+)")


def parse_mentions(body: str, member_names: set[str]) -> set[str]:
    """Return only @mentions that name current group members."""
    return set(_MENTION.findall(body)) & member_names
