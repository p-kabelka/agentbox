#!/usr/bin/env python3
"""Restore passt IPv4 and its connected subnet route without changing DNS or gateways."""

import argparse
import ipaddress
import json
import socket
import subprocess
import sys
from pathlib import Path

OCI_CONFIG = Path("/.krun_config.json")
SUBNET_CONFIG = Path("/agentbox-network/ipv4-subnets.json")
HOSTS = Path("/etc/hosts")


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
        if SUBNET_CONFIG.is_file():
            print(f"[agent] Skipping passt IPv4: {OCI_CONFIG} is missing (not a krun guest)", file=sys.stderr)
        return False  # Ordinary crun containers have no krun configuration.
    if int(config.get("annotations", {}).get("krun.use_passt", "0")) <= 0:
        if SUBNET_CONFIG.is_file():
            print(f"[agent] Skipping passt IPv4: krun.use_passt is not enabled in {OCI_CONFIG}", file=sys.stderr)
        return False  # Do not change TSI or TAP networking.

    if subnets is None:
        try:
            subnets = json.loads(SUBNET_CONFIG.read_text())["ipv4_subnets"]
        except FileNotFoundError as exc:
            raise ValueError(f"missing {SUBNET_CONFIG}; initialize a new session with krun.use_passt enabled, "
                             "or pass --subnet for standalone use") from exc
    address = allocated_address(HOSTS.read_text(), socket.gethostname(), subnets)
    # Reapply only the required configuration. These operations are idempotent;
    # leave unrelated addresses/routes and Podman's resolver configuration alone.
    for args in (["link", "set", "dev", "eth0", "up"],
                 ["address", "replace", str(address), "dev", "eth0"],
                 ["route", "replace", str(address.network), "dev", "eth0", "src", str(address.ip)]):
        subprocess.run(["ip", *args], check=True)
    print(f"[agent] Configured passt IPv4 {address} (no default route added)", file=sys.stderr)
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subnet", action="append", help="Internal IPv4 subnet for standalone podman run (repeatable)")
    args = parser.parse_args(argv)
    try:
        configure(args.subnet)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.CalledProcessError) as exc:
        print(f"[agent] Cannot configure passt IPv4: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
