import importlib.util
import shutil
import socket
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("upstream_guard", ROOT / "proxy/addons/upstream_isolation.py")
upstream = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upstream)


class UpstreamIsolationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ("HOST_NET", "LOCAL_NET"):
            directory = self.root / name
            directory.mkdir()
            p = patch.object(upstream, name, directory)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(upstream, "CONFIG", self.root / "proxy.yaml")
        p.start()
        self.addCleanup(p.stop)
        upstream.CONFIG.write_text("providers: []\n")
        self.hosts("9.9.9.9")
        (upstream.HOST_NET / "if_inet6").write_text("20014860486000000000000000008888 01 80 00 80 eth0\n")
        (upstream.LOCAL_NET / "route").write_text(
            "Iface Destination Gateway Flags RefCnt Use Metric Mask\n"
            "eth0 0000590A 00000000 0001 0 0 0 00FFFFFF\n"
            "eth0 00000000 0100590A 0003 0 0 0 00000000\n")
        (upstream.LOCAL_NET / "ipv6_route").write_text(
            "fd000000000000000000000000000000 40 00000000000000000000000000000000 00 "
            "00000000000000000000000000000000 00000100 00000000 00000000 00000001 eth0\n")
        self.guard = upstream.UpstreamIsolation()

    def hosts(self, *addresses):
        (upstream.HOST_NET / "fib_trie").write_text(
            "".join(f" |-- {address}\n    /32 host LOCAL\n" for address in addresses))

    def connect(self, address):
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        self.guard.audit("socket.connect", (types.SimpleNamespace(family=family), (address, 8081)))

    def test_actual_host_control_plane_and_loopback_destinations_are_denied(self):
        for address in ("10.89.0.1", "10.89.0.99", "9.9.9.9", "127.0.0.1", "::1", "fd00::2",
                        "::ffff:127.0.0.1", "::ffff:10.89.0.1", "169.254.1.2", "fe80::1",
                        "2001:4860:4860::8888", "2001:4860:4860::8888%eth0", "0.0.0.0", "224.0.0.1"):
            with self.subTest(address=address), self.assertRaisesRegex(PermissionError, "Agentbox blocked"):
                self.connect(address)

    def test_public_connections_do_not_change_mitmproxy_addresses_or_sni(self):
        for address in ("8.8.8.8", "2606:4700:4700::1111", "::ffff:8.8.8.8"):
            self.connect(address)
        server = types.SimpleNamespace(address=("allowed.example", 443), sni="allowed.example",
                                       transport_protocol="tcp", error=None)
        self.guard.server_connect(types.SimpleNamespace(server=server))
        self.assertEqual(server.address, ("allowed.example", 443))
        self.assertEqual(server.sni, "allowed.example")
        self.assertIsNone(server.error)
        server.transport_protocol = "udp"
        self.guard.server_connect(types.SimpleNamespace(server=server))
        self.assertIn("blocked", server.error)

    def test_host_address_changes_are_effective_without_reload(self):
        self.connect("8.8.8.8")
        self.hosts("9.9.9.9", "8.8.8.8")
        with self.assertRaises(PermissionError):
            self.connect("8.8.8.8")

    def test_private_exceptions_reload_without_overriding_hard_blocks(self):
        with self.assertRaises(PermissionError):
            self.connect("10.50.1.42")
        upstream.CONFIG.write_text(yaml.safe_dump({
            "private_upstreams": ["10.50.1.42", "10.89.0.3", "127.0.0.1", "9.9.9.9"],
        }))
        self.connect("10.50.1.42")
        for address in ("10.89.0.3", "127.0.0.1", "9.9.9.9"):
            with self.assertRaises(PermissionError):
                self.connect(address)

    def test_unavailable_network_state_or_invalid_policy_fails_closed(self):
        for invalid in ("not: [yaml", "[]", "null", "private_upstreams: not-a-list",
                        "private_upstreams: [not-an-ip]"):
            upstream.CONFIG.write_text(invalid)
            with self.assertRaises(PermissionError):
                self.connect("8.8.8.8")
        upstream.CONFIG.write_text("providers: []\n")
        (upstream.HOST_NET / "fib_trie").unlink()
        with self.assertRaises(PermissionError):
            self.connect("8.8.8.8")

    def test_only_internet_socket_connect_events_are_checked(self):
        self.guard.audit("socket.bind", ())
        self.guard.audit("socket.connect", (types.SimpleNamespace(family=socket.AF_UNIX), "/some/socket"))
        with patch.object(sys, "addaudithook") as register:
            self.guard.running()
        register.assert_called_once_with(self.guard.audit)

    def test_real_socket_audit_blocks_both_connect_apis_and_post_dns_addresses(self):
        # Audit hooks are process-wide and cannot be removed: isolate the test.
        script = """
import importlib.util, socket, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('guard', sys.argv[1])
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
guard.HOST_NET, guard.LOCAL_NET, guard.CONFIG = map(Path, sys.argv[2:])
guard.UpstreamIsolation().running()
with socket.socket() as client:
    try:
        client.connect(('9.9.9.9', 80))
        raise AssertionError('connect bypassed audit')
    except PermissionError:
        pass
    try:
        client.connect_ex(('9.9.9.9', 80))
        raise AssertionError('connect_ex bypassed audit')
    except PermissionError:
        pass
# This also exercises asyncio's numeric fast path and normal DNS path.
import asyncio
async def check():
    for host in ('9.9.9.9', 'localhost'):
        try:
            await asyncio.open_connection(host, 80)
            raise AssertionError('asyncio bypassed audit')
        except OSError as exc:
            assert 'Agentbox blocked' in str(exc), exc
asyncio.run(check())
"""
        result = subprocess.run([sys.executable, "-c", script, str(ROOT / "proxy/addons/upstream_isolation.py"),
                                 str(upstream.HOST_NET), str(upstream.LOCAL_NET), str(upstream.CONFIG)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_read_only_host_proc_mount_tracks_addresses_across_network_namespaces(self):
        if not all(shutil.which(tool) for tool in ("unshare", "ip", "mount")):
            self.skipTest("Requires unshare, iproute, and mount")
        command = ["unshare", "--user", "--map-root-user", "--net", "--mount"]
        probe = subprocess.run([*command, "true"], capture_output=True, text=True)
        if probe.returncode:
            self.skipTest(probe.stderr.strip())
        script = """
import os, subprocess, sys
from pathlib import Path
root = Path(sys.argv[2])
host = root / 'live-net'
host.mkdir()
subprocess.run(['ip', 'link', 'set', 'lo', 'up'], check=True)
subprocess.run(['mount', '--bind', f'/proc/{os.getpid()}/net', str(host)], check=True)
subprocess.run(['mount', '-o', 'remount,bind,ro', str(host)], check=True)
worker = '''
import importlib.util, socket, sys, types
from pathlib import Path
spec = importlib.util.spec_from_file_location("guard", sys.argv[1])
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
guard.HOST_NET = Path(sys.argv[2]) / "live-net"
guard.LOCAL_NET = Path(sys.argv[2]) / "proxy-net"
guard.CONFIG = Path(sys.argv[2]) / "config.yaml"
policy = guard.UpstreamIsolation()
def check():
    policy.audit("socket.connect", (types.SimpleNamespace(family=socket.AF_INET), ("192.0.0.9", 80)))
check()
print("ready", flush=True)
input()
try:
    check()
    raise AssertionError("New public host address was not blocked")
except PermissionError:
    pass
'''
(root / 'proxy-net').mkdir()
(root / 'proxy-net/route').write_text('Iface Destination Gateway Flags RefCnt Use Metric Mask\\n')
(root / 'config.yaml').write_text('providers: []\\n')
child = subprocess.Popen(['unshare', '--net', sys.executable, '-c', worker, sys.argv[1], str(root)],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
assert child.stdout.readline().strip() == 'ready'
subprocess.run(['ip', 'address', 'add', '192.0.0.9/32', 'dev', 'lo'], check=True)
stdout, stderr = child.communicate('\\n', timeout=10)
assert child.returncode == 0, (stdout, stderr)
"""
        result = subprocess.run([*command, sys.executable, "-c", script,
                                 str(ROOT / "proxy/addons/upstream_isolation.py"), str(self.root)],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
