#!/usr/bin/env python3
"""Configure managed sidecar names and restore passt IPv4 without adding a gateway."""

import ipaddress
import json
import socket
import subprocess
import sys
from pathlib import Path

OCI_CONFIG = Path("/.krun_config.json")
NETWORK_CONFIG = Path("/agentbox-network/network.json")
HOSTS = Path("/etc/hosts")
RESOLV = Path("/etc/resolv.conf")


def configure():
    metadata = json.loads(NETWORK_CONFIG.read_text())
    address = ipaddress.IPv4Interface(metadata["ipv4"])
    endpoints = {name: ipaddress.IPv4Address(metadata["endpoints"][name]) for name in ("proxy", "git")}
    if not address.network.prefixlen:
        raise ValueError("the passt workaround must not install a default route")
    endpoints[socket.gethostname()] = address.ip
    lines = []
    for line in HOSTS.read_text().splitlines():
        fields = line.partition("#")[0].split()
        if len(fields) > 1 and endpoints.keys() & set(fields[1:]):
            fields = [fields[0], *(name for name in fields[1:] if name not in endpoints)]
            if len(fields) == 1:
                continue
            line = "\t".join(fields)
        lines.append(line)
    HOSTS.write_text("\n".join([*lines, *(f"{ip}\t{name}" for name, ip in endpoints.items())]) + "\n")
    RESOLV.write_text("nameserver 127.0.0.1\noptions timeout:1 attempts:1\n")
    if not OCI_CONFIG.is_file():
        return False
    config = json.loads(OCI_CONFIG.read_text())
    if int(config.get("annotations", {}).get("krun.use_passt", "0")) <= 0:
        return False
    for args in (["link", "set", "dev", "eth0", "up"],
                 ["address", "replace", str(address), "dev", "eth0"],
                 ["route", "replace", str(address.network), "dev", "eth0", "src", str(address.ip)]):
        subprocess.run(["ip", *args], check=True)
    return True


def main():
    try:
        configure()
    except (OSError, ValueError, TypeError, KeyError, subprocess.CalledProcessError) as exc:
        print(f"[agent] Cannot configure network: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
