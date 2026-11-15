"""Gateway-less passt networking, without requiring Podman or network privileges."""

import errno
import importlib.util
import io
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
        self.oci.write_text(json.dumps({"annotations": {"krun.use_passt": "1"}}))
        self.subnets.write_text(json.dumps({"ipv4_subnets": ["10.89.40.0/24"]}))
        self.hosts.write_text("127.0.0.1 localhost agent-id\n::1 agent-id\n10.89.40.3 agent-id other-alias\n")
        self.current = None
        self.operations = []
        self.control = Mock()
        self.control.__enter__ = Mock(return_value=self.control)
        self.control.__exit__ = Mock(return_value=False)
        self.socket = Mock(return_value=self.control)
        for p in (patch.object(network, "OCI_CONFIG", self.oci),
                  patch.object(network, "SUBNET_CONFIG", self.subnets),
                  patch.object(network, "HOSTS", self.hosts),
                  patch.object(network.socket, "gethostname", return_value="agent-id"),
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
        if operation == network.SIOCGIFFLAGS:
            return struct.pack("16sH22x", b"eth0", 0x1002)
        return request

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
            with self.subTest(annotations=annotations):
                self.oci.write_text(json.dumps({"annotations": annotations}))
                self.assertFalse(network.configure())
        self.oci.unlink()
        self.assertFalse(network.configure())
        self.socket.assert_not_called()

    def test_existing_ipv4_skips_metadata_and_mutations(self):
        self.current = "10.89.40.3"
        self.subnets.unlink()
        self.hosts.unlink()
        self.assertFalse(network.configure())
        self.assertEqual([operation for operation, _ in self.operations], [network.SIOCGIFADDR])

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
    assert network.configure()
    assert not network.configure(), 'Already configured networking must be untouched'
    assert Path('/etc/resolv.conf').read_bytes() == resolv
    routes = [line.split() for line in Path('/proc/net/route').read_text().splitlines()[1:]]
    assert len(routes) == 1, routes
    assert routes[0][0] == 'eth0' and routes[0][1] != '00000000', routes
    assert routes[0][2] == '00000000', routes
    destination = socket.inet_ntoa(struct.pack('<I', int(routes[0][1], 16)))
    mask = socket.inet_ntoa(struct.pack('<I', int(routes[0][7], 16)))
    assert (destination, mask) == ('10.123.16.0', '255.255.240.0'), routes
"""
        result = subprocess.run([*command, sys.executable, "-c", script, str(ROOT / "agent" / "configure_network.py")],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
