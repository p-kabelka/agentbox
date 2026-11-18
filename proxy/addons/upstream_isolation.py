"""Check actual Python TCP socket destinations without changing mitmproxy's pool keys."""

import ipaddress
import socket
import sys
from pathlib import Path

HOST_NET = Path("/host-net")
LOCAL_NET = Path("/proc/net")
BLOCKED = "Agentbox blocked a host/control-plane destination or unavailable network policy"


def compile_private_upstreams(values):
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise ValueError("private_upstreams must be a list of exact IP addresses")
    addresses = (ipaddress.ip_address(value) for value in values)
    return frozenset(getattr(address, "ipv4_mapped", None) or address for address in addresses)


def host_addresses(*, require_ipv6=False):
    lines = (HOST_NET / "fib_trie").read_text().splitlines()
    addresses = {ipaddress.IPv4Address(previous.split()[-1])
                 for previous, current in zip(lines, lines[1:]) if current.strip() == "/32 host LOCAL"}
    if not addresses:
        raise ValueError("host network state is unavailable")
    try:
        ipv6 = (HOST_NET / "if_inet6").read_text()
    except FileNotFoundError:
        if require_ipv6:
            raise
    else:
        addresses.update(ipaddress.IPv6Address(int(line.split()[0], 16))
                         for line in ipv6.splitlines())
    return addresses


def connected_networks():
    for line in (LOCAL_NET / "route").read_text().splitlines()[1:]:
        fields = line.split()
        if fields[2] == "00000000" and fields[7] != "00000000":
            address, mask = (ipaddress.IPv4Address(int(fields[i], 16).to_bytes(4, "little")) for i in (1, 7))
            yield ipaddress.IPv4Network(f"{address}/{mask}")
    ipv6 = LOCAL_NET / "ipv6_route"
    if ipv6.exists():
        for line in ipv6.read_text().splitlines():
            fields = line.split()
            if int(fields[1], 16) and int(fields[4], 16) == 0:
                yield ipaddress.IPv6Network((int(fields[0], 16), int(fields[1], 16)))


def check_destination(host, private_upstreams):
    """Reject numeric destinations outside the process's fixed IP policy."""
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
        address = getattr(address, "ipv4_mapped", None) or address
        if (address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast
                or (not address.is_global and address not in private_upstreams)
                or address in host_addresses(require_ipv6=address.version == 6)
                or any(address in network for network in connected_networks())):
            raise ValueError("host or control-plane destination")
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError):
        raise PermissionError(BLOCKED) from None


class UpstreamIsolation:
    def __init__(self, private_upstreams):
        self.private_upstreams = private_upstreams

    def running(self):
        # asyncio's TCP connector audits each numeric destination after DNS,
        # immediately before connect(). No second lookup or address rewriting.
        sys.addaudithook(self.audit)

    def server_connect(self, data):
        # mitmproxy's Rust UDP connector does not use Python's socket audit hook.
        if data.server.transport_protocol != "tcp":
            data.server.error = BLOCKED

    def dns_request(self, flow):
        # rawtcp=false still permits mitmproxy's DNS layer after CONNECT to
        # port 53/5353. Refuse it here: DNS flows never run the HTTP allowlist.
        flow.response = flow.request.fail(5)  # DNS REFUSED

    def audit(self, event, args):
        if event != "socket.connect" or args[0].family not in (socket.AF_INET, socket.AF_INET6):
            return
        check_destination(args[1][0], self.private_upstreams)
