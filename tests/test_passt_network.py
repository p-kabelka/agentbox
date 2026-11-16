"""Managed guest configuration and host policy setup."""

import importlib.util
import io
import json
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import call, patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("configure_network", ROOT / "agent/configure_network.py")
network = importlib.util.module_from_spec(spec)
spec.loader.exec_module(network)


class GuestPasstNetworkTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.metadata = {"ipv4": "10.89.40.6/24", "endpoints": {"proxy": "10.89.40.5", "git": "10.89.40.4"}}
        for name, filename in (("OCI_CONFIG", "oci.json"), ("NETWORK_CONFIG", "network.json"),
                               ("HOSTS", "hosts"), ("RESOLV", "resolv.conf")):
            p = patch.object(network, name, self.root / filename)
            p.start()
            self.addCleanup(p.stop)
        network.OCI_CONFIG.write_text(json.dumps({"annotations": {"krun.use_passt": "1"}}))
        network.NETWORK_CONFIG.write_text(json.dumps(self.metadata))
        network.HOSTS.write_text("127.0.0.1 localhost\n10.1.1.1 proxy old-alias\n")
        p = patch.object(network.subprocess, "run")
        self.run = p.start()
        self.addCleanup(p.stop)

    def test_passt_configuration_is_idempotent_and_uses_current_metadata(self):
        self.assertTrue(network.configure())
        self.assertTrue(network.configure())
        self.assertEqual(self.run.call_args_list, [
            call(["ip", "link", "set", "dev", "eth0", "up"], check=True),
            call(["ip", "address", "replace", "10.89.40.6/24", "dev", "eth0"], check=True),
            call(["ip", "route", "replace", "10.89.40.0/24", "dev", "eth0", "src", "10.89.40.6"], check=True),
        ] * 2)
        self.metadata["ipv4"] = "10.100.18.19/20"
        network.NETWORK_CONFIG.write_text(json.dumps(self.metadata))
        network.configure()
        self.assertEqual(self.run.call_args_list[-2].args[0][3], "10.100.18.19/20")

    def test_sidecar_names_work_without_dns_on_every_backend(self):
        for annotations in ({}, {"krun.use_passt": "0"}, {"krun.tap_name": "tap0"}):
            network.OCI_CONFIG.write_text(json.dumps({"annotations": annotations}))
            self.assertFalse(network.configure())
        network.OCI_CONFIG.unlink()
        self.assertFalse(network.configure())
        self.run.assert_not_called()
        self.assertIn("10.89.40.5\tproxy", network.HOSTS.read_text())
        self.assertIn("10.89.40.4\tgit", network.HOSTS.read_text())
        self.assertIn("10.1.1.1\told-alias", network.HOSTS.read_text())
        self.assertEqual(network.RESOLV.read_text(), "nameserver 127.0.0.1\noptions timeout:1 attempts:1\n")

    def test_default_route_is_not_a_valid_connected_subnet(self):
        self.metadata["ipv4"] = "10.89.40.6/0"
        network.NETWORK_CONFIG.write_text(json.dumps(self.metadata))
        with self.assertRaisesRegex(ValueError, "default route"):
            network.configure()
        self.run.assert_not_called()

    def test_command_failure_stops_configuration(self):
        for operation in range(3):
            self.run.reset_mock()
            self.run.side_effect = [None] * operation + [subprocess.CalledProcessError(2, "ip")]
            with redirect_stderr(io.StringIO()):
                self.assertEqual(network.main(), 1)
            self.assertEqual(self.run.call_count, operation + 1)

    def test_image_configures_network_before_using_sidecars(self):
        start = (ROOT / "agent/start.sh").read_text()
        self.assertLess(start.index("/usr/local/bin/agentbox-network"), start.index("until [ -f"))
        self.assertLess(start.index("/usr/local/bin/agentbox-network"), start.index("/usr/local/bin/agentbox-clone"))


class HostPasstNetworkTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.session = Path(temporary.name) / "session"
        self.session.mkdir()
        (self.session / "proxy-config").mkdir()
        (self.session / "proxy-config/proxy.yaml").write_text("providers: []\n")
        self.agentbox = runpy.run_path(str(ROOT / "bin/agentbox"))
        self.g = self.agentbox["main"].__globals__
        self.calls = []
        p = patch.dict(self.g, AGENTBOX_HOME=ROOT, _resolve_images=lambda images: images, compose=self.compose)
        p.start()
        self.addCleanup(p.stop)
        self.agentbox["generate_compose"](self.session, self.session.parent, "feature", 9876, {}, {}, [])
        self.details = [
            {"NetworkSettings": {"Networks": {"session_agent-net": {"IPAddress": address, "IPPrefixLen": 24}}}}
            for address in ("10.89.40.5", "10.89.40.4", "10.89.40.6")
        ]

    def compose(self, session, *args, **kwargs):
        self.calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    def inspect(self, args, **kwargs):
        self.assertEqual(args, ["podman", "inspect", *(self.g["_persistent_name"](self.session, service)
                                                      for service in ("proxy", "git", "network"))])
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(self.details))

    def test_topology_has_native_guard_and_drops_agent_network_capabilities(self):
        cfg = yaml.safe_load((self.session / "compose.yaml").read_text())
        self.assertEqual(cfg["services"]["agent"]["network_mode"], "service:network")
        self.assertEqual(cfg["services"]["agent"]["cap_drop"], ["NET_ADMIN", "NET_RAW"])
        for service in ("proxy", "network"):
            self.assertEqual(cfg["services"][service]["runtime"], "crun")
        self.assertIn("/proc/1/net:/host-net:ro", cfg["services"]["proxy"]["volumes"])
        self.assertTrue(cfg["networks"]["agent-net"]["internal"])

    def test_one_inspection_and_one_apply_refresh_current_metadata(self):
        with patch.object(subprocess, "run", side_effect=self.inspect) as inspect:
            self.g["_prepare_agent_network"](self.session)
            metadata = self.session / "network-config/network.json"
            self.assertEqual(json.loads(metadata.read_text()), {
                "ipv4": "10.89.40.6/24",
                "endpoints": {"proxy": "10.89.40.5", "git": "10.89.40.4", "vertex_metadata": False},
            })
            self.assertEqual(self.calls[-1][0][-1], "apply")
            self.details[2]["NetworkSettings"]["Networks"]["session_agent-net"]["IPPrefixLen"] = 20
            self.g["_prepare_agent_network"](self.session)
            self.assertEqual(json.loads(metadata.read_text())["ipv4"], "10.89.40.6/20")
        self.assertEqual(inspect.call_count, 2)

    def test_failed_policy_installation_prevents_agent_launch(self):
        def fail_apply(session, *args, **kwargs):
            return subprocess.CompletedProcess(args, int(args[-1] == "apply"), stderr="denied")

        with (patch.object(subprocess, "run", side_effect=self.inspect),
              patch.dict(self.g, compose=fail_apply),
              redirect_stderr(io.StringIO()), self.assertRaises(SystemExit)):
            self.g["_prepare_agent_network"](self.session)
        self.assertFalse((self.session / "network-config/network.json").exists())

    def test_reload_updates_a_running_guard_only(self):
        for running in ("true", "false"):
            calls = []
            with (patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout=running)),
                  patch.dict(self.g, _prepare_agent_network=lambda session: calls.append(session))):
                self.g["_refresh_agent_network"](self.session)
            self.assertEqual(calls, [self.session] if running == "true" else [])

    def test_policy_installation_precedes_agent_launch(self):
        events = []

        def compose(session, *args, **kwargs):
            events.append(args[0])
            return subprocess.CompletedProcess(args, 0)

        with (patch.dict(self.g, compose=compose,
                         _secret_preflight=lambda session: ("config", "providers", []),
                         _synchronize_proxy=lambda *args: events.append("sync"),
                         _prepare_agent_network=lambda session: events.append("network")),
              self.assertRaises(SystemExit) as result):
            self.g["_launch"](self.session, ["bash"])
        self.assertEqual(result.exception.code, 0)
        self.assertEqual(events, ["up", "sync", "network", "run", "down"])


class LinuxPasstNetworkTest(unittest.TestCase):
    def test_repairs_ipv4_in_a_real_network_namespace(self):
        if not all(shutil.which(tool) for tool in ("unshare", "ip")):
            self.skipTest("Requires unshare and iproute")
        command = ["unshare", "--user", "--map-root-user", "--net"]
        probe = subprocess.run([*command, "true"], capture_output=True, text=True)
        if probe.returncode:
            self.skipTest(probe.stderr.strip())
        script = """
import importlib.util, json, subprocess, sys, tempfile
from pathlib import Path
spec = importlib.util.spec_from_file_location('network', sys.argv[1])
network = importlib.util.module_from_spec(spec)
spec.loader.exec_module(network)
def ip(*args):
    return subprocess.check_output(['ip', *args], text=True)
ip('link', 'add', 'eth0', 'type', 'dummy')
with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    for name in ('OCI_CONFIG', 'NETWORK_CONFIG', 'HOSTS', 'RESOLV'):
        setattr(network, name, root / name)
    network.OCI_CONFIG.write_text(json.dumps({'annotations': {'krun.use_passt': '1'}}))
    network.HOSTS.write_text('127.0.0.1 localhost\\n')
    network.NETWORK_CONFIG.write_text(json.dumps({
        'ipv4': '10.123.17.3/20', 'endpoints': {'proxy': '10.123.16.2', 'git': '10.123.16.3'}}))
    for address in (None, '10.123.17.3/32', '10.123.17.3/8', '169.254.2.2/16'):
        ip('-4', 'address', 'flush', 'dev', 'eth0')
        if address:
            ip('address', 'add', address, 'dev', 'eth0')
        network.configure()
        interface = json.loads(ip('-j', '-4', 'address', 'show', 'dev', 'eth0'))[0]
        assert 'UP' in interface['flags']
        assert any(a['local'] == '10.123.17.3' and a['prefixlen'] == 20 for a in interface['addr_info'])
        selected = json.loads(ip('-j', '-4', 'route', 'get', '10.123.16.1'))[0]
        assert selected['dev'] == 'eth0' and selected['prefsrc'] == '10.123.17.3'
        assert not ip('-4', 'route', 'show', 'default')
"""
        result = subprocess.run([*command, sys.executable, "-c", script, str(ROOT / "agent/configure_network.py")],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
