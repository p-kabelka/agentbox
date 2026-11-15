"""Gateway-less passt networking, without requiring Podman or network privileges."""

import errno
import importlib.util
import io
import ipaddress
import json
import runpy
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("configure_network", ROOT / "agent" / "configure_network.py")
network = importlib.util.module_from_spec(spec)
spec.loader.exec_module(network)


class GuestPasstNetworkTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.oci = self.root / "oci.json"
        self.subnets = self.root / "subnets.json"
        self.hosts = self.root / "hosts"
        self.routes = self.root / "route"
        self.routes.write_text("Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n")
        self.oci.write_text(json.dumps({"annotations": {"krun.use_passt": "1"}}))
        self.subnets.write_text(json.dumps({"ipv4_subnets": ["10.89.40.0/24"]}))
        self.hosts.write_text("127.0.0.1 localhost agent-id\n::1 agent-id\n10.89.40.3 agent-id other-alias\n")
        self.current = None
        self.netmask = "255.255.255.0"
        self.flags = 0x1002
        self.auto_route = True
        self.operations = []
        self.control = Mock()
        self.control.__enter__ = Mock(return_value=self.control)
        self.control.__exit__ = Mock(return_value=False)
        self.netlink = Mock()
        self.netlink.__enter__ = Mock(return_value=self.netlink)
        self.netlink.__exit__ = Mock(return_value=False)
        self.netlink.recv.return_value = struct.pack("IHHIIi", 20, 2, 0, 1, 0, 0)
        self.socket = Mock(side_effect=lambda family, *args: self.netlink if family == socket.AF_NETLINK else self.control)
        for p in (patch.object(network, "OCI_CONFIG", self.oci),
                  patch.object(network, "SUBNET_CONFIG", self.subnets),
                  patch.object(network, "HOSTS", self.hosts),
                  patch.object(network, "ROUTES", self.routes),
                  patch.object(network.socket, "gethostname", return_value="agent-id"),
                  patch.object(network.socket, "if_nametoindex", return_value=7),
                  patch.object(network.socket, "socket", self.socket),
                  patch.object(network.fcntl, "ioctl", side_effect=self.ioctl)):
            p.start()
            self.addCleanup(p.stop)

    def ioctl(self, fd, operation, request):
        self.assertIs(fd, self.control)
        self.assertEqual(len(request), 40)
        self.assertEqual(request[:16].rstrip(b"\0"), b"eth0")
        self.operations.append((operation, request))
        if operation == network.SIOCGIFADDR:
            if self.current is None:
                raise OSError(errno.EADDRNOTAVAIL, "no IPv4 address")
            return struct.pack("16sH2x4s16x", b"eth0", socket.AF_INET, socket.inet_aton(self.current))
        if operation == network.SIOCGIFNETMASK:
            return struct.pack("16sH2x4s16x", b"eth0", socket.AF_INET, socket.inet_aton(self.netmask))
        if operation == network.SIOCGIFFLAGS:
            return struct.pack("16sH22x", b"eth0", self.flags)
        if operation == network.SIOCSIFFLAGS:
            self.flags = struct.unpack_from("H", request, 16)[0]
        if operation == network.SIOCSIFADDR:
            self.current = socket.inet_ntoa(request[20:24])
        if operation == network.SIOCSIFNETMASK:
            self.netmask = socket.inet_ntoa(request[20:24])
            if self.auto_route:
                self.add_connected_route()
        return request

    def add_connected_route(self):
        subnet = ipaddress.IPv4Network(f"{self.current}/{self.netmask}", strict=False)
        destination = int.from_bytes(subnet.network_address.packed, sys.byteorder)
        mask = int.from_bytes(subnet.netmask.packed, sys.byteorder)
        with self.routes.open("a") as stream:
            stream.write(f"eth0\t{destination:08X}\t00000000\t0001\t0\t0\t0\t{mask:08X}\t0\t0\t0\n")

    def configured_values(self):
        return [(operation, socket.inet_ntoa(request[20:24])) for operation, request in self.operations
                if operation in (network.SIOCSIFADDR, network.SIOCSIFNETMASK)]

    def test_missing_address_uses_allocated_ip_and_only_connected_subnet(self):
        with redirect_stderr(io.StringIO()) as output:
            self.assertTrue(network.configure())
        self.assertIn("10.89.40.3/24", output.getvalue())
        self.socket.assert_called_once_with(socket.AF_INET, socket.SOCK_DGRAM)
        self.assertEqual([operation for operation, _ in self.operations],
                         [network.SIOCGIFADDR, network.SIOCGIFFLAGS, network.SIOCSIFFLAGS,
                          network.SIOCSIFADDR, network.SIOCSIFNETMASK])
        self.assertEqual(struct.unpack_from("H", self.operations[2][1], 16)[0], 0x1003)
        self.assertEqual(self.configured_values(), [(network.SIOCSIFADDR, "10.89.40.3"),
                                                   (network.SIOCSIFNETMASK, "255.255.255.0")])
        self.assertEqual(self.hosts.read_text(), "127.0.0.1 localhost agent-id\n::1 agent-id\n10.89.40.3 agent-id other-alias\n")

    def test_ordinary_container_tsi_and_tap_are_unchanged(self):
        for annotations in ({}, {"krun.use_passt": "0"}, {"krun.tap_name": "tap0"}):
            with self.subTest(annotations=annotations), redirect_stderr(io.StringIO()) as output:
                self.oci.write_text(json.dumps({"annotations": annotations}))
                self.assertFalse(network.configure())
                self.assertIn("krun.use_passt is not enabled", output.getvalue())
        self.oci.unlink()
        with redirect_stderr(io.StringIO()) as output:
            self.assertFalse(network.configure())
        self.assertIn("not a krun guest", output.getvalue())
        self.subnets.unlink()
        with redirect_stderr(io.StringIO()) as output:
            self.assertFalse(network.configure())
        self.assertEqual(output.getvalue(), "")
        self.socket.assert_not_called()

    def test_matching_address_prefix_link_and_route_are_unchanged_and_reported(self):
        self.current = "10.89.40.3"
        self.flags |= 1
        self.add_connected_route()
        with redirect_stderr(io.StringIO()) as output:
            self.assertFalse(network.configure())
        self.assertIn("already configured", output.getvalue())
        self.assertEqual(self.configured_values(), [])
        self.assertNotIn(network.SIOCSIFFLAGS, [operation for operation, _ in self.operations])
        self.netlink.sendto.assert_not_called()

    def test_existing_address_with_wrong_prefix_or_ip_is_repaired(self):
        for current, netmask in (("10.89.40.3", "255.255.255.255"),
                                 ("10.89.40.3", "255.0.0.0"),
                                 ("169.254.2.2", "255.255.0.0"),
                                 ("10.89.40.9", "255.255.255.0")):
            with self.subTest(current=current, netmask=netmask):
                self.current, self.netmask = current, netmask
                self.flags |= 1
                self.operations.clear()
                self.add_connected_route()
                with redirect_stderr(io.StringIO()) as output:
                    self.assertTrue(network.configure())
                self.assertIn("Configured passt IPv4 10.89.40.3/24", output.getvalue())
                self.assertEqual(self.configured_values(), [(network.SIOCSIFADDR, "10.89.40.3"),
                                                           (network.SIOCSIFNETMASK, "255.255.255.0")])

    def test_matching_address_and_prefix_with_missing_route_are_repaired(self):
        self.current = "10.89.40.3"
        self.flags |= 1
        self.auto_route = False  # Reapplying an unchanged address/mask need not restore a deleted route.
        with redirect_stderr(io.StringIO()):
            self.assertTrue(network.configure())
        self.socket.assert_any_call(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE)
        packet, destination = self.netlink.sendto.call_args.args
        self.assertEqual(destination, (0, 0))
        self.assertEqual(struct.unpack_from("IHHII", packet), (len(packet), 24, 0x505, 1, 0))
        self.assertEqual(struct.unpack_from("BBBBBBBBI", packet, 16),
                         (socket.AF_INET, 24, 0, 0, 254, 2, 253, 1, 0))
        self.assertEqual(packet[32:36], socket.inet_aton("10.89.40.0"))
        self.assertEqual(struct.unpack_from("I", packet, 40)[0], 7)
        self.assertEqual(packet[48:52], socket.inet_aton("10.89.40.3"))

    def test_default_gateway_or_unusable_route_does_not_count_as_connected(self):
        self.current = "10.89.40.3"
        self.flags |= 1
        self.add_connected_route()
        connected = self.routes.read_text().splitlines()[-1].split()
        invalid = [(0, "other0"), (1, "00000000"), (2, "0128590A"),
                   (3, "0000"), (3, "0003"), (3, "0201"), (7, "FFFFFFFF")]
        for field, value in invalid:
            with self.subTest(field=field, value=value):
                row = connected.copy()
                row[field] = value
                self.routes.write_text("header\n" + "\t".join(row) + "\n")
                self.operations.clear()
                with redirect_stderr(io.StringIO()):
                    self.assertTrue(network.configure())
                self.assertTrue(self.configured_values())

    def test_matching_address_and_route_on_a_down_link_are_repaired(self):
        self.current = "10.89.40.3"
        self.add_connected_route()
        with redirect_stderr(io.StringIO()):
            self.assertTrue(network.configure())
        self.assertTrue(self.flags & 1)

    def test_route_repair_failure_is_not_reported_as_success(self):
        self.current = "10.89.40.3"
        self.auto_route = False
        for reply in (struct.pack("IHHIIi", 20, 2, 0, 1, 0, -errno.EPERM), b"short"):
            with self.subTest(reply=reply), redirect_stderr(io.StringIO()) as output:
                self.netlink.recv.return_value = reply
                self.assertEqual(network.main([]), 1)
            self.assertIn("Cannot configure passt IPv4", output.getvalue())
            self.assertNotIn("[agent] Configured passt IPv4", output.getvalue())

    def test_zero_address_and_new_boot_use_current_podman_hosts_and_prefix(self):
        self.current = "0.0.0.0"
        with redirect_stderr(io.StringIO()):
            network.configure()
            self.hosts.write_text("10.100.18.19 agent-id\n")
            self.subnets.write_text(json.dumps({"ipv4_subnets": ["10.100.16.0/20"]}))
            network.configure()
        self.assertEqual(self.configured_values()[-2:], [(network.SIOCSIFADDR, "10.100.18.19"),
                                                        (network.SIOCSIFNETMASK, "255.255.240.0")])

    def test_standalone_subnet_option_needs_no_metadata_mount(self):
        self.subnets.unlink()
        with redirect_stderr(io.StringIO()):
            self.assertEqual(network.main(["--subnet", "10.89.40.0/24"]), 0)
        self.assertEqual(self.configured_values()[0], (network.SIOCSIFADDR, "10.89.40.3"))

    def test_invalid_or_ambiguous_addresses_fail_before_any_changes(self):
        cases = [([], "10.89.40.3 agent-id\n"),
                 (["0.0.0.0/0"], "10.89.40.3 agent-id\n"),
                 (["10.89.40.0/24"], "10.89.41.3 agent-id\n"),
                 (["10.89.40.0/24"], "10.89.40.3 another-agent-id\n"),
                 (["10.89.40.0/24"], "10.89.40.0 agent-id\n10.89.40.255 agent-id\n"),
                 (["10.89.40.0/24"], "10.89.40.3 agent-id\n10.89.40.4 agent-id\n"),
                 (["127.0.0.0/8"], "127.0.0.1 agent-id\n")]
        for subnets, hosts in cases:
            with self.subTest(subnets=subnets, hosts=hosts):
                self.operations.clear()
                self.subnets.write_text(json.dumps({"ipv4_subnets": subnets}))
                self.hosts.write_text(hosts)
                with self.assertRaises(ValueError):
                    network.configure()
                self.assertEqual([operation for operation, _ in self.operations], [network.SIOCGIFADDR])

    def test_duplicate_hosts_entries_do_not_make_the_address_ambiguous(self):
        self.hosts.write_text("10.89.40.3 agent-id\n10.89.40.3 agent-id # repeated alias\n")
        with redirect_stderr(io.StringIO()):
            self.assertTrue(network.configure())

    def test_missing_metadata_or_ioctl_failure_is_actionable_and_fails_closed(self):
        self.subnets.unlink()
        with redirect_stderr(io.StringIO()) as output:
            self.assertEqual(network.main([]), 1)
        self.assertIn("Cannot configure passt IPv4", output.getvalue())
        self.assertIn(str(self.subnets), output.getvalue())
        with (patch.object(network.fcntl, "ioctl", side_effect=PermissionError("denied")),
              redirect_stderr(io.StringIO()) as output):
            self.assertEqual(network.main([]), 1)
        self.assertIn("denied", output.getvalue())
        self.assertEqual(self.configured_values(), [])

    def test_image_runs_helper_before_ca_wait_git_clone_and_harness(self):
        containerfile = (ROOT / "agent" / "Containerfile.base").read_text()
        self.assertIn("COPY --chmod=755 configure_network.py /usr/local/bin/agentbox-network", containerfile)
        start = (ROOT / "agent" / "start.sh").read_text()
        self.assertLess(start.index("/usr/local/bin/agentbox-network"), start.index("until [ -f"))
        self.assertLess(start.index("/usr/local/bin/agentbox-network"), start.index("/usr/local/bin/agentbox-clone"))


class HostPasstNetworkTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.session = Path(temporary.name) / "session"
        self.session.mkdir()
        self.project = self.session.parent / "project"
        self.project.mkdir()
        self.agentbox = runpy.run_path(str(ROOT / "bin" / "agentbox"))
        self.g = self.agentbox["main"].__globals__
        p = patch.dict(self.g, AGENTBOX_HOME=ROOT, _resolve_images=lambda images: images)
        p.start()
        self.addCleanup(p.stop)
        self.git = {"NetworkSettings": {"Networks": {"session_agent-net": {"IPAddress": "10.89.40.4"}}}}
        self.network = {"internal": True, "subnets": [{"subnet": "10.89.40.0/24", "gateway": "10.89.40.1"},
                                                    {"subnet": "fd00::/64", "gateway": "fd00::1"}]}

    def generate(self, annotations=None):
        self.agentbox["generate_compose"](self.session, self.project, "feature", 9876, {},
                                           {"annotations": annotations or {}}, [])
        return yaml.safe_load((self.session / "compose.yaml").read_text())

    def inspect(self, args, **kwargs):
        self.assertTrue(kwargs["check"])
        if args[:2] == ["podman", "inspect"]:
            self.assertEqual(args[2], self.agentbox["_persistent_name"](self.session, "git"))
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps([self.git]))
        self.assertEqual(args, ["podman", "network", "inspect", "session_agent-net"])
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps([self.network]))

    def test_passt_adds_only_a_read_only_metadata_mount_to_the_agent(self):
        cfg = self.generate({"krun.use_passt": "1", "krun.ram_mib": "3072"})
        self.assertTrue((self.session / "network-config").is_dir())
        self.assertIn(self.g["NETWORK_CONFIG_VOLUME"], cfg["services"]["agent"]["volumes"])
        self.assertEqual(cfg["services"]["agent"]["networks"], ["agent-net"])
        self.assertTrue(cfg["networks"]["agent-net"]["internal"])
        self.assertNotIn("ipam", cfg["networks"]["agent-net"])
        for service in ("proxy", "git"):
            self.assertNotIn(self.g["NETWORK_CONFIG_VOLUME"], cfg["services"][service]["volumes"])

    def test_default_and_explicitly_disabled_passt_do_not_inspect_networks(self):
        for annotations in ({}, {"krun.use_passt": "0"}):
            with self.subTest(annotations=annotations), patch.object(subprocess, "run") as run:
                cfg = self.generate(annotations)
                self.assertNotIn(self.g["NETWORK_CONFIG_VOLUME"], cfg["services"]["agent"]["volumes"])
                self.agentbox["_prepare_agent_network"](self.session)
                run.assert_not_called()

    def test_prepare_refreshes_only_real_ipv4_subnets_without_static_agent_ips(self):
        self.generate({"krun.use_passt": "1"})
        directory = self.session / "network-config"
        config = directory / "ipv4-subnets.json"
        compose = (self.session / "compose.yaml").read_bytes()
        with patch.object(subprocess, "run", side_effect=self.inspect) as run:
            self.agentbox["_prepare_agent_network"](self.session)
            self.assertEqual(json.loads(config.read_text()), {"ipv4_subnets": ["10.89.40.0/24"]})
            self.network["subnets"] = [{"subnet": "10.100.16.0/20"}]
            self.agentbox["_prepare_agent_network"](self.session)
        self.assertEqual(json.loads(config.read_text()), {"ipv4_subnets": ["10.100.16.0/20"]})
        self.assertEqual(run.call_count, 4)
        self.assertEqual(list(directory.iterdir()), [config])
        self.assertEqual((self.session / "compose.yaml").read_bytes(), compose)

    def test_metadata_preparation_precedes_ephemeral_agent_launch(self):
        self.generate({"krun.use_passt": "1"})
        events = []

        def compose(session, *args, **kwargs):
            events.append(args[0])
            return subprocess.CompletedProcess(args, 0)

        with (patch.dict(self.g, compose=compose,
                         _secret_preflight=lambda session: ("config", "providers", []),
                         _synchronize_proxy=lambda *args: events.append("sync"),
                         _prepare_agent_network=lambda session: events.append("network")),
              self.assertRaises(SystemExit) as result):
            self.agentbox["_launch"](self.session, ["bash"])
        self.assertEqual(result.exception.code, 0)
        self.assertEqual(events, ["up", "sync", "network", "run", "down"])

    def test_missing_ambiguous_or_invalid_sidecar_network_is_rejected(self):
        self.generate({"krun.use_passt": "1"})
        for networks in ({}, {"one": {}, "two": {}}, None, ["session_agent-net"]):
            self.git["NetworkSettings"]["Networks"] = networks
            with (self.subTest(networks=networks), patch.object(subprocess, "run", side_effect=self.inspect) as run,
                  redirect_stderr(io.StringIO()), self.assertRaises(SystemExit)):
                self.agentbox["_prepare_agent_network"](self.session)
            self.assertEqual(run.call_count, 1)
        self.assertEqual(list((self.session / "network-config").iterdir()), [])

    def test_invalid_passt_annotation_preserves_saved_compose(self):
        self.generate()
        original = (self.session / "compose.yaml").read_bytes()
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.generate({"krun.use_passt": "invalid"})
        self.assertEqual((self.session / "compose.yaml").read_bytes(), original)

    def test_external_missing_or_invalid_subnet_does_not_replace_previous_metadata(self):
        self.generate({"krun.use_passt": "1"})
        config = self.session / "network-config" / "ipv4-subnets.json"
        config.write_text("previous metadata\n")
        cases = [{"internal": False, "subnets": [{"subnet": "10.89.40.0/24"}]},
                 {"internal": True, "subnets": []},
                 {"internal": True, "subnets": [{"subnet": "0.0.0.0/0"}]},
                 {"internal": True, "subnets": [{"subnet": "fd00::/64"}]},
                 {"internal": True, "subnets": [{"subnet": "invalid"}]}]
        for value in cases:
            self.network = value
            with (self.subTest(network=value), patch.object(subprocess, "run", side_effect=self.inspect),
                  redirect_stderr(io.StringIO()), self.assertRaises(SystemExit)):
                self.agentbox["_prepare_agent_network"](self.session)
            self.assertEqual(config.read_text(), "previous metadata\n")
            self.assertEqual(list(config.parent.iterdir()), [config])


