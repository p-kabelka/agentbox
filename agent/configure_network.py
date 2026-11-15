#!/usr/bin/env python3
"""Configure missing passt IPv4 without adding a gateway or changing DNS."""

import argparse
import errno
import fcntl
import ipaddress
import json
import socket
import struct
import sys
from pathlib import Path

OCI_CONFIG = Path("/.krun_config.json")
SUBNET_CONFIG = Path("/agentbox-network/ipv4-subnets.json")
HOSTS = Path("/etc/hosts")
INTERFACE = b"eth0"
SIOCGIFFLAGS = 0x8913
SIOCSIFFLAGS = 0x8914
SIOCGIFADDR = 0x8915
SIOCSIFADDR = 0x8916
SIOCSIFNETMASK = 0x891C


def allocated_address(hosts: str, hostname: str, subnets: list[str]) -> ipaddress.IPv4Interface:
    if not isinstance(subnets, list) or not subnets:
        raise ValueError("expected a nonempty list of internal IPv4 subnets")
    networks = [ipaddress.IPv4Network(subnet) for subnet in subnets]
    if any(network.prefixlen == 0 for network in networks):
        raise ValueError("the passt workaround must not install a default route")
    candidates = set()
    for line in hosts.splitlines():
        fields = line.partition("#")[0].split()
        if len(fields) < 2 or hostname not in fields[1:]:
            continue
        address = ipaddress.ip_address(fields[0])
        if not isinstance(address, ipaddress.IPv4Address):
            continue
        if address.is_loopback or address.is_unspecified or address.is_multicast:
            continue
        for network in networks:
            if address not in network:
                continue
            if network.num_addresses > 2 and address in (network.network_address, network.broadcast_address):
                continue
            candidates.add(ipaddress.IPv4Interface(f"{address}/{network.prefixlen}"))
    if len(candidates) != 1:
        raise ValueError(f"expected one allocated IPv4 address for {hostname!r} in /etc/hosts; found {len(candidates)}")
    return candidates.pop()


def configure(subnets=None):
    try:
        config = json.loads(OCI_CONFIG.read_text())
    except FileNotFoundError:
        return False  # Ordinary crun containers have no krun configuration.
    if int(config.get("annotations", {}).get("krun.use_passt", "0")) <= 0:
        return False  # Do not change TSI or TAP networking.

    request = struct.pack("16s24x", INTERFACE)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
        try:
            current = fcntl.ioctl(control, SIOCGIFADDR, request)
        except OSError as exc:
            if exc.errno != errno.EADDRNOTAVAIL:
                raise
        else:
            if current[20:24] != b"\0" * 4:
                return False  # Preserve working DHCP or explicit configuration.

        if subnets is None:
            try:
                subnets = json.loads(SUBNET_CONFIG.read_text())["ipv4_subnets"]
            except FileNotFoundError as exc:
                raise ValueError(f"missing {SUBNET_CONFIG}; initialize a new session with krun.use_passt enabled, "
                                 "or pass --subnet for standalone use") from exc
        address = allocated_address(HOSTS.read_text(), socket.gethostname(), subnets)
        flags = fcntl.ioctl(control, SIOCGIFFLAGS, request)
        flags = struct.unpack_from("H", flags, 16)[0] | 1  # IFF_UP
        fcntl.ioctl(control, SIOCSIFFLAGS, struct.pack("16sH22x", INTERFACE, flags))
        for operation, value in ((SIOCSIFADDR, address.ip), (SIOCSIFNETMASK, address.netmask)):
            fcntl.ioctl(control, operation,
                        struct.pack("16sH2x4s16x", INTERFACE, socket.AF_INET, value.packed))

    # Setting the address/netmask creates only the connected subnet route.
    print(f"[agent] Configured passt IPv4 {address} (no default route)", file=sys.stderr)
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subnet", action="append", help="Internal IPv4 subnet for standalone podman run (repeatable)")
    args = parser.parse_args(argv)
    try:
        configure(args.subnet)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        print(f"[agent] Cannot configure passt IPv4: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
