"""mDNS publication for the LAN agent-meeting message hub."""

import ipaddress
import os
import socket


SERVICE_TYPE = "_agent-meeting._tcp.local."
_RFC1918 = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def candidate_ipv4_addresses() -> set[str]:
    """Return IPv4 candidates without making adapter discovery mandatory."""
    candidates: set[str] = set()
    try:
        import ifaddr

        for adapter in ifaddr.get_adapters():
            for address in adapter.ips:
                if isinstance(address.ip, str):
                    candidates.add(address.ip)
    except Exception:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            candidates.add(probe.getsockname()[0])
        except Exception:
            pass
        finally:
            probe.close()
    return candidates


def select_advertise_address(candidates: set[str] | None = None) -> str:
    """Prefer RFC1918 LAN addresses and reject loopback, link-local, and CGNAT."""
    override = os.environ.get("MEETING_ADVERTISE_IP")
    if override:
        return override

    def score(raw_address: str) -> int:
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError:
            return -1
        if (
            address.version != 4
            or address in _CGNAT
            or address.is_loopback
            or address.is_link_local
        ):
            return -1
        if any(address in network for network in _RFC1918):
            return 2
        return 1

    selected, selected_score = "127.0.0.1", 0
    for address in sorted(candidates if candidates is not None else candidate_ipv4_addresses()):
        address_score = score(address)
        if address_score > selected_score:
            selected, selected_score = address, address_score
    return selected


def publish_message_hub(port: int, runtime_version: str):
    """Publish the message hub and return the zeroconf owner and service info."""
    from zeroconf import IPVersion, ServiceInfo, Zeroconf

    zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
    hostname = socket.gethostname().replace(".local", "")
    instance = f"{hostname}-meeting"
    address = select_advertise_address()
    info = ServiceInfo(
        SERVICE_TYPE,
        f"{instance}.{SERVICE_TYPE}",
        addresses=[socket.inet_aton(address)],
        port=port,
        properties={
            b"version": runtime_version.encode("utf-8"),
            b"host": hostname.encode("utf-8"),
        },
        server=f"{hostname}.local.",
    )
    zeroconf.register_service(info)
    print(f"[mDNS] published {instance} -> {address}:{port}", flush=True)
    return zeroconf, info