class LinuxPasstNetworkTest(unittest.TestCase):
    def test_real_ioctls_in_an_isolated_network_namespace(self):
        if not shutil.which("unshare"):
            self.skipTest("Install unshare to verify real Linux network ioctls")
        command = ["unshare", "--user", "--map-root-user", "--net"]
        probe = subprocess.run([*command, "true"], capture_output=True, text=True, timeout=10)
        if probe.returncode:
            self.skipTest("Unprivileged network namespaces unavailable: " + probe.stderr.strip())
        script = r"""
import importlib.util, json, socket, struct, sys, tempfile
from pathlib import Path

# Create a dummy eth0 with netlink: no iproute package or host network changes.
def attribute(kind, data):
    size = 4 + len(data)
    return struct.pack('HH', size, kind) + data + b'\0' * (-size % 4)

payload = struct.pack('BxHiII', socket.AF_UNSPEC, 0, 0, 0, 0)
payload += attribute(3, b'eth0\0')  # IFLA_IFNAME
payload += attribute(18 | 0x8000, attribute(1, b'dummy\0'))  # IFLA_LINKINFO/INFO_KIND
with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE) as netlink:
    netlink.sendto(struct.pack('IHHII', 16 + len(payload), 16, 0x605, 1, 0) + payload, (0, 0))
    reply = netlink.recv(65536)
    assert struct.unpack_from('i', reply, 16)[0] == 0, reply

spec = importlib.util.spec_from_file_location('configure_network', sys.argv[1])
network = importlib.util.module_from_spec(spec)
spec.loader.exec_module(network)
with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    network.OCI_CONFIG = root / 'oci.json'
    network.SUBNET_CONFIG = root / 'subnets.json'
    network.HOSTS = root / 'hosts'
    network.OCI_CONFIG.write_text(json.dumps({'annotations': {'krun.use_passt': '1'}}))
    network.SUBNET_CONFIG.write_text(json.dumps({'ipv4_subnets': ['10.123.16.0/20']}))
    network.HOSTS.write_text(f'10.123.17.3 {socket.gethostname()}\n')
    resolv = Path('/etc/resolv.conf').read_bytes()

    def assert_configured():
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
            request = struct.pack('16s24x', b'eth0')
            for operation, expected in ((network.SIOCGIFADDR, '10.123.17.3'),
                                        (network.SIOCGIFNETMASK, '255.255.240.0')):
                reply = network.fcntl.ioctl(control, operation, request)
                assert socket.inet_ntoa(reply[20:24]) == expected, reply
        assert Path('/etc/resolv.conf').read_bytes() == resolv
        routes = [line.split() for line in Path('/proc/net/route').read_text().splitlines()[1:]]
        assert len(routes) == 1, routes
        assert routes[0][0] == 'eth0' and routes[0][1] != '00000000', routes
        assert routes[0][2] == '00000000', routes
        destination = socket.inet_ntoa(struct.pack('=I', int(routes[0][1], 16)))
        mask = socket.inet_ntoa(struct.pack('=I', int(routes[0][7], 16)))
        assert (destination, mask) == ('10.123.16.0', '255.255.240.0'), routes

    assert network.configure()
    assert not network.configure(), 'Already configured networking must be untouched'
    assert_configured()

    # Existing IPv4 is not sufficient: reproduce a wrong prefix and a fallback IP.
    for address, mask in (('10.123.17.3', '255.255.255.255'),
                          ('10.123.17.3', '255.0.0.0'),
                          ('169.254.2.2', '255.255.0.0')):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
            for operation, value in ((network.SIOCSIFADDR, address), (network.SIOCSIFNETMASK, mask)):
                network.fcntl.ioctl(control, operation,
                                    struct.pack('16sH2x4s16x', b'eth0', socket.AF_INET, socket.inet_aton(value)))
        assert network.configure(), (address, mask)
        assert_configured()

    # Deleting only the route leaves the correct IP/prefix behind. The helper
    # must restore it even when reapplying unchanged ioctls does not do so.
    payload = struct.pack('BBBBBBBBI', socket.AF_INET, 20, 0, 0, 254, 2, 253, 1, 0)
    payload += attribute(1, socket.inet_aton('10.123.16.0'))
    payload += attribute(4, struct.pack('I', socket.if_nametoindex('eth0')))
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE) as netlink:
        netlink.sendto(struct.pack('IHHII', 16 + len(payload), 25, 5, 1, 0) + payload, (0, 0))
        reply = netlink.recv(65536)
        assert struct.unpack_from('i', reply, 16)[0] == 0, reply
    address = network.allocated_address(network.HOSTS.read_text(), socket.gethostname(), ['10.123.16.0/20'])
    assert not network.has_connected_route(address)
    assert network.configure(), 'Correct IP/prefix without a connected route must be repaired'
    assert_configured()

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
        request = struct.pack('16s24x', b'eth0')
        flags = struct.unpack_from('H', network.fcntl.ioctl(control, network.SIOCGIFFLAGS, request), 16)[0]
        network.fcntl.ioctl(control, network.SIOCSIFFLAGS, struct.pack('16sH22x', b'eth0', flags & ~1))
    assert network.configure(), 'A down link with an existing address must be repaired'
    assert_configured()
"""
        result = subprocess.run([*command, sys.executable, "-c", script, str(ROOT / "agent" / "configure_network.py")],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
