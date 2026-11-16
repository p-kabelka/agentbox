"""The outer firewall is an endpoint allowlist, not a guest routing convention."""

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("network_guard", ROOT / "network" / "guard.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class NetworkGuardTest(unittest.TestCase):
    def test_guard_refuses_to_run_inside_a_microvm(self):
        with patch.object(guard.Path, "is_file", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "native crun"):
                guard.main()

    def test_only_required_endpoint_ports_are_allowed(self):
        rules = guard.rules({"proxy": "10.123.0.2", "git": "10.123.0.3"})
        self.assertEqual(rules, "ip daddr 10.123.0.2 tcp dport 8080 accept\n"
                                "ip daddr 10.123.0.3 tcp dport { 9418, 8083 } accept")
        self.assertIn("{ 8080, 9090 }", guard.rules(
            {"proxy": "10.123.0.2", "git": "10.123.0.3", "vertex_metadata": True}))
        for endpoints in ({}, {"proxy": "127.0.0.1", "git": "10.123.0.3"},
                          {"proxy": "10.123.0.2; accept", "git": "10.123.0.3"},
                          {"proxy": "::1", "git": "10.123.0.3"}):
            with self.subTest(endpoints=endpoints), self.assertRaises((ValueError, KeyError)):
                guard.rules(endpoints)

    def test_install_is_atomic_and_defaults_to_deny_for_both_families(self):
        with patch.object(guard.subprocess, "run") as run:
            guard.install()
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ["nft", "-f", "-"])
        policy = run.call_args.kwargs["input"]
        self.assertLess(policy.index("add table inet agentbox"), policy.index("flush table inet agentbox"))
        self.assertIn("flush table inet agentbox", policy)
        self.assertEqual(policy.count("policy drop"), 3)
        self.assertNotIn("ip daddr", policy)
        self.assertIn('iifname "lo" accept', policy)  # Deliver local rejection errors to clients.
        self.assertIn("reject with tcp reset", policy)
        self.assertTrue(all(c.kwargs["check"] for c in run.call_args_list))

    def test_restart_restores_saved_policy_atomically_without_deny_all_gap(self):
        endpoints = {"proxy": "10.123.0.2", "git": "10.123.0.3"}
        with tempfile.TemporaryDirectory() as temporary:
            config, ready = Path(temporary) / "policy.json", Path(temporary) / "ready"
            config.write_text(json.dumps({"endpoints": endpoints}))
            with (patch.object(guard, "CONFIG", config), patch.object(guard, "READY", ready),
                  patch.object(guard.Path, "is_file", return_value=False),
                  patch.object(sys, "argv", ["guard.py"]),
                  patch.object(guard.signal, "signal"),
                  patch.object(guard.signal, "pause", side_effect=KeyboardInterrupt),
                  patch.object(guard, "install") as install, self.assertRaises(KeyboardInterrupt)):
                guard.main()
            install.assert_called_once_with(guard.rules(endpoints))
            self.assertTrue(ready.exists())

    def test_invalid_restart_policy_fails_closed_without_marking_ready(self):
        with tempfile.TemporaryDirectory() as temporary:
            config, ready = Path(temporary) / "policy.json", Path(temporary) / "ready"
            config.write_text('{"endpoints": null}')
            ready.touch()
            with (patch.object(guard, "CONFIG", config), patch.object(guard, "READY", ready),
                  patch.object(guard.Path, "is_file", return_value=False),
                  patch.object(sys, "argv", ["guard.py"]),
                  patch.object(guard.signal, "signal"),
                  patch.object(guard, "install") as install, self.assertRaises(ValueError)):
                guard.main()
            install.assert_called_once_with()
            self.assertFalse(ready.exists())

    def test_real_ipv4_ipv6_and_policy_replacement_in_private_namespace(self):
        if not all(shutil.which(tool) for tool in ("unshare", "ip", "nft")):
            self.skipTest("Requires unshare, iproute, and nftables")
        command = ["unshare", "--user", "--map-root-user", "--net"]
        probe = subprocess.run([*command, "nft", "list", "ruleset"], capture_output=True, text=True)
        if probe.returncode:
            self.skipTest("Unprivileged namespace nftables unavailable: " + probe.stderr.strip())
        script = """
import importlib.util, socket, subprocess, sys
spec = importlib.util.spec_from_file_location('guard', sys.argv[1])
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
def ip(*args):
    subprocess.run(['ip', *args], check=True)
ip('link', 'set', 'lo', 'up')
for address in ('10.123.0.1/24', '10.123.0.2/24', '10.123.0.3/24', 'fd00::2/64'):
    ip('address', 'add', address, 'dev', 'lo')
listeners = []
for family, address, ports in ((socket.AF_INET, '0.0.0.0', (8080,8081,8082,8083,9090,9418)),
                                (socket.AF_INET6, 'fd00::2', (8080,))):
    for port in ports:
        s = socket.socket(family)
        if family == socket.AF_INET6:
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        s.bind((address, port))
        s.listen()
        listeners.append(s)
def connected(address, port):
    try:
        with socket.create_connection((address, port), timeout=1):
            return True
    except OSError:
        return False
endpoints = {'proxy': '10.123.0.2', 'git': '10.123.0.3'}
def install():
    # Only this fixture puts servers in the guarded namespace. Permit their
    # reply-direction packets; new connections still exercise the real policy.
    guard.install(guard.rules(endpoints) + '\\noifname "lo" ct direction reply accept')
install()
for address, port in (('10.123.0.2',8080), ('10.123.0.3',9418), ('10.123.0.3',8083)):
    assert connected(address,port), (address,port)
for address, port in (('10.123.0.2',8081), ('10.123.0.2',8082), ('10.123.0.2',9090),
                       ('10.123.0.3',8080), ('10.123.0.1',8080), ('fd00::2',8080),
                       ('::ffff:10.123.0.1',8080), ('127.0.0.1',8080)):
    assert not connected(address,port), (address,port)
endpoints['vertex_metadata'] = True
install()
assert connected('10.123.0.2',9090)
guard.install()
assert not connected('10.123.0.2',8080), 'Deny-all must replace an existing allowlist'
"""
        result = subprocess.run([*command, sys.executable, "-c", script, str(ROOT / "network" / "guard.py")],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
