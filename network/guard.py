"""Enforce agent egress in its outer network namespace, never inside the VM."""

import ipaddress
import json
import signal
import subprocess
import sys
import time
from pathlib import Path

READY = Path("/run/agentbox-network-ready")
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
    if sys.argv[1:] == ["apply"]:
        allowed = rules(json.load(sys.stdin))
        deadline = time.monotonic() + 10
        while not READY.is_file():
            if time.monotonic() >= deadline:
                raise RuntimeError("network guard did not become ready")
            time.sleep(0.05)
        install(allowed)
        return
    if sys.argv[1:]:
        raise ValueError("expected no arguments or apply")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    READY.unlink(missing_ok=True)
    try:
        allowed = rules(json.loads(CONFIG.read_text())["endpoints"])
    except (OSError, ValueError, TypeError, KeyError):
        install()  # Invalid policy fails closed, without stopping the sandbox.
        raise
    # Kernel rules survive guard exit. Replace them atomically on restart,
    # without a deny-all interval that would reset active TCP connections.
    install(allowed)
    READY.touch()
    while True:
        signal.pause()


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Network guard failed: {exc}", file=sys.stderr)
        sys.exit(1)
