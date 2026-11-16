"""Gateway-less passt networking, without requiring Podman or network privileges."""

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
        self.resolv = self.root / "resolv.conf"
        self.resolv.write_text("nameserver 10.89.40.1\n")
        self.oci.write_text(json.dumps({"annotations": {"krun.use_passt": "1"}}))
        self.subnets.write_text(json.dumps({"ipv4_subnets": ["10.89.40.0/24"]}))
        self.hosts.write_text("127.0.0.1 localhost agent-id\n::1 agent-id\n10.89.40.3 agent-id other-alias\n")
        p = patch.object(network.subprocess, "run")
        self.run = p.start()
        self.addCleanup(p.stop)
        for p in (patch.object(network, "OCI_CONFIG", self.oci),
                  patch.object(network, "SUBNET_CONFIG", self.subnets),
                  patch.object(network, "HOSTS", self.hosts),
                  patch.object(network, "RESOLV", self.resolv),
                  patch.dict(network.os.environ, AGENTBOX_NETWORK_GUARD="0"),
                  patch.object(network.socket, "gethostname", return_value="agent-id")):
            p.start()
            self.addCleanup(p.stop)

    def test_applies_only_allocated_address_link_and_subnet_route_on_each_run(self):
        with redirect_stderr(io.StringIO()) as output:
            self.assertTrue(network.configure())
            self.assertTrue(network.configure())
        self.assertIn("10.89.40.3/24", output.getvalue())
        self.assertEqual(self.run.call_args_list, [
            call(["ip", "link", "set", "dev", "eth0", "up"], check=True),
            call(["ip", "address", "replace", "10.89.40.3/24", "dev", "eth0"], check=True),
            call(["ip", "route", "replace", "10.89.40.0/24", "dev", "eth0", "src", "10.89.40.3"], check=True),
        ] * 2)
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
        self.run.assert_not_called()

    def test_command_failure_stops_setup_and_is_not_reported_as_success(self):
        for operation in range(3):
            with self.subTest(operation=operation), redirect_stderr(io.StringIO()) as output:
                self.run.reset_mock()
                self.run.side_effect = [None] * operation + [subprocess.CalledProcessError(2, "ip")]
                self.assertEqual(network.main([]), 1)
            self.assertEqual(self.run.call_count, operation + 1)
            self.assertIn("Cannot configure passt IPv4", output.getvalue())
            self.assertNotIn("[agent] Configured passt IPv4", output.getvalue())

    def test_new_boot_uses_current_podman_hosts_and_prefix(self):
        with redirect_stderr(io.StringIO()):
            network.configure()
            self.hosts.write_text("10.100.18.19 agent-id\n")
            self.subnets.write_text(json.dumps({"ipv4_subnets": ["10.100.16.0/20"]}))
            network.configure()
        self.assertEqual(self.run.call_args_list[-2:], [
            call(["ip", "address", "replace", "10.100.18.19/20", "dev", "eth0"], check=True),
            call(["ip", "route", "replace", "10.100.16.0/20", "dev", "eth0", "src", "10.100.18.19"], check=True),
        ])

    def test_standalone_subnet_option_needs_no_metadata_mount(self):
        self.subnets.unlink()
        with redirect_stderr(io.StringIO()):
            self.assertEqual(network.main(["--subnet", "10.89.40.0/24"]), 0)
        self.assertEqual(self.run.call_args_list[1].args[0][3], "10.89.40.3/24")

    def test_invalid_or_ambiguous_addresses_fail_before_any_changes(self):
        cases = [([], "10.89.40.3 agent-id\n"),
                 (["0.0.0.0/0"], "10.89.40.3 agent-id\n"),
                 (["10.89.40.0/24"], "10.89.41.3 agent-id\n"),
                 (["10.89.40.0/24"], "10.89.40.3 another-agent-id\n"),
                  (["10.89.40.0/24"], "10.89.40.0 agent-id\n10.89.40.255 agent-id\n"),
                  (["10.89.40.0/24"], "10.89.40.3 agent-id\n10.89.40.4 agent-id\n"),
                  (["10.89.40.0/24", "10.89.0.0/16"], "10.89.40.3 agent-id\n"),
                  (["127.0.0.0/8"], "127.0.0.1 agent-id\n")]
        for subnets, hosts in cases:
            with self.subTest(subnets=subnets, hosts=hosts):
                self.subnets.write_text(json.dumps({"ipv4_subnets": subnets}))
                self.hosts.write_text(hosts)
                with self.assertRaises(ValueError):
                    network.configure()
        self.run.assert_not_called()

    def test_duplicate_hosts_entries_do_not_make_the_address_ambiguous(self):
        self.hosts.write_text("10.89.40.3 agent-id\n10.89.40.3 agent-id # repeated alias\n")
        with redirect_stderr(io.StringIO()):
            self.assertTrue(network.configure())

    def test_missing_metadata_is_actionable_and_fails_before_any_changes(self):
        self.subnets.unlink()
        with redirect_stderr(io.StringIO()) as output:
            self.assertEqual(network.main([]), 1)
        self.assertIn("Cannot configure passt IPv4", output.getvalue())
        self.assertIn(str(self.subnets), output.getvalue())
        self.run.assert_not_called()

    def test_image_runs_helper_before_ca_wait_git_clone_and_harness(self):
        containerfile = (ROOT / "agent" / "Containerfile.base").read_text()
        self.assertIn("iproute", containerfile)
        self.assertIn("COPY --chmod=755 configure_network.py /usr/local/bin/agentbox-network", containerfile)
        start = (ROOT / "agent" / "start.sh").read_text()
        self.assertLess(start.index("/usr/local/bin/agentbox-network"), start.index("until [ -f"))
        self.assertLess(start.index("/usr/local/bin/agentbox-network"), start.index("/usr/local/bin/agentbox-clone"))

    def test_guarded_names_need_no_external_dns_and_shared_namespace_supplies_guest_ip(self):
        self.hosts.write_text("127.0.0.1 localhost\n10.1.1.1 proxy old-alias\n")
        self.subnets.write_text(json.dumps({"ipv4_subnets": ["10.89.40.0/24"],
                                           "endpoints": {"proxy": "10.89.40.5", "git": "10.89.40.4"},
                                           "guest_ipv4": "10.89.40.6"}))
        with redirect_stderr(io.StringIO()):
            self.assertTrue(network.configure())
        self.assertIn("10.89.40.5\tproxy", self.hosts.read_text())
        self.assertIn("10.89.40.4\tgit", self.hosts.read_text())
        self.assertIn("10.1.1.1\told-alias", self.hosts.read_text())
        self.assertIn("nameserver 127.0.0.1", self.resolv.read_text())
        self.assertIn("10.89.40.6/24", self.run.call_args_list[1].args[0])

    def test_guarded_session_rejects_missing_endpoints(self):
        with patch.dict(network.os.environ, AGENTBOX_NETWORK_GUARD="1"):
            with self.assertRaisesRegex(ValueError, "guarded network endpoints"):
                network.configure()
        self.run.assert_not_called()


class HostPasstNetworkTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.session = Path(temporary.name) / "session"
        self.session.mkdir()
        self.project = self.session.parent / "project"
        self.project.mkdir()
        (self.session / "proxy-config").mkdir()
        (self.session / "proxy-config" / "proxy.yaml").write_text("providers: []\n")
        self.agentbox = runpy.run_path(str(ROOT / "bin" / "agentbox"))
        self.g = self.agentbox["main"].__globals__
        self.compose_calls = []
        p = patch.dict(self.g, AGENTBOX_HOME=ROOT, _resolve_images=lambda images: images, compose=self.compose)
        p.start()
        self.addCleanup(p.stop)
        self.git = {"NetworkSettings": {"Networks": {"session_agent-net": {"IPAddress": "10.89.40.4"}}}}
        self.proxy = {"NetworkSettings": {"Networks": {"session_agent-net": {"IPAddress": "10.89.40.5"}}}}
        self.guard = {"Id": "guard-id", "NetworkSettings": {"Networks": {
            "session_agent-net": {"IPAddress": "10.89.40.6"}}}}
        self.agent = {"HostConfig": {"NetworkMode": "container:guard-id"},
                      "BoundingCaps": ["CAP_CHOWN"], "EffectiveCaps": ["CAP_CHOWN"]}
        self.network = {"internal": True, "subnets": [{"subnet": "10.89.40.0/24", "gateway": "10.89.40.1"},
                                                    {"subnet": "fd00::/64", "gateway": "fd00::1"}]}

    def generate(self, annotations=None):
        self.agentbox["generate_compose"](self.session, self.project, "feature", 9876, {},
                                           {"annotations": annotations or {}}, [])
        cfg = yaml.safe_load((self.session / "compose.yaml").read_text())
        cfg["services"]["proxy"]["container_name"] = "proxy-container"
        (self.session / "compose.yaml").write_text(yaml.safe_dump(cfg))
        return cfg

    def compose(self, session, *args, **kwargs):
        self.compose_calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    def inspect(self, args, **kwargs):
        self.assertTrue(kwargs["check"])
        if args[:2] == ["podman", "inspect"]:
            values = {self.agentbox["_persistent_name"](self.session, "git"): self.git,
                      self.agentbox["_persistent_name"](self.session, "agent"): self.agent,
                      self.agentbox["_persistent_name"](self.session, "network"): self.guard,
                      "proxy-container": self.proxy}
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps([values[args[2]]]))
        self.assertEqual(args, ["podman", "network", "inspect", "session_agent-net"])
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps([self.network]))

    def test_agent_uses_guard_namespace_and_read_only_metadata(self):
        cfg = self.generate({"krun.use_passt": "1", "krun.ram_mib": "3072"})
        self.assertTrue((self.session / "network-config").is_dir())
        self.assertIn(self.g["NETWORK_CONFIG_VOLUME"], cfg["services"]["agent"]["volumes"])
        self.assertEqual(cfg["services"]["agent"]["network_mode"], "service:network")
        self.assertIn("NET_ADMIN", cfg["services"]["agent"]["cap_drop"])
        self.assertIn("NET_RAW", cfg["services"]["agent"]["cap_drop"])
        self.assertEqual(cfg["services"]["network"]["runtime"], "crun")
        self.assertTrue(cfg["networks"]["agent-net"]["internal"])
        self.assertNotIn("ipam", cfg["networks"]["agent-net"])
        for service in ("proxy", "git"):
            self.assertNotIn(self.g["NETWORK_CONFIG_VOLUME"], cfg["services"][service]["volumes"])

    def test_all_backends_install_the_outer_policy(self):
        for annotations in ({}, {"krun.use_passt": "0"}):
            with self.subTest(annotations=annotations), patch.object(subprocess, "run", side_effect=self.inspect):
                cfg = self.generate(annotations)
                self.assertIn(self.g["NETWORK_CONFIG_VOLUME"], cfg["services"]["agent"]["volumes"])
                self.agentbox["_prepare_agent_network"](self.session)
                self.assertEqual(self.compose_calls[-1][0][-1], "apply")
                self.assertEqual(json.loads(self.compose_calls[-1][1]["input"]),
                                 {"proxy": "10.89.40.5", "git": "10.89.40.4", "vertex_metadata": False})

    def test_prepare_refreshes_only_real_ipv4_subnets_without_static_agent_ips(self):
        self.generate({"krun.use_passt": "1"})
        directory = self.session / "network-config"
        config = directory / "ipv4-subnets.json"
        compose = (self.session / "compose.yaml").read_bytes()
        with patch.object(subprocess, "run", side_effect=self.inspect) as run:
            self.agentbox["_prepare_agent_network"](self.session)
            self.assertEqual(json.loads(config.read_text())["ipv4_subnets"], ["10.89.40.0/24"])
            self.network["subnets"] = [{"subnet": "10.100.16.0/20"}]
            self.agentbox["_prepare_agent_network"](self.session)
            self.assertEqual(json.loads(config.read_text())["ipv4_subnets"], ["10.100.16.0/20"])
        self.assertEqual(run.call_count, 8)
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

    def test_missing_guard_and_failed_policy_are_fail_closed(self):
        cfg = self.generate()
        cfg["services"]["agent"].pop("network_mode")
        (self.session / "compose.yaml").write_text(yaml.safe_dump(cfg))
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.agentbox["_prepare_agent_network"](self.session)
        self.assertEqual(self.compose_calls, [])
        self.generate()
        def fail_apply(session, *args, **kwargs):
            return subprocess.CompletedProcess(args, int(args[-1] == "apply"), stderr="denied")
        with (patch.object(subprocess, "run", side_effect=self.inspect),
              patch.dict(self.g, compose=fail_apply),
              redirect_stderr(io.StringIO()), self.assertRaises(SystemExit)):
            self.agentbox["_prepare_agent_network"](self.session)
        self.assertFalse((self.session / "network-config" / "ipv4-subnets.json").exists())

    def test_reload_refreshes_only_a_running_guard(self):
        self.generate()
        marker = self.session / "network-config" / "ipv4-subnets.json"
        marker.write_text("{}")
        for running in ("true", "false"):
            calls = []
            with (patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout=running)),
                  patch.dict(self.g, _prepare_agent_network=lambda session: calls.append(session))):
                self.agentbox["_refresh_agent_network"](self.session)
            self.assertEqual(calls, [self.session] if running == "true" else [])

    def test_retained_container_cannot_silently_keep_old_network_or_capabilities(self):
        with patch.object(subprocess, "run", side_effect=self.inspect):
            self.agentbox["_assert_retained_network"](self.session)
            self.agent["HostConfig"]["NetworkMode"] = "bridge"
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.agentbox["_assert_retained_network"](self.session)
            self.agent["HostConfig"]["NetworkMode"] = "container:guard-id"
            self.agent["BoundingCaps"].append("CAP_NET_RAW")
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.agentbox["_assert_retained_network"](self.session)

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
    def test_configuration_and_repairs_in_an_isolated_network_namespace(self):
        if not all(shutil.which(tool) for tool in ("unshare", "ip")):
            self.skipTest("Install unshare and iproute to verify real Linux networking")
        command = ["unshare", "--user", "--map-root-user", "--net"]
        probe = subprocess.run([*command, "true"], capture_output=True, text=True, timeout=10)
        if probe.returncode:
            self.skipTest("Unprivileged network namespaces unavailable: " + probe.stderr.strip())
        script = """
import importlib.util, json, socket, subprocess, sys, tempfile
from pathlib import Path

def ip(*args):
    return subprocess.check_output(['ip', *args], text=True)

ip('link', 'add', 'eth0', 'type', 'dummy')
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
    network.HOSTS.write_text(f'10.123.17.3 {socket.gethostname()}\\n')
    resolv = Path('/etc/resolv.conf').read_bytes()

    def assert_configured():
        interface = json.loads(ip('-j', '-4', 'address', 'show', 'dev', 'eth0'))[0]
        assert 'UP' in interface['flags'], interface
        assert any(a['local'] == '10.123.17.3' and a['prefixlen'] == 20 for a in interface['addr_info']), interface
        routes = json.loads(ip('-j', '-4', 'route', 'show'))
        assert any(r['dst'] == '10.123.16.0/20' and r['dev'] == 'eth0'
                   and r['prefsrc'] == '10.123.17.3' and 'gateway' not in r for r in routes), routes
        selected = json.loads(ip('-j', '-4', 'route', 'get', '10.123.16.1'))[0]
        assert selected['dev'] == 'eth0' and selected['prefsrc'] == '10.123.17.3', selected
        assert Path('/etc/resolv.conf').read_bytes() == resolv

    # Missing IPv4, incorrect prefixes, and an unrelated fallback address.
    for address in (None, '10.123.17.3/32', '10.123.17.3/8', '169.254.2.2/16'):
        ip('-4', 'address', 'flush', 'dev', 'eth0')
        if address:
            ip('address', 'add', address, 'dev', 'eth0')
        network.configure()
        assert_configured()
        assert not ip('-4', 'route', 'show', 'default')

    # Deleted or gateway routes must be replaced with a direct subnet route.
    ip('route', 'del', '10.123.16.0/20')
    network.configure()
    assert_configured()
    ip('route', 'replace', '10.123.16.0/20', 'via', '10.123.16.1', 'dev', 'eth0', 'onlink')
    network.configure()
    assert_configured()

    ip('link', 'set', 'eth0', 'down')
    network.configure()
    assert_configured()

    # Repeated runs preserve working state, unrelated addresses, and any existing default.
    ip('route', 'add', 'default', 'via', '10.123.16.1', 'dev', 'eth0')
    before = ip('-j', '-4', 'address', 'show'), ip('-j', '-4', 'route', 'show')
    network.configure()
    assert_configured()
    assert before == (ip('-j', '-4', 'address', 'show'), ip('-j', '-4', 'route', 'show'))
"""
        result = subprocess.run([*command, sys.executable, "-c", script, str(ROOT / "agent" / "configure_network.py")],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
