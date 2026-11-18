"""Install agent egress rules in its current outer namespace, then exit."""

import ipaddress
import json
import subprocess
import sys
from pathlib import Path

CONFIG = Path("/agentbox-network/network.json")


def rules(endpoints):
    if not isinstance(endpoints, dict):
        raise ValueError("expected an endpoint mapping")
    allowed = []
    proxy_ports = "{ 8080, 9090 }" if endpoints.get("vertex_metadata") is True else "8080"
    for service, ports in (("proxy", proxy_ports), ("git", "{ 9418, 8083 }")):
        address = ipaddress.IPv4Address(endpoints[service])
        if address.is_unspecified or address.is_loopback or address.is_multicast:
            raise ValueError(f"invalid {service} address")
        allowed.append(f"ip daddr {address} tcp dport {ports} accept")
    # krun's TSI backend uses sockets in this private namespace even for
    # guest localhost. Require the loopback interface AND a loopback address.
    allowed.extend((
        'oifname "lo" ip daddr 127.0.0.0/8 meta l4proto tcp accept',
        'oifname "lo" ip6 daddr ::1 meta l4proto tcp accept',
    ))
    return "\n".join(allowed)


def install(allowed=""):
    # add is idempotent; creation, flush, and replacement are one transaction.
    # Loopback resets deliver rejection errors, not application connections.
    subprocess.run(["nft", "-f", "-"], input=f"""
add table inet agentbox
flush table inet agentbox
table inet agentbox {{
    chain output {{
        type filter hook output priority 0; policy drop;
        {allowed}
        oifname "lo" tcp flags & rst == rst accept
        meta l4proto tcp reject with tcp reset
    }}
    chain input {{
        type filter hook input priority 0; policy drop;
        iifname "lo" accept
        ct state established,related accept
    }}
    chain forward {{
        type filter hook forward priority 0; policy drop;
    }}
}}
""", text=True, check=True)


def main():
    if Path("/.krun_config.json").is_file():
        raise RuntimeError("network guard must use native crun, not a microVM")
    try:
        allowed = rules(json.loads(CONFIG.read_text())["endpoints"])
    except (OSError, ValueError, TypeError, KeyError):
        install()  # Invalid policy fails closed, without stopping the sandbox.
        raise
    # Kernel rules survive installer exit. Replace them atomically on refresh,
    # without a deny-all interval that would reset active TCP connections.
    install(allowed)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Network guard failed: {exc}", file=sys.stderr)
        sys.exit(1)
