#!/usr/bin/env python3
"""
am — peer-to-peer agent messaging CLI backed by SQLite.

Messages are addressed sender->recipient; no room containers exist.
SQLite gives us atomic transactions, version-based optimistic concurrency,
unlimited history with O(log N) reads, and clean query semantics.

Database: ~/.agent-meeting/db/rooms.db (WAL mode for concurrent readers).

Identity: sessions are (project, name) composite. project is derived from
git rev-parse --show-toplevel basename, or cwd basename for non-git dirs.
Peer addressing: bare <name> resolves if globally unique across projects;
ambiguous names require <name>@<project> qualifier.

Usage:
  am init
  am send <self>[@project] <peer>[@project] <body> [--kind=X] [--ask=Y]
  am read <self>[@project] <peer>[@project] [--limit=30] [--since=ID]
  am show <self>[@project] <peer>[@project] [--limit=30]
  am turn <self>[@project] <peer>[@project]
  am list
  am online <name> --cwd <cwd>
  am offline <name>
"""

import argparse
import json
import os
import secrets
import signal
import socket
import sqlite3
import struct
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from agent_meeting.clients import hub_http_client
from agent_meeting.commands import (
    conversation_commands,
    group_commands,
)
from agent_meeting.message_hub import sqlite_message_database
from agent_meeting.message_hub import service_configuration
from agent_meeting.messaging import project_identity

if sys.platform.startswith("win"):
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

MEETING_HOME = os.environ.get("MEETING_HOME") or os.path.expanduser("~/.agent-meeting")
DB_PATH = os.path.join(MEETING_HOME, "db", "rooms.db")

_TELEMETRY_URL = "https://www.woodor.ai/_functions/t"
_CONFIG_PATH = os.path.join(MEETING_HOME, "config.json")

if sys.platform == "darwin":
    _OS_LABEL = "mac"
elif sys.platform.startswith("win"):
    _OS_LABEL = "win"
else:
    _OS_LABEL = "linux"


def beacon(event: str):
    if os.environ.get("MEETING_NO_TELEMETRY"):
        return

    def _send():
        try:
            cfg = {}
            try:
                with open(_CONFIG_PATH) as _f:
                    cfg = json.load(_f)
            except Exception:
                pass
            if cfg.get("telemetry") is False:
                return
            machine_id = cfg.get("machine_id", "unknown")
            version = cfg.get("plugin_version", "unknown")
            params = urllib.parse.urlencode({
                "e": event,
                "id": machine_id,
                "v": version,
                "os": _OS_LABEL,
            })
            url = f"{_TELEMETRY_URL}?{params}"
            urllib.request.urlopen(url, timeout=2)
        except Exception:
            pass

    t = threading.Thread(target=_send, daemon=True)
    t.start()


MDNS_BROWSE_TIMEOUT = 1.5
TCP_PROBE_TIMEOUT = 0.6
SERVICE_TYPE = "_agent-meeting._tcp.local."

ONLINE_THRESHOLD = 12

_NO_CONTROL_MSG = (
    "No control node found (agent-meeting-control). "
    "mDNS auto-discovery returned nothing and no last-known-good cache is available. "
    "Possible causes: control not started / different subnet / multicast blocked "
    "(Wi-Fi AP isolation or firewall blocking UDP 5353). "
    "Fix: run /imagent setup am-msgd to make this machine the central am-msgd session/message hub; "
    "or pin a control for unreachable machines — am host http://<ip>:<port> (persistent) "
    "or temporarily export AM_MSGD_HOST=http://<ip>:<port>."
)


# ---------- project derivation ----------

def _derive_project(cwd: str) -> str:
    return project_identity.derive_project(cwd, meeting_home=MEETING_HOME)


# ---------- name resolution ----------

def _parse_name_arg(raw: str, cwd: str | None = None) -> tuple[str, str]:
    """Parse a bare name or name@project into (project, name).

    For a bare name with cwd, derive project from cwd.
    For a bare name without cwd, return ("", name) — caller resolves via central am-msgd.
    """
    if not raw:
        raise SystemExit(
            "identity argument is empty; pass the canonical name@project value explicitly"
        )
    if "@" in raw:
        name, _, project = raw.partition("@")
        return (project, name)
    if cwd:
        return (_derive_project(cwd), raw)
    return ("", raw)


def _resolve_peer(
    host_url: str, raw: str, *, require_full_session: bool = False
) -> tuple[str, str]:
    """Resolve a peer name arg to (project, name).

    1. If raw has @project, use it directly.
    2. Otherwise query /resolve on the central am-msgd.
       - 0 results: no live session or message history anywhere for this name.
         Guessing self_project used to be the default here -- if the peer later
         registers under a different project, the message is filed under a
         composite key the peer can never read from, permanently and silently
         (phase 2 target #5). Require an explicit qualifier instead.
       - 1 group result: a caller may use it when group short names are allowed.
       - 1 session result: send requires the canonical name@project form.
       - 2+ results: require @project qualifier and raise SystemExit.
    """
    if "@" in raw:
        name, _, project = raw.partition("@")
        return (project, name)

    candidates = http("GET", host_url, "/resolve", params={"name": raw})

    if not candidates:
        raise SystemExit(
            f"peer '{raw}' has no live session or message history under any project -- "
            f"cannot safely guess its project. Pass an explicit qualifier: {raw}@<project> "
            f"(or {raw}@* if it registers globally)."
        )
    if len(candidates) == 1:
        candidate = candidates[0]
        if require_full_session and candidate.get("kind") != "group":
            project = candidate["project"]
            raise SystemExit(
                f"private recipient '{raw}' must use its full identity: "
                f"{raw}@{project}. Bare private names are not accepted even "
                "when the current candidate is unique."
            )
        return (candidates[0]["project"], raw)
    # Ambiguous
    options = ", ".join(f"{raw}@{c['project']}" for c in candidates)
    raise SystemExit(
        f"ambiguous name '{raw}' found in multiple projects. Use one of: {options}"
    )


