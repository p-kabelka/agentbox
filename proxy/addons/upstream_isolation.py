"""Keep HTTP proxy requests from reaching the host or the proxy's control plane."""

import asyncio
import ipaddress
import json
import os
import socket
from pathlib import Path

POLICY = Path("/config/network-policy.json")


class UpstreamIsolation:
    def __init__(self):
        self.enabled = os.environ.get("AGENTBOX_UPSTREAM_ISOLATION") == "1"

    async def server_connect(self, data):
        if not self.enabled or data.server.address is None:
            return
        server = data.server
        host, port = server.address
        try:
            policy = json.loads(POLICY.read_text())
            if not isinstance(policy, dict):
                raise ValueError("invalid network policy")
            blocked = policy["blocked"]
            private = policy.get("private_upstreams", [])
            if (not isinstance(blocked, list) or not blocked or not isinstance(private, list)
                    or any(not isinstance(value, str) for value in [*blocked, *private])):
                raise ValueError("invalid network policy")
            blocked = [ipaddress.ip_network(address, strict=False) for address in blocked]
            private = {ipaddress.ip_address(address) for address in private}
            answers = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
            addresses = [ipaddress.ip_address(answer[4][0]) for answer in answers]
            if not addresses:
                raise ValueError("no upstream address")
            for address in addresses:
                address = getattr(address, "ipv4_mapped", None) or address
                if (any(address in network for network in blocked) or address.is_loopback or address.is_link_local
                        or address.is_unspecified or address.is_multicast
                        or (not address.is_global and address not in private)):
                    raise ValueError("host or control-plane destination")
            # Pin the validated address: a second DNS lookup must not permit rebinding.
            # The default proxy bridge is IPv4-only, even on dual-stack hosts.
            address = next((a for a in addresses if a.version == 4), addresses[0])
            server.sni = server.sni or host
            server.address = (str(address), port)
        except (OSError, ValueError, TypeError, KeyError):
            server.error = "Agentbox blocked a host/control-plane destination or unavailable network policy"
