"""Enforce agent egress in its outer network namespace, never inside the VM."""

import ipaddress
import json
import signal
import subprocess
import sys
import time
from pathlib import Path

READY = Path("/run/agentbox-network-ready")


def rules(endpoints):
    allowed = []
    proxy_ports = "{ 8080, 9090 }" if endpoints.get("vertex_metadata") is True else "8080"
    for service, ports in (("proxy", proxy_ports), ("git", "{ 9418, 8083 }")):
        address = ipaddress.IPv4Address(endpoints[service])
        if address.is_unspecified or address.is_loopback or address.is_multicast:
            raise ValueError(f"invalid {service} address")
        allowed.append(f"ip daddr {address} tcp dport {ports} accept")
    return "\n".join(allowed)


def install(allowed=""):
    # add is idempotent; the flush and replacement are one atomic transaction.
    # Loopback resets deliver rejection errors, not application connections.
    subprocess.run(["nft", "add", "table", "inet", "agentbox"], check=True)
    subprocess.run(["nft", "-f", "-"], input=f"""
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
                raise RuntimeError("network guard did not enter deny-all state")
            time.sleep(0.05)
        install(allowed)
        return
    if sys.argv[1:]:
        raise ValueError("expected no arguments or apply")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    READY.unlink(missing_ok=True)
    install()
    READY.touch()
    while True:
        signal.pause()


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Network guard failed: {exc}", file=sys.stderr)
        sys.exit(1)