# ---------- persistent host config (config.json) ----------
#
# Control resolution precedence (highest first):
#   1. env AM_MSGD_HOST                       — transient override
#   2. config.json "control_host"             — user-set, sticky; beats mDNS
#   3. mDNS live discovery                    — auto-follows IP changes
#   4. config.json "control_cache" (TCP-probed) — auto last-known-good fallback
#   5. healthy local loopback am-msgd
#   6. hard-fail with an actionable message
#
# 2 and 4 are distinct on purpose: control_host is pinned by the user and is
# never auto-evicted; control_cache is written automatically on every successful
# reach and may be refreshed/cleared by discovery. A box where mDNS is
# permanently dead (Wi-Fi multicast isolation / firewall) needs only to set
# control_host once — it then survives reboots and any cache logic.

def _read_config() -> dict:
    try:
        with open(_CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _write_config(cfg: dict):
    os.makedirs(os.path.dirname(_CONFIG_PATH), exist_ok=True)
    with open(_CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def _control_host() -> str | None:
    """User-pinned sticky host (precedence 2). None if unset."""
    url = _read_config().get("control_host")
    return url.rstrip("/") if url else None


def _read_control_cache() -> str | None:
    """Auto last-known-good host (precedence 4), ignoring age. None if unset."""
    entry = _read_config().get("control_cache") or {}
    url = entry.get("url") if isinstance(entry, dict) else None
    return url.rstrip("/") if url else None


def _read_control_cache_fresh(ttl: float) -> str | None:
    """Auto last-known-good host only if reached within `ttl` seconds.

    Fast path: lets repeated CLI calls / the 3s monitor poll skip the mDNS
    browse (~1.5-4.5s) while a control was recently confirmed reachable.
    """
    entry = _read_config().get("control_cache") or {}
    if not isinstance(entry, dict):
        return None
    url, ts = entry.get("url"), entry.get("ts", 0)
    if url and (time.time() - ts) < ttl:
        return url.rstrip("/")
    return None


def _write_control_cache(url: str):
    """Seed/refresh the auto last-known-good host. Never touches control_host."""
    if not url:
        return
    cfg = _read_config()
    cfg["control_cache"] = {"url": url.rstrip("/"), "ts": time.time()}
    try:
        _write_config(cfg)
    except Exception:
        pass


def _clear_control_cache():
    """Drop the auto cache (e.g. host went unreachable). Keeps control_host."""
    cfg = _read_config()
    if "control_cache" in cfg:
        del cfg["control_cache"]
        try:
            _write_config(cfg)
        except Exception:
            pass


def _tcp_reachable(url: str, timeout: float = TCP_PROBE_TIMEOUT) -> bool:
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname
    port = parsed.port or 8765
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _host_entry(url: str, version: str = "") -> dict:
    url = url.rstrip("/")
    parsed = urllib.parse.urlparse(url)
    ip = parsed.hostname or ""
    port = parsed.port or 8765
    return {"url": url, "host": ip, "ip": ip, "port": port, "version": version}


# ---------- LAN discovery (mDNS + HTTP client) ----------
#
# Discovery is layered because python-zeroconf is the single most unreliable
# component in this whole path. Observed on two machines: on a Windows client it
# receives ZERO mDNS responses (zombie 169.254 NICs / platform quirk) even though
# the control advertises fine and a raw socket sees it; on the Mac host a cold
# one-shot browse intermittently loses a timing race and returns empty, then
# self-heals on the next call. The control and the multicast network are fine in
# both cases — zeroconf just doesn't hand the packets up.
#
# So we never trust a single empty zeroconf browse as "no control": we fall
# through to a raw-socket mDNS query (bypasses the library entirely), then to a
# TCP-probed last-known-good. See discover_controls() for the full precedence.

RAW_DISCOVER_TIMEOUT = 2.0
_MCAST_ADDR = "224.0.0.251"
_MCAST_PORT = 5353


def _discover_zeroconf() -> list[dict]:
    """Normal fast path: browse via python-zeroconf. May flake/return empty."""
    try:
        from zeroconf import IPVersion, ServiceBrowser, ServiceListener, Zeroconf
    except ImportError:
        return []

    found: list[dict] = []

    class _L(ServiceListener):
        def add_service(self, zc, type_, name):
            info = zc.get_service_info(type_, name, timeout=int(MDNS_BROWSE_TIMEOUT * 1000))
            if info and info.addresses:
                ip = socket.inet_ntoa(info.addresses[0])
                port = info.port
                url = f"http://{ip}:{port}"
                props = info.properties or {}
                host = (props.get(b"host") or b"").decode("utf-8", errors="replace")
                version = (props.get(b"version") or b"").decode("utf-8", errors="replace")
                found.append({"url": url, "host": host, "ip": ip, "port": port, "version": version})

        def update_service(self, *a): pass
        def remove_service(self, *a): pass

    for _attempt in range(3):
        try:
            zc = Zeroconf(ip_version=IPVersion.V4Only)
            ServiceBrowser(zc, SERVICE_TYPE, _L())
            time.sleep(MDNS_BROWSE_TIMEOUT)
            zc.close()
        except Exception:
            break
        if found:
            break
    return found


# ----- raw-socket mDNS fallback (bypasses python-zeroconf) -----
#
# Adapted from a probe roof-notify verified on the exact box where zeroconf was
# blind. Sends the same PTR query python-zeroconf would, but on a plain UDP
# socket we control, joined to the multicast group on every up interface. Only
# parses response packets (QR=1) so our own outgoing query doesn't loopback as a
# false hit. macOS already holds :5353 via mDNSResponder, so SO_REUSEPORT is
# mandatory there to co-bind; if we can't bind :5353 we genuinely cannot receive
# the responses, so we bail rather than bind an ephemeral port that hears nothing.

def _raw_local_ipv4s() -> list[str]:
    ips: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    return [ip for ip in ips if ip != "127.0.0.1" and not ip.startswith("169.254.")]


def _raw_build_query(labels, qtype=12) -> bytes:
    hdr = struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0)
    q = b"".join(bytes([len(l)]) + l.encode() for l in labels) + b"\x00"
    return hdr + q + struct.pack("!HH", qtype, 1)


def _raw_read_name(data, off):
    labels, jumped, orig = [], False, off
    while off < len(data):
        l = data[off]
        if l == 0:
            off += 1
            break
        if l & 0xC0 == 0xC0:
            ptr = ((l & 0x3F) << 8) | data[off + 1]
            if not jumped:
                orig = off + 2
            off, jumped = ptr, True
            continue
        off += 1
        labels.append(data[off:off + l])
        off += l
    return b".".join(labels).decode("utf-8", "replace"), (orig if jumped else off)


def _raw_parse(data, ptrs, srv, a, txt):
    try:
        qd, an, ns, ar = struct.unpack("!HHHH", data[4:12])
        off = 12
        for _ in range(qd):
            _, off = _raw_read_name(data, off)
            off += 4
        for _ in range(an + ns + ar):
            name, off = _raw_read_name(data, off)
            rtype, _c, _t, rdlen = struct.unpack("!HHIH", data[off:off + 10])
            off += 10
            rd = off
            off += rdlen
            if rtype == 12:
                tgt, _ = _raw_read_name(data, rd)
                ptrs.setdefault(name, set()).add(tgt)
            elif rtype == 33:
                _p, _w, port = struct.unpack("!HHH", data[rd:rd + 6])
                host, _ = _raw_read_name(data, rd + 6)
                srv[name] = (host, port)
            elif rtype == 16:
                props, i = {}, rd
                while i < rd + rdlen:
                    ln = data[i]
                    i += 1
                    if ln:
                        kv = data[i:i + ln]
                        i += ln
                        k, _, v = kv.partition(b"=")
                        props[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
                txt[name] = props
            elif rtype == 1 and rdlen == 4:
                a[name] = socket.inet_ntoa(data[rd:rd + 4])
    except Exception:
        pass


def _discover_controls_raw(timeout: float = RAW_DISCOVER_TIMEOUT) -> list[dict]:
    ifaces = _raw_local_ipv4s() or ["0.0.0.0"]
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    try:
        rx.bind(("", _MCAST_PORT))
    except OSError:
        # Cannot co-bind :5353 → cannot receive the responses. Raw path is
        # unavailable on this box; let the caller fall through to the cache.
        rx.close()
        return []
    for ip in ifaces:
        try:
            rx.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                          socket.inet_aton(_MCAST_ADDR) + socket.inet_aton(ip))
        except OSError:
            pass
    pkt = _raw_build_query(SERVICE_TYPE.rstrip(".").split("."))
    for ip in ifaces:
        try:
            rx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
            rx.sendto(pkt, (_MCAST_ADDR, _MCAST_PORT))
        except OSError:
            pass
    ptrs, srv, a, txt = {}, {}, {}, {}
    rx.settimeout(0.5)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data, _ = rx.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError:
            break
        if len(data) >= 12 and (data[2] & 0x80):  # QR=1: response only
            _raw_parse(data, ptrs, srv, a, txt)
    rx.close()

    base = SERVICE_TYPE.rstrip(".")
    out = []
    for inst in ptrs.get(base, set()) | ptrs.get(base + ".", set()):
        host_l, port = srv.get(inst, (None, None))
        if not port:
            continue
        ip = a.get(host_l, "")
        props = txt.get(inst, {})
        out.append({
            "url": f"http://{ip}:{port}" if ip else "",
            "host": props.get("host", "") or host_l or ip,
            "ip": ip,
            "port": port,
            "version": props.get("version", ""),
        })
    return [c for c in out if c["url"]]


def _healthy_local_control_url() -> str | None:
    configuration = service_configuration.load(
        service_configuration.default_path(Path(MEETING_HOME)),
        create=False,
    )
    local_url = f"http://127.0.0.1:{configuration.port}"
    try:
        with _NO_PROXY_OPENER.open(
            f"{local_url}/health",
            timeout=1,
        ) as response:
            return local_url if response.status == 200 else None
    except Exception:
        return None


def discover_controls() -> list[dict]:
    # 1. env override.
    if os.environ.get("AM_MSGD_HOST"):
        return [_host_entry(os.environ["AM_MSGD_HOST"])]

    # 2. user-pinned sticky host — beats discovery entirely.
    sticky = _control_host()
    if sticky:
        return [_host_entry(sticky)]

    # 3. zeroconf (fast, normal path). Authoritative when it returns anything.
    found = _discover_zeroconf()
    if found:
        _write_control_cache(found[0]["url"])
        return found

    # 4. raw-socket mDNS — zeroconf came up empty, but the library is the weak
    #    link, not the network. Re-query without it before giving up.
    raw = _discover_controls_raw()
    if raw:
        _write_control_cache(raw[0]["url"])
        return raw

    # 5. TCP-probed last-known-good. Covers boxes where even raw multicast is
    #    blocked (e.g. true Wi-Fi AP isolation) but a control was reachable before.
    cached = _read_control_cache()
    if cached and _tcp_reachable(cached):
        return [_host_entry(cached)]

    # 5. Every installation owns a loopback am-msgd. Use it only after all
    # shared remote-selection layers have been exhausted.
    local_url = _healthy_local_control_url()
    if local_url:
        return [_host_entry(local_url)]

    return []


HOST_FASTPATH_TTL = 60


def discover_host() -> str | None:
    # 1. env override.
    if os.environ.get("AM_MSGD_HOST"):
        return os.environ["AM_MSGD_HOST"].rstrip("/")

    # 2. user-pinned sticky host — beats mDNS, no probe needed.
    sticky = _control_host()
    if sticky:
        return sticky

    # Fast path: a control was confirmed reachable within the TTL — skip the
    # mDNS browse so back-to-back CLI calls / the 3s monitor poll stay cheap.
    fresh = _read_control_cache_fresh(HOST_FASTPATH_TTL)
    if fresh:
        return fresh

    # 3-6. Live discovery, cache, then healthy local loopback.
    controls = discover_controls()
    if controls:
        return controls[0]["url"]
    return None


def _invalidate_host_cache():
    """Force re-discovery after a host went unreachable.

    Only clears the auto cache — env AM_MSGD_HOST and the user-pinned
    control_host are intentionally left intact so a deliberate override is
    never silently dropped on a transient network blip.
    """
    _clear_control_cache()


def _resolve_host(explicit: str | None = None) -> str:
    if explicit:
        return explicit.rstrip("/")
    url = discover_host()
    if url:
        return url
    raise SystemExit(_NO_CONTROL_MSG)


def _get_auth_token() -> str | None:
    env_tok = os.environ.get("MEETING_TOKEN")
    if env_tok:
        return env_tok
    try:
        with open(_CONFIG_PATH) as _f:
            return json.load(_f).get("auth_token") or None
    except Exception:
        return None


def _http_once(method: str, host_url: str, path: str,
               params: dict | None, body: dict | None):
    return hub_http_client.request_once(
        method,
        host_url,
        path,
        params,
        body,
        auth_token=_get_auth_token(),
        opener=_NO_PROXY_OPENER,
    )


def http(method: str, host_url: str, path: str,
         params: dict | None = None, body: dict | None = None):
    return hub_http_client.request_with_rediscovery(
        method,
        host_url,
        path,
        params,
        body,
        read_auth_token=_get_auth_token,
        invalidate_host_cache=_invalidate_host_cache,
        discover_host=discover_host,
        opener=_NO_PROXY_OPENER,
    )


SCHEMA = sqlite_message_database.SCHEMA


# ---------- connection ----------

def connect():
    db = sqlite3.connect(DB_PATH, isolation_level=None, timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript("PRAGMA journal_mode = WAL; PRAGMA foreign_keys = ON;")
    return db


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    sqlite_message_database.prepare_message_database(DB_PATH)


# ---------- commands ----------

def _conversation_services():
    return conversation_commands.ConversationCommandServices(
        resolve_host=_resolve_host,
        parse_self=_parse_name_arg,
        resolve_peer=_resolve_peer,
        request=http,
        record_event=beacon,
    )


def cmd_init(args):
    init_db()
    print(f"db ready: {DB_PATH}")


def cmd_send(args):
    conversation_commands.send(args, _conversation_services())


def cmd_read(args):
    conversation_commands.read(args, _conversation_services())


def cmd_message(args):
    conversation_commands.message(args, _conversation_services())


def cmd_show(args):
    conversation_commands.show(args, _conversation_services())


def cmd_turn(args):
    conversation_commands.turn(args, _conversation_services())


def cmd_delete(args):
    conversation_commands.delete(args, _conversation_services())


def cmd_online(args):
    name = args.name
    is_global = getattr(args, "is_global", False)
    cwd = args.cwd
    if cwd is None:
        if is_global:
            cwd = os.path.expanduser("~")
        else:
            print("online: --cwd is required (unless --global)", file=sys.stderr)
            sys.exit(2)

    explicit_proj = getattr(args, "proj", None)
    if is_global:
        project = "*"
    else:
        if explicit_proj is not None:
            try:
                explicit_proj = project_identity.validate_project(explicit_proj)
            except ValueError as e:
                print(f"online: {e}", file=sys.stderr)
                sys.exit(2)
        # Authoritative identity only: an explicit --proj (this call or a past
        # one cached for this repo root). No path-based fallback -- a session
        # with neither must not silently register under a cwd-derived guess
        # (see docs/archive/contracts/0.10.0-composite-key-identity.md).
        project = project_identity.resolve_authoritative_project(
            cwd,
            explicit_proj,
            meeting_home=MEETING_HOME,
        )
        if project is None:
            print(json.dumps({"ok": False, "code": "missing_project_identity"}))
            print(
                f"online: no project identity for cwd={cwd} -- pass --proj explicitly "
                "(no explicit --proj on this call and no cached declaration for this "
                "repo root); registration was not attempted.",
                file=sys.stderr,
            )
            sys.exit(4)
    host_name = socket.gethostname()

    explicit_host = getattr(args, "host", None)
    host = _resolve_host(explicit_host)

    payload = {
        "project": project, "name": name, "cwd": cwd,
        "force": args.force,
        # Always send an explicit role so re-registration is authoritative:
        # central am-msgd upserts role via COALESCE(?, role), so sending None on a
        # worker re-register would preserve a stale 'director' and make demotion
        # impossible. "director"/"worker" lets the monitor's --director flag win.
        "role": "director" if args.director else "worker",
        "host": host_name, "os": _OS_LABEL,
        "instance": getattr(args, "instance", None),
        "client_version": getattr(args, "client_version", None),
    }
    r = http("POST", host, "/register", body=payload)
    if r.get("error"):
        print(r["error"], file=sys.stderr)
        # Exit code 3 is a distinct, machine-checkable signal for "name is taken
        # by another live registration" (central-am-msgd-supplied code, not string-matched)
        # so callers like monitor.py can tell it apart from generic failures.
        sys.exit(3 if r.get("code") == "name_taken" else 1)
    # Any host we just registered against is a good last-known-good — seed the
    # auto cache so a later mDNS blackout (Wi-Fi multicast isolation, etc.) still
    # has a reachable fallback. Skipped when env AM_MSGD_HOST drove the resolve,
    # since that override is transient and shouldn't be persisted.
    if not os.environ.get("AM_MSGD_HOST"):
        _write_control_cache(host)

    display = name if project == "*" else f"{name}@{project}"
    print(f"online: {display} (cwd={cwd}, host={host_name})")
    beacon("register")


def cmd_rename(args):
    import re
    if not re.fullmatch(r"[A-Za-z0-9-]{2,20}", args.new) or "--" in args.new:
        print(f"invalid name '{args.new}': must be 2-20 chars, only [A-Za-z0-9-], no '--'",
              file=sys.stderr)
        sys.exit(1)
    host = _resolve_host(getattr(args, "host", None))
    cwd = os.getcwd()
    project = _derive_project(cwd)
    r = http("POST", host, "/rename", body={"project": project, "old": args.old, "new": args.new})
    if r.get("error"):
        print(r["error"], file=sys.stderr)
        sys.exit(1)
    print(f"renamed: {r['old']}@{r['project']} -> {r['new']}@{r['project']} (messages: {r['messages_migrated']})")


def cmd_offline(args):
    name = args.name

    host = _resolve_host(getattr(args, "host", None))
    cwd = os.getcwd()

    is_global = getattr(args, "is_global", False)
    explicit_proj = getattr(args, "proj", None)
    if is_global:
        project = "*"
    elif explicit_proj is not None:
        try:
            project = project_identity.validate_project(explicit_proj)
        except ValueError as e:
            print(f"offline: {e}", file=sys.stderr)
            sys.exit(2)
    else:
        # No escape hatch given -- fall back to the caller's own cwd-derived
        # project, same as every other bare-name command. A session that
        # registered with --proj/--global must pass the same flag here or it
        # will target the wrong composite key (see the `deleted` check below).
        project = _derive_project(cwd)

    payload = {"project": project, "name": name}
    if getattr(args, "instance", None):
        payload["instance"] = args.instance
    r = http("POST", host, "/unregister", body=payload)
    if r.get("error"):
        print(r["error"], file=sys.stderr)
        sys.exit(1)
    if not r.get("deleted"):
        print(
            f"offline: no session '{name}@{project}' was registered -- nothing to take offline. "
            "If it registered under a different project, retry with --proj <project> or --global.",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"offline: {name}@{project}")


def cmd_telemetry(args):
    try:
        with open(_CONFIG_PATH) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        cfg = {}

    if args.action == "status":
        flag = cfg.get("telemetry")
        if flag is False:
            config_state = "disabled"
        else:
            config_state = "enabled"
        env_override = bool(os.environ.get("MEETING_NO_TELEMETRY"))
        print(f"telemetry: {config_state} (config.json)")
        if env_override:
            print("MEETING_NO_TELEMETRY is set -- environment variable overrides config and forces telemetry off")
    elif args.action == "off":
        cfg["telemetry"] = False
        with open(_CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
        print("telemetry disabled (config.json)")
    elif args.action == "on":
        cfg["telemetry"] = True
        with open(_CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
        print("telemetry enabled (config.json)")


def cmd_token(args):
    try:
        with open(_CONFIG_PATH) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        cfg = {}

    value = getattr(args, "value", None)

    if value == "clear":
        cfg.pop("auth_token", None)
        with open(_CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
        print("auth_token cleared -- central am-msgd is now open (no auth required)")
        return

    if value:
        cfg["auth_token"] = value
        with open(_CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
        print(f"auth_token set: {value}")
        return

    existing = cfg.get("auth_token")
    if existing:
        print(existing)
        print("(existing token -- distribute this to every client machine)")
        return

    new_token = secrets.token_urlsafe(32)
    cfg["auth_token"] = new_token
    with open(_CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
    print(new_token)
    print(
        "Token generated and written to config.json. "
        "Run `am token <value>` on every client machine to distribute it, "
        "or set the MEETING_TOKEN env var. Keep it secret."
    )


def cmd_host(args):
    """Pin / clear / show the sticky control host (config.json control_host).

    For boxes where mDNS discovery is permanently broken (zeroconf blind to
    multicast, AP isolation, etc.). A pinned host wins over live discovery —
    precedence 2 — so it survives reboots and any cache logic. One knob.
    """
    value = getattr(args, "value", None)

    if value is None:
        current = _control_host()
        env = os.environ.get("AM_MSGD_HOST")
        if env:
            print(f"control_host (config): {current or '(unset)'}")
            print(f"AM_MSGD_HOST (env, overrides): {env.rstrip('/')}")
        else:
            print(current or "(unset)")
        return

    cfg = _read_config()
    if value == "clear":
        cfg.pop("control_host", None)
        _write_config(cfg)
        print("control_host cleared -- discovery falls back to mDNS / raw socket / cache")
        return

    url = value.rstrip("/")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        print(f"invalid host '{value}': expected http://<ip-or-name>:<port>", file=sys.stderr)
        sys.exit(2)
    cfg["control_host"] = url
    _write_config(cfg)
    reachable = "reachable" if _tcp_reachable(url) else "NOT reachable right now"
    print(f"control_host pinned: {url} ({reachable})")
    print("(this beats mDNS; clear with `am host clear`)")


def _validate_group_name(name: str) -> str | None:
    return group_commands.validate_group_name(name)


def cmd_group(args):
    group_commands.run_group_command(
        args,
        resolve_host=_resolve_host,
        derive_project=_derive_project,
        request=http,
    )


def cmd_controls(args):
    controls = discover_controls()

    # "Current" = whatever this box would actually resolve to right now.
    current_url = (
        (os.environ.get("AM_MSGD_HOST") or "").rstrip("/") or None
        or _control_host()
        or _read_control_cache()
    )

    if args.json:
        result = []
        for c in controls:
            result.append({
                "host": c.get("host") or "",
                "ip": c.get("ip") or "",
                "port": c.get("port") or 0,
                "url": c.get("url") or "",
                "version": c.get("version") or "",
                "is_current": c["url"] == current_url,
            })
        print(json.dumps(result, ensure_ascii=False))
        return

    if not controls:
        print("no control node found")
        return

    for i, c in enumerate(controls):
        is_current = c["url"] == current_url
        current_tag = "  current" if is_current else ""
        print(f"control {i + 1}{current_tag}")
        print(f"  host:    {c['host'] or '(unknown)'}")
        print(f"  ip:port: {c['ip']}:{c['port']}")
        print(f"  url:     {c['url']}")
        print(f"  version: {c['version'] or '(unknown)'}")

def cmd_stop(args):
    cwd = os.getcwd()
    is_global = getattr(args, "is_global", False)
    explicit_proj = getattr(args, "proj", None)
    if is_global:
        project = "*"
    elif explicit_proj is not None:
        try:
            project = project_identity.validate_project(explicit_proj)
        except ValueError as e:
            print(f"stop: {e}", file=sys.stderr)
            sys.exit(2)
    else:
        # No escape hatch given -- fall back to cwd derivation, same as the
        # monitor's own registration default. Two projects' same-named
        # monitors have distinct project-scoped pidfiles,
        # so stopping the wrong one now fails loudly instead of silently
        # killing whichever process happened to win the old shared filename.
        project = _derive_project(cwd)

    run_dir = os.path.join(MEETING_HOME, "run")
    pid_file = os.path.join(
        run_dir,
        f"{project_identity.monitor_pidfile_stem(args.name, project)}.pid",
    )
    display = args.name if project == "*" else f"{args.name}@{project}"

    if not os.path.exists(pid_file):
        print(
            f"no running monitor for '{display}' -- if it registered under a different "
            "project, retry with --proj <project> or --global",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        with open(pid_file) as f:
            pid = int(f.read().strip())
    except Exception as e:
        print(f"could not read pidfile: {e}", file=sys.stderr)
        sys.exit(1)

    if not process_liveness.is_process_alive(pid):
        try:
            os.remove(pid_file)
        except Exception:
            pass
        print(f"no running monitor for '{display}' (cleaned stale pidfile)", file=sys.stderr)
        sys.exit(1)

    os.kill(pid, signal.SIGTERM)

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        time.sleep(0.1)
        if not process_liveness.is_process_alive(pid):
            print(f"stopped monitor: {display} (pid {pid})")
            return

    print(f"SIGTERM sent to monitor '{display}' (pid {pid}), but process has not exited within 3s")


def cmd_list(args):
    host = _resolve_host(getattr(args, "host", None))
    rows = http("GET", host, "/list")
    for r in rows:
        print(f"{r['status']}\t{r['name']}\t{r['project']}\t{r['msgs']}\t{r.get('role') or 'worker'}\t{r.get('cwd') or ''}\t{r.get('host') or ''}\t{r.get('os') or ''}")


def cmd_projcache(args):
    action = getattr(args, "action", None) or "list"
    cwd = os.getcwd()
    root = project_identity._project_root(cwd)

    if action == "list":
        entries = project_identity.proj_cache_entries(meeting_home=MEETING_HOME)
        if not entries:
            print("no cached project declarations")
            return
        for e in entries:
            current = "*" if e["root"] == root else " "
            label = e["root"] or f"(unknown root, key={e['key']})"
            print(f"[{current}] {label}\t{e['proj']}")
        return

    if action == "clear":
        if getattr(args, "all", False):
            n = project_identity.proj_cache_clear_all(meeting_home=MEETING_HOME)
            print(f"cleared {n} cached declaration(s)")
            return
        proj = project_identity.proj_cache_get(root, meeting_home=MEETING_HOME)
        if project_identity.proj_cache_clear(root, meeting_home=MEETING_HOME):
            print(f"cleared cached declaration for {root}: {proj}")
        else:
            print(f"no cached declaration for {root}")


def cmd_prune(args):
    host = _resolve_host(getattr(args, "host", None))
    r = http("POST", host, "/prune", body={
        "older_than_days": args.older_than,
        "include_referenced": args.include_referenced,
        "apply": args.yes,
    })
    prune, skipped = r["pruned"], r["skipped_referenced"]

    for it in skipped:
        print(f"skip   {it['name']}@{it['project']}\t{it['age_days']}d\t{it['msgs']} msgs\t(has history)")
    for it in prune:
        verb = "pruned" if r["applied"] else "would prune"
        print(f"{verb} {it['name']}@{it['project']}\t{it['age_days']}d\t{it['msgs']} msgs\t{it['host'] or '?'}")

    if not prune and not skipped:
        print(f"nothing older than {args.older_than}d")
        return
    if r["applied"]:
        print(f"\npruned {len(prune)} session row(s); {len(skipped)} kept (referenced by messages)")
    else:
        print(f"\ndry run -- {len(prune)} row(s) would be pruned, {len(skipped)} kept. "
              f"Re-run with --yes to apply.")


# ---------- main ----------

def main():
    p = argparse.ArgumentParser(prog="am")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(func=cmd_init)

    s = sub.add_parser("send", help="insert a message and flip turn")
    s.add_argument("self_arg", metavar="self", help="sender name[@project]")
    s.add_argument("peer", help="peer name[@project]")
    s.add_argument("body", nargs="?", default=None,
                   help="message body inline; or `-` to read stdin; ignored when --body-file given")
    s.add_argument("--body-file", default=None)
    s.add_argument("--kind", default="回应")
    s.add_argument("--ask", default=None)
    s.add_argument("--host", default=None)
    s.set_defaults(func=cmd_send)

    s = sub.add_parser("read", help="dump messages as TSV rows")
    s.add_argument("self_arg", metavar="self")
    s.add_argument("peer")
    s.add_argument("--limit", type=int, default=30)
    s.add_argument("--since", type=int, default=0)
    s.add_argument("--host", default=None)
    s.set_defaults(func=cmd_read)

    s = sub.add_parser("message", help="read one exact message by global msg_id")
    s.add_argument("self_arg", metavar="self")
    s.add_argument("message_id", type=int, metavar="msg_id")
    s.add_argument("--host", default=None)
    s.set_defaults(func=cmd_message)

    s = sub.add_parser("show", help="pretty markdown render")
    s.add_argument("self_arg", metavar="self")
    s.add_argument("peer")
    s.add_argument("--limit", type=int, default=30)
    s.add_argument("--host", default=None)
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("turn", help="print current turn-holder")
    s.add_argument("self_arg", metavar="self")
    s.add_argument("peer")
    s.add_argument("--host", default=None)
    s.set_defaults(func=cmd_turn)

    s = sub.add_parser("list", help="list all session names with status + msg count")
    s.add_argument("--host", default=None)
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("prune", help="drop stale sessions rows (dry run unless --yes); never touches messages")
    s.add_argument("--older-than", dest="older_than", type=float, default=7,
                   help="only consider rows whose last heartbeat is older than N days (default 7)")
    s.add_argument("--include-referenced", dest="include_referenced", action="store_true",
                   help="also prune identities that appear in messages (their history stays)")
    s.add_argument("--yes", action="store_true", help="actually delete; without it this is a dry run")
    s.add_argument("--host", default=None)
    s.set_defaults(func=cmd_prune)

    s = sub.add_parser("projcache", help="inspect/clear cached --proj declarations (local only, no central am-msgd call)")
    s.add_argument("action", nargs="?", choices=["list", "clear"], default="list")
    s.add_argument("--all", action="store_true", help="with clear: clear every cached declaration, not just this cwd's repo root")
    s.set_defaults(func=cmd_projcache)

    s = sub.add_parser("delete", help="clear all messages with a peer (hard delete, atomic)")
    s.add_argument("self_arg", metavar="self")
    s.add_argument("peer")
    s.add_argument("--host", default=None)
    s.set_defaults(func=cmd_delete)

    s = sub.add_parser("controls",
                       help="list all discovered control nodes")
    s.add_argument("--json", action="store_true", default=False)
    s.set_defaults(func=cmd_controls)

    s = sub.add_parser("telemetry")
    s.add_argument("action", choices=["on", "off", "status"])
    s.set_defaults(func=cmd_telemetry)

    s = sub.add_parser("token")
    s.add_argument("value", nargs="?", default=None)
    s.set_defaults(func=cmd_token)

    s = sub.add_parser("host",
                       help="pin/clear/show a sticky control host for boxes where mDNS is broken")
    s.add_argument("value", nargs="?", default=None,
                   help="http://<ip-or-name>:<port> to pin, 'clear' to remove, omit to show")
    s.set_defaults(func=cmd_host)

    s = sub.add_parser("online",
                       help="bring this session online (register in the directory)")
    s.add_argument("name", help="session name to bring online")
    s.add_argument("--cwd", default=None,
                   help="working dir (required unless --global, where it defaults to ~)")
    s.add_argument("--force", action="store_true")
    s.add_argument("--director", action="store_true")
    s.add_argument("--global", dest="is_global", action="store_true",
                   help="register as global identity (project='*'), skips cwd project derivation")
    s.add_argument("--proj", default=None,
                   help="explicit project identity; bypasses folder-based derivation and is cached per repo root")
    s.add_argument("--instance", default=None,
                   help="process-unique id (e.g. a monitor's own uuid) so central-am-msgd-restart reconnects "
                        "of the SAME process are always allowed, while a DIFFERENT live process "
                        "registering the same name is refused instead of silently taking over")
    s.add_argument("--client-version", dest="client_version", default=None,
                   help="reporting client's plugin version (e.g. monitor.py's), stored on the "
                        "session row for observability; omit for no value")
    s.add_argument("--host", default=None)
    s.set_defaults(func=cmd_online)

    s = sub.add_parser("offline",
                       help="take this session offline")
    s.add_argument("name")
    s.add_argument("--global", dest="is_global", action="store_true",
                   help="target the global identity (project='*'), skips cwd project derivation")
    s.add_argument("--proj", default=None,
                   help="explicit project identity to target; bypasses folder-based derivation")
    s.add_argument("--instance", default=None, help=argparse.SUPPRESS)
    s.add_argument("--host", default=None)
    s.set_defaults(func=cmd_offline)

    sp = sub.add_parser("stop",
                       help="stop the local monitor process for a session")
    sp.add_argument("name")
    sp.add_argument("--global", dest="is_global", action="store_true",
                    help="target the global identity (project='*'), skips cwd project derivation")
    sp.add_argument("--proj", default=None,
                    help="explicit project identity to target; bypasses folder-based derivation")
    sp.set_defaults(func=cmd_stop)

    rp = sub.add_parser("rename",
                        help="rename a session and migrate all its messages (same project only)")
    rp.add_argument("old", help="current session name")
    rp.add_argument("new", help="new session name (2-20 chars, [A-Za-z0-9-], no '--')")
    rp.add_argument("--host", default=None)
    rp.set_defaults(func=cmd_rename)

    gp = sub.add_parser("group", help="manage groups")
    gp.add_argument("--host", default=None)
    gsub = gp.add_subparsers(dest="group_cmd", required=True)

    gc = gsub.add_parser("create", help="create a group")
    gc.add_argument("group_name", help="group name (bare or name@project)")
    gc.add_argument("--members", required=True, help="comma-separated member names (bare or name@project)")
    gc.add_argument("--creator", default=None)

    ga = gsub.add_parser("add", help="add a member to a group")
    ga.add_argument("group_name", help="group name (bare or name@project)")
    ga.add_argument("member", help="member name (bare or name@project)")

    gr = gsub.add_parser("remove", help="remove a member from a group")
    gr.add_argument("group_name", help="group name (bare or name@project)")
    gr.add_argument("member", help="member name (bare or name@project)")

    gnr = gsub.add_parser("rename", help="rename a group (cascades group_members + messages)")
    gnr.add_argument("old_name", help="current group name (bare or name@project); new stays in the same project")
    gnr.add_argument("new_name")

    gl = gsub.add_parser("list", help="list groups (optionally filter by member)")
    gl.add_argument("--member", default=None, help="filter by member (bare or name@project)")

    gm = gsub.add_parser("members", help="list members of a group")
    gm.add_argument("group_name", help="group name (bare or name@project)")

    gd = gsub.add_parser("delete", help="delete a group and purge its messages")
    gd.add_argument("group_name", help="group name (bare or name@project)")

    gch = gsub.add_parser("charter", help="get or set group charter (rule text)")
    gch.add_argument("group_name", help="group name (bare or name@project)")
    gch.add_argument("charter_text", nargs="*", help="charter text to set (omit to read)")
    gch.add_argument("--clear", action="store_true", help="clear the charter")

    gp.set_defaults(func=cmd_group)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
