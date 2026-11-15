#!/usr/bin/env python3
"""Restore passt IPv4 and its connected subnet route without changing DNS or gateways."""

import argparse
import errno
import fcntl
import ipaddress
import json
import os
import socket
import struct
import sys
from pathlib import Path

OCI_CONFIG = Path("/.krun_config.json")
SUBNET_CONFIG = Path("/agentbox-network/ipv4-subnets.json")
HOSTS = Path("/etc/hosts")
ROUTES = Path("/proc/net/route")
INTERFACE = b"eth0"
SIOCGIFFLAGS = 0x8913
SIOCSIFFLAGS = 0x8914
SIOCGIFADDR = 0x8915
SIOCSIFADDR = 0x8916
SIOCGIFNETMASK = 0x891B
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


def has_connected_route(address: ipaddress.IPv4Interface) -> bool:
    destination = int.from_bytes(address.network.network_address.packed, sys.byteorder)
    mask = int.from_bytes(address.netmask.packed, sys.byteorder)
    for line in ROUTES.read_text().splitlines()[1:]:
        fields = line.split()
        if len(fields) < 8 or fields[0] != INTERFACE.decode():
            continue
        # Require an up, direct route, not a gateway or reject route.
        if (int(fields[1], 16) == destination and int(fields[2], 16) == 0
                and int(fields[7], 16) == mask and int(fields[3], 16) & 0x203 == 1):
            return True
    return False


def add_connected_route(address: ipaddress.IPv4Interface) -> None:
    # RTM_NEWROUTE: main table, kernel protocol, link scope, unicast.
    payload = struct.pack("BBBBBBBBI", socket.AF_INET, address.network.prefixlen, 0, 0, 254, 2, 253, 1, 0)
    payload += struct.pack("HH4s", 8, 1, address.network.network_address.packed)  # RTA_DST
    payload += struct.pack("HHI", 8, 4, socket.if_nametoindex(INTERFACE.decode()))  # RTA_OIF
    payload += struct.pack("HH4s", 8, 7, address.ip.packed)  # RTA_PREFSRC
    # REQUEST | ACK | REPLACE | CREATE; touch only this internal subnet route.
    packet = struct.pack("IHHII", 16 + len(payload), 24, 0x505, 1, 0) + payload
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE) as netlink:
        netlink.sendto(packet, (0, 0))
        reply = netlink.recv(65536)
    if len(reply) < 20 or struct.unpack_from("H", reply, 4)[0] != 2:  # NLMSG_ERROR ACK
        raise ValueError("invalid netlink acknowledgement for connected subnet route")
    error = struct.unpack_from("i", reply, 16)[0]
    if error:
        raise OSError(-error, os.strerror(-error))


def configure(subnets=None):
    try:
        config = json.loads(OCI_CONFIG.read_text())
    except FileNotFoundError:
        if SUBNET_CONFIG.is_file():
            print(f"[agent] Skipping passt IPv4: {OCI_CONFIG} is missing (not a krun guest)", file=sys.stderr)
        return False  # Ordinary crun containers have no krun configuration.
    if int(config.get("annotations", {}).get("krun.use_passt", "0")) <= 0:
        if SUBNET_CONFIG.is_file():
            print(f"[agent] Skipping passt IPv4: krun.use_passt is not enabled in {OCI_CONFIG}", file=sys.stderr)
        return False  # Do not change TSI or TAP networking.

    request = struct.pack("16s24x", INTERFACE)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
        current = b"\0" * 4
        try:
            current = fcntl.ioctl(control, SIOCGIFADDR, request)[20:24]
        except OSError as exc:
            if exc.errno != errno.EADDRNOTAVAIL:
                raise

        if subnets is None:
            try:
                subnets = json.loads(SUBNET_CONFIG.read_text())["ipv4_subnets"]
            except FileNotFoundError as exc:
                raise ValueError(f"missing {SUBNET_CONFIG}; initialize a new session with krun.use_passt enabled, "
                                 "or pass --subnet for standalone use") from exc
        address = allocated_address(HOSTS.read_text(), socket.gethostname(), subnets)
        netmask = None
        if current != b"\0" * 4:
            try:
                netmask = fcntl.ioctl(control, SIOCGIFNETMASK, request)[20:24]
            except OSError as exc:
                if exc.errno != errno.EADDRNOTAVAIL:
                    raise
        flags = fcntl.ioctl(control, SIOCGIFFLAGS, request)
        flags = struct.unpack_from("H", flags, 16)[0]
        if (current == address.ip.packed and netmask == address.netmask.packed
                and flags & 1 and has_connected_route(address)):
            print(f"[agent] Passt IPv4 {address} already configured (connected subnet route present)", file=sys.stderr)
            return False

        if not flags & 1:  # IFF_UP
            fcntl.ioctl(control, SIOCSIFFLAGS, struct.pack("16sH22x", INTERFACE, flags | 1))
        for operation, value in ((SIOCSIFADDR, address.ip), (SIOCSIFNETMASK, address.netmask)):
            fcntl.ioctl(control, operation,
                        struct.pack("16sH2x4s16x", INTERFACE, socket.AF_INET, value.packed))

    # Normally the ioctls create this route. Reapplying an unchanged address and
    # mask does not recreate a deleted route, so restore that case explicitly.
    if not has_connected_route(address):
        add_connected_route(address)
    print(f"[agent] Configured passt IPv4 {address} (no default route added)", file=sys.stderr)
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
