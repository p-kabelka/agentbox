"""Opt-in, real Podman/Compose tests. Defaults to the production krun runtime.

Build first: AGENTBOX_IMAGE_TAG=git-sidecar-test agentbox build base network git proxy
Run: AGENTBOX_INTEGRATION=1 AGENTBOX_IMAGE_TAG=git-sidecar-test python3 -m unittest discover -s tests -p test_git_sidecar_integration.py -v
On systems without KVM only, explicitly set AGENTBOX_TEST_RUNTIME=crun.
Set AGENTBOX_TEST_USE_PASST=1 to exercise krun's passt backend and IPv4 workaround.
"""

import hashlib
import ipaddress
import json
import os
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("AGENTBOX_INTEGRATION") == "1", "Set AGENTBOX_INTEGRATION=1 for real container tests")
class GitSidecarIntegrationTest(unittest.TestCase):
    def setUp(self):
        if not shutil.which("podman"):
            self.fail("Install Podman and podman-compose before running integration tests")
        temporary = tempfile.TemporaryDirectory(prefix="agentbox-integration-", dir="/tmp/opencode" if Path("/tmp/opencode").is_dir() else None)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.home = self.root / "installation"
        self.home.mkdir()
        for name in ("agent", "proxy", "git", "network", "compose-base.yaml", "presets"):
            (self.home / name).symlink_to(ROOT / name)
        preset = self.home / "custom" / "presets" / "integration"
        preset.mkdir(parents=True)
        (preset / "proxy.yaml").write_text("providers: []\nextra_request_policy: []\n")
        self.use_passt = os.environ.get("AGENTBOX_TEST_USE_PASST") == "1"
        agent_cfg = {"dotfiles": {".gitconfig": "[user]\nname=Agent\nemail=agent@example.com\n"}}
        if self.use_passt:
            agent_cfg["annotations"] = {"krun.use_passt": "1"}
        (preset / "agent.yaml").write_text(yaml.safe_dump(agent_cfg))
        helpers = self.root / "bin"
        helpers.mkdir()
        tag = os.environ.get("AGENTBOX_IMAGE_TAG") or "latest"
        (helpers / f"git-remote-agentbox-{tag}").symlink_to(ROOT / "bin" / "git-remote-agentbox")
        self.env = {**os.environ, "AGENTBOX_HOME": str(self.home), "AGENTBOX_STATE": str(self.root / "state"),
                    "PATH": f"{helpers}:{ROOT / 'bin'}:{Path(sys.executable).parent}:{os.environ['PATH']}"}
        self.session_id = "feature-" + hashlib.sha256(str(self.project.resolve()).encode()).hexdigest()
        self.session = self.root / "state" / "sessions" / self.session_id
        self.remote = f"agentbox-{self.session_id}"
        self.git(self.project, "init", "-q", "--initial-branch=main")
        (self.project / "source").write_text("host\n")
        self.git(self.project, "add", ".")
        self.git(self.project, "commit", "-qm", "initial")
        self.addCleanup(self.cleanup_session)

    def run_command(self, args, *, check=True, **kwargs):
        try:
            result = subprocess.run(args, env=self.env, cwd=self.project, capture_output=True, text=True,
                                    timeout=kwargs.pop("timeout", 120), **kwargs)
        except subprocess.TimeoutExpired as exc:
            def text(value):
                return value.decode(errors="replace") if isinstance(value, bytes) else value or ""
            self.fail(f"{args!r} timed out\n{text(exc.stdout)}\n{text(exc.stderr)}")
        if check:
            self.assertEqual(result.returncode, 0, f"{args!r}\n{result.stdout}\n{result.stderr}")
        return result

    def cli(self, *args, **kwargs):
        return self.run_command([sys.executable, str(ROOT / "bin" / "agentbox"), *args], **kwargs)

    def git(self, path, *args, **kwargs):
        return self.run_command(["git", "-C", str(path), "-c", "user.name=Test", "-c", "user.email=test@example.com", *args], **kwargs)

    def cleanup_session(self):
        if (self.session / "compose.yaml").is_file():
            self.cli("remove", "--session", self.session_id, check=False)

    def config(self):
        return yaml.safe_load((self.session / "compose.yaml").read_text())

    def repositories(self):
        path = self.session / "git-repositories.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def initialize(self, *args):
        self.cli("init", str(self.project), "--name", "feature", "--preset", "integration", *args)
        cfg = self.config()
        self.assertEqual(cfg["services"]["agent"]["runtime"], "krun")
        # The runtime override is test-local and explicit; production stays krun.
        runtime = os.environ.get("AGENTBOX_TEST_RUNTIME", "krun")
        if runtime != "krun":
            cfg["services"]["agent"]["runtime"] = runtime
            (self.session / "compose.yaml").write_text(yaml.safe_dump(cfg))
        return cfg

    def inspect_git(self):
        name = self.config()["services"]["git"]["container_name"]
        return json.loads(self.run_command(["podman", "inspect", name]).stdout)[0]

    def assert_isolation(self):
        cfg = self.config()
        container = self.inspect_git()
        self.assertEqual(container["Config"]["User"], "git")
        self.assertTrue(container["HostConfig"]["ReadonlyRootfs"])
        self.assertTrue(all(mount["Type"] == "volume" for mount in container["Mounts"]))
        self.assertFalse(container["HostConfig"].get("PortBindings"))
        networks = container["NetworkSettings"]["Networks"]
        self.assertEqual(len(networks), 1)
        network = json.loads(self.run_command(["podman", "network", "inspect", next(iter(networks))]).stdout)[0]
        self.assertTrue(network["internal"])
        for service in cfg["services"].values():
            for volume in service.get("volumes", []):
                parts = volume.split(":")
                if parts[0] not in cfg["volumes"]:
                    self.assertIn("ro", parts[2].split(","))

    def start_waiting_agent(self):
        script = """
import http.client, os, socket, sys
from pathlib import Path
work = Path('/root/network-lifetime-work')
token = os.urandom(16).hex()
work.write_text(token)
client = http.client.HTTPConnection('proxy', 8080, timeout=5)
def check():
    assert work.read_text() == token, 'Working state lost'
    client.request('GET', 'http://not-allowed.invalid/')
    response = client.getresponse()
    assert response.status == 403, response.status
    response.read()
    assert client.sock is not None, 'Proxy closed the keepalive connection'
    with socket.create_connection(('git', 9418), timeout=5):
        pass
    for port in (8081, 8082):
        try:
            with socket.create_connection(('proxy', port), timeout=1):
                raise AssertionError(('Management port reachable', port))
        except OSError:
            pass
check()
original_socket = client.sock
print('NETWORK_LIFETIME_READY', flush=True)
for command in sys.stdin:
    if command.strip() == 'exit':
        break
    check()
    assert client.sock is original_socket, 'TCP connection replaced'
    print('NETWORK_LIFETIME_OK', flush=True)
"""
        process = subprocess.Popen([sys.executable, str(ROOT / "bin/agentbox"), "start",
                                    "--session", self.session_id, "--", "python3", "-c", script],
                                   env=self.env, cwd=self.project, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        def cleanup():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            process.stdin.close()
            process.stdout.close()

        self.addCleanup(cleanup)
        self.read_agent_output(process, b"NETWORK_LIFETIME_READY", 90)
        return process

    def read_agent_output(self, process, marker, timeout=15):
        output = bytearray()
        deadline = time.monotonic() + timeout
        while marker not in output:
            ready, _, _ = select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                break
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                break
            output.extend(chunk)
        self.assertIn(marker, output, output.decode(errors="replace"))

    def check_waiting_agent(self, process):
        self.assertIsNone(process.poll(), "Agent attachment exited")
        process.stdin.write(b"check\n")
        process.stdin.flush()
        self.read_agent_output(process, b"NETWORK_LIFETIME_OK")

    def finish_waiting_agent(self, process):
        process.stdin.write(b"exit\n")
        process.stdin.flush()
        output, _ = process.communicate(timeout=30)
        self.assertEqual(process.returncode, 0, output.decode(errors="replace"))

    def test_network_policy_refresh_preserves_persistent_agent_and_connections(self):
        self.initialize("--persist")
        process = self.start_waiting_agent()
        cfg = self.config()
        agent = cfg["services"]["agent"]["container_name"]
        identity = ["podman", "inspect", "--format", "{{.Id}} {{.State.Pid}} {{.State.StartedAt}}", agent]
        original = self.run_command(identity).stdout
        for _ in range(2):
            self.cli("proxy", "reload", "--session", self.session_id)
            self.check_waiting_agent(process)
            containers = json.loads(self.cli("containers", "--session", self.session_id, "--json").stdout)
            self.assertEqual({c["name"] for c in containers},
                             {cfg["services"][s]["container_name"] for s in ("proxy", "git", "network", "agent")})
        self.assertEqual(self.run_command(identity).stdout, original)
        self.finish_waiting_agent(process)

    def test_network_installer_failure_preserves_concurrent_ephemeral_agents_and_policy(self):
        self.initialize()
        agents = [self.start_waiting_agent(), self.start_waiting_agent()]
        original = {c["name"]: (c["id"], c["pid"]) for c in
                    json.loads(self.cli("containers", "--session", self.session_id, "--json").stdout)}
        self.assertEqual(len(original), 5)  # Two agents, two sidecars, and the unprivileged holder.
        cfg = self.config()
        cfg["services"]["guard"]["entrypoint"] = ["/usr/bin/false"]
        (self.session / "compose.yaml").write_text(yaml.safe_dump(cfg))
        failed = self.cli("proxy", "reload", "--session", self.session_id, check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("cannot enforce agent network policy", failed.stderr)
        for process in agents:
            self.check_waiting_agent(process)
        del cfg["services"]["guard"]["entrypoint"]
        (self.session / "compose.yaml").write_text(yaml.safe_dump(cfg))
        self.cli("proxy", "reload", "--session", self.session_id)
        for process in agents:
            self.check_waiting_agent(process)
        current = {c["name"]: (c["id"], c["pid"]) for c in
                   json.loads(self.cli("containers", "--session", self.session_id, "--json").stdout)}
        self.assertEqual(current, original)
        for process in agents:
            self.finish_waiting_agent(process)

    def test_network_holder_restart_filters_new_agents_in_the_current_namespace(self):
        self.initialize()
        old = self.start_waiting_agent()  # Keep the original namespace and session alive.
        holder = self.config()["services"]["network"]["container_name"]
        self.run_command(["podman", "kill", holder])
        new = self.start_waiting_agent()  # Checks sidecar access and management-port denial.
        self.check_waiting_agent(new)
        self.assertIsNone(old.poll(), "Holder failure terminated the existing agent")
        self.finish_waiting_agent(new)
        self.finish_waiting_agent(old)

    def test_workspace_round_trip_selected_branch_and_sidecar_survives_teardown(self):
        self.git(self.project, "checkout", "-qb", "chosen")
        self.git(self.project, "commit", "--allow-empty", "-qm", "chosen")
        self.git(self.project, "checkout", "-q", "main")
        self.initialize("--branch", "chosen")
        self.assert_isolation()
        self.cli("start", "--session", self.session_id, "--", "bash", "-ec", """
test "$(git branch --show-current)" = chosen
git switch -c from-agent
echo agent > source
git commit -am 'agent work'
git push
""")
        self.assertNotEqual(self.git(self.project, "show", f"{self.remote}/from-agent:source", check=False).returncode, 0)
        self.assertEqual((self.project / "source").read_text(), "host\n")
        # Ordinary host Git restarts only the Git sidecar after compose down.
        self.git(self.project, "fetch", self.remote)
        self.assertEqual(self.git(self.project, "show", f"{self.remote}/from-agent:source").stdout, "agent\n")
        self.assert_isolation()
        self.cli("remove", "--session", self.session_id)
        self.assertNotIn(self.remote, self.git(self.project, "remote").stdout.splitlines())

    def test_host_transport_volume_lifecycle_and_remote_cleanup(self):
        self.initialize()
        self.assert_isolation()
        volume = next(mount["Name"] for mount in self.inspect_git()["Mounts"] if mount["Destination"] == "/srv/git")
        self.git(self.project, "checkout", "-qb", "host-update")
        (self.project / "source").write_text("published\n")
        self.git(self.project, "commit", "-qam", "host update")
        self.git(self.project, "push", self.remote, "HEAD")
        reference = self.root / "reference"
        self.git(self.project, "clone", "-q", str(self.project), str(reference))
        compose = (self.session / "compose.yaml").read_bytes()
        self.cli("mount", str(reference), "--as", "org/library", "--session", self.session_id)
        self.assertEqual((self.session / "compose.yaml").read_bytes(), compose)
        worktree = self.root / "worktree"
        self.git(self.project, "worktree", "add", "-qb", "worktree-update", str(worktree))
        self.cli("mount", str(worktree), "--session", self.session_id)
        self.git(worktree, "push", self.remote, "HEAD:manual-worktree-branch")
        self.cli("remote-cleanup")
        self.assertIn(self.remote, self.git(worktree, "remote").stdout.splitlines())
        self.assertEqual(set(self.repositories()), {"workspace", "org/library"})
        sidecar = self.config()["services"]["git"]["container_name"]
        manifest = self.run_command(["podman", "exec", sidecar, "cat", "/srv/git/manifest.json"])
        self.assertEqual(json.loads(manifest.stdout), ["org/library", "workspace"])
        registry = (self.session / "git-repositories.json").read_bytes()
        self.cli("stop", "--session", self.session_id)
        self.assertEqual((self.session / "git-repositories.json").read_bytes(), registry)
        self.assertEqual(self.run_command(["podman", "volume", "exists", volume], check=False).returncode, 0)
        # Fetch automatically recreates just the sidecar, preserving its volume.
        self.git(self.project, "fetch", self.remote)
        self.git(reference, "fetch", self.remote)
        self.assert_isolation()
        self.assertEqual(self.git(self.project, "show", f"{self.remote}/host-update:source").stdout, "published\n")
        self.assertEqual(self.git(self.project, "show", f"{self.remote}/manual-worktree-branch:source").stdout, "published\n")
        containers = json.loads(self.cli("containers", "--session", self.session_id, "--json").stdout)
        self.assertEqual([c["name"] for c in containers], [self.config()["services"]["git"]["container_name"]])
        self.assertEqual(json.loads(self.cli("list", "--json").stdout)[0]["status"], "stopped")
        self.cli("remove", "--session", self.session_id)
        self.assertNotIn(self.remote, self.git(self.project, "remote").stdout.splitlines())
        self.assertNotIn(self.remote, self.git(worktree, "remote").stdout.splitlines())
        self.assertNotIn(self.remote, self.git(reference, "remote").stdout.splitlines())
        self.assertEqual(self.run_command(["podman", "volume", "exists", volume], check=False).returncode, 1)

    def test_host_transport_persistent_stop_retains_git_container_and_volume(self):
        self.initialize("--persist")
        before = self.inspect_git()
        volume = next(mount["Name"] for mount in before["Mounts"] if mount["Destination"] == "/srv/git")
        self.cli("stop", "--session", self.session_id)
        self.assertFalse(self.inspect_git()["State"]["Running"])
        self.git(self.project, "fetch", self.remote)
        after = self.inspect_git()
        self.assertEqual(after["Id"], before["Id"])
        self.assertEqual(next(mount["Name"] for mount in after["Mounts"] if mount["Destination"] == "/srv/git"), volume)
        self.assert_isolation()
        self.cli("remove", "--session", self.session_id)
        self.assertEqual(self.run_command(["podman", "volume", "exists", volume], check=False).returncode, 1)

    def test_tagged_host_transport_ignores_agentbox_wrapper_environment(self):
        tag = self.env["AGENTBOX_IMAGE_TAG"] = self.env.get("AGENTBOX_IMAGE_TAG") or "latest"
        self.initialize()
        compose = (self.session / "compose.yaml").read_bytes()
        sidecar = self.config()["services"]["git"]["container_name"]
        url = self.git(self.project, "remote", "get-url", self.remote).stdout
        self.assertTrue(url.startswith(f"agentbox-{tag}::"))
        original = self.env
        variables = ("AGENTBOX_HOME", "AGENTBOX_STATE", "AGENTBOX_IMAGE_TAG",
                     "AGENTBOX_PRESET", "XDG_STATE_HOME")
        foreign = {
            "AGENTBOX_HOME": str(self.root / "other-installation"),
            "AGENTBOX_STATE": str(self.root / "other-state"),
            "AGENTBOX_IMAGE_TAG": "not-built",
            "AGENTBOX_PRESET": "not-a-preset",
            "XDG_STATE_HOME": str(self.root / "other-xdg-state"),
        }
        try:
            for index, settings in enumerate(({}, foreign)):
                with self.subTest(environment=settings):
                    self.env = {key: value for key, value in original.items() if key not in variables}
                    self.env.update(settings)
                    self.git(self.project, "commit", "--allow-empty", "-qm", f"host update {index}")
                    commit = self.git(self.project, "rev-parse", "HEAD").stdout.strip()
                    self.git(self.project, "push", self.remote, f"HEAD:published-{index}")
                    stored = self.run_command(["podman", "exec", sidecar, "git", "-C",
                                               "/srv/git/repos/workspace.git", "rev-parse", f"published-{index}"])
                    self.assertEqual(stored.stdout.strip(), commit)
                    self.run_command(["podman", "exec", sidecar, "git", "-C", "/srv/git/repos/workspace.git",
                                      "update-ref", f"refs/heads/reply-{index}", commit])
                    self.git(self.project, "fetch", self.remote)
                    self.assertEqual(self.git(self.project, "rev-parse", f"{self.remote}/reply-{index}").stdout.strip(), commit)
                    self.assertEqual(self.git(self.project, "remote", "get-url", self.remote).stdout, url)
                    self.assertNotEqual(self.git(self.project, "config", "--get",
                                                f"remote.{self.remote}.vcs", check=False).returncode, 0)
                    self.assertEqual((self.session / "compose.yaml").read_bytes(), compose)
        finally:
            self.env = original
        self.assert_isolation()

    def test_git_daemon_direct_transport_and_hook_isolation(self):
        self.initialize()
        sidecar = self.config()["services"]["git"]["container_name"]
        # Even an administrator-created server hook is ignored by Git services.
        self.run_command(["podman", "exec", sidecar, "python3", "-c", """
from pathlib import Path
hook = Path('/srv/git/repos/workspace.git/hooks/pre-receive')
hook.parent.mkdir(exist_ok=True)
hook.write_text('#!/bin/sh\\nexit 1\\n')
hook.chmod(0o755)
"""])
        self.cli("start", "--session", self.session_id, "--", "bash", "-ec", """
git clone git://git/workspace.git /workspace/project
cd /workspace/project
git config user.name Agent
git config user.email agent@example.com
git switch -c direct-work
echo direct > source
git commit -am 'direct daemon push'
git push -u origin HEAD
git checkout --orphan rewritten
git commit --allow-empty -m rewritten
if git push origin HEAD:direct-work; then exit 3; fi
git push --force origin HEAD:direct-work
git push origin HEAD:temporary
git push origin --delete temporary
""")
        self.git(self.project, "fetch", self.remote)
        self.assertEqual(self.git(self.project, "show", f"{self.remote}/direct-work:source").stdout, "direct\n")
        self.assertEqual((self.project / "source").read_text(), "host\n")

    def test_passt_dns_and_workaround_routes_survive_vm_restart(self):
        if not self.use_passt:
            self.skipTest("Set AGENTBOX_TEST_USE_PASST=1 to exercise passt guest networking")
        if os.environ.get("AGENTBOX_TEST_RUNTIME", "krun") == "crun":
            self.skipTest("This regression requires a krun VM, not the explicit crun fallback")
        self.initialize("--persist")
        script = """
import json, socket
from pathlib import Path
assert int(json.loads(Path('/.krun_config.json').read_text())['annotations']['krun.use_passt']) == 1
for service in ('proxy', 'git'):
    assert socket.getaddrinfo(service, 8080, socket.AF_INET, socket.SOCK_STREAM)
routes = [line.split() for line in Path('/proc/net/route').read_text().splitlines()[1:]]
assert any(route[0] == 'eth0' and route[1] != '00000000' and route[2] == '00000000' for route in routes), routes
"""
        self.cli("start", "--session", self.session_id, "--", "python3", "-c", script)
        name = self.config()["services"]["agent"]["container_name"]
        before = self.run_command(["podman", "inspect", "--format", "{{.Id}}", name]).stdout.strip()
        # Reuse the exact retained container, but boot a new guest kernel.
        self.cli("start", "--session", self.session_id)
        self.assertEqual(self.run_command(["podman", "inspect", "--format", "{{.Id}}", name]).stdout.strip(), before)
        self.assert_isolation()

    def test_passt_repairs_existing_ipv4_before_resolving_sidecars(self):
        if not self.use_passt:
            self.skipTest("Set AGENTBOX_TEST_USE_PASST=1 to exercise passt guest networking")
        if os.environ.get("AGENTBOX_TEST_RUNTIME", "krun") == "crun":
            self.skipTest("This regression requires a krun VM, not the explicit crun fallback")
        self.initialize()
        self.assert_isolation()  # Ephemeral start tears down the sidecar containers on exit.
        self.cli("start", "--session", self.session_id, "--", "python3", "-c", """
import json, socket, subprocess, urllib.request
from pathlib import Path

def ip(*args):
    return subprocess.check_output(['ip', *args], text=True)

resolv = Path('/etc/resolv.conf').read_bytes()
original = json.loads(ip('-j', '-4', 'address', 'show', 'dev', 'eth0'))[0]['addr_info']
for address in (original[0]['local'] + '/32', '169.254.2.2/16'):
    ip('-4', 'address', 'flush', 'dev', 'eth0')
    ip('address', 'add', address, 'dev', 'eth0')
    subprocess.run(['agentbox-network'], check=True)
    configured = json.loads(ip('-j', '-4', 'address', 'show', 'dev', 'eth0'))[0]['addr_info']
    assert all(any(a['local'] == b['local'] and a['prefixlen'] == b['prefixlen']
                   for a in configured) for b in original), configured
    for service in ('proxy', 'git'):
        assert socket.getaddrinfo(service, 8080, socket.AF_INET, socket.SOCK_STREAM)
    with socket.create_connection(('proxy', 8080), timeout=5):
        pass
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with client.open('http://git:8083/manifest.json', timeout=5) as response:
        assert 'workspace' in json.load(response)
assert Path('/etc/resolv.conf').read_bytes() == resolv
""")

    def test_passt_blocking_full_duplex_and_large_git_transfer(self):
        if not self.use_passt or os.environ.get("AGENTBOX_TEST_RUNTIME", "krun") == "crun":
            self.skipTest("Requires a real krun/passt VM")
        self.initialize()
        self.cli("start", "--session", self.session_id, "--", "python3", "-c", """
import hashlib, json, os, socket, subprocess, threading
from pathlib import Path

config = json.loads(Path('/.krun_config.json').read_text())
assert int(config['annotations']['krun.use_passt']) == 1
assert Path('/sys/class/net/eth0').exists()
sock = socket.create_connection(('git', 9418), timeout=5)
sock.settimeout(None)  # Git uses blocking sockets for its concurrent read/write.
receiving, received, sent = threading.Event(), threading.Event(), threading.Event()
reply = bytearray()

def reader():
    receiving.set()
    while len(reply) < 4:
        chunk = sock.recv(4 - len(reply))
        if not chunk:
            return
        reply.extend(chunk)
    received.set()

def writer():
    request = b'git-upload-pack /workspace.git\\0host=git\\0'
    sock.sendall(f'{len(request) + 4:04x}'.encode() + request)
    sent.set()

threading.Thread(target=reader, daemon=True).start()
assert receiving.wait(1)
assert not received.wait(0.25)  # Enter recv() before the writer attempts send().
threading.Thread(target=writer, daemon=True).start()
if not received.wait(5) or not sent.wait(5):
    print('Blocking read starved concurrent write on the Git socket', flush=True)
    os._exit(1)  # Do not wait for threads stuck inside the guest kernel.
assert int(reply, 16) > 4, reply
sock.close()

def git(*args):
    subprocess.run(['git', *args], check=True, timeout=30)

git('-C', '/workspace', 'switch', '-c', 'bulk-roundtrip')
payload = os.urandom(4 * 1024 * 1024)
Path('/workspace/bulk').write_bytes(payload)
git('-C', '/workspace', 'add', 'bulk')
git('-C', '/workspace', 'commit', '-m', 'large native Git transfer')
git('-C', '/workspace', 'push', '-u', 'origin', 'HEAD')
git('clone', '--branch', 'bulk-roundtrip', 'git://git/workspace.git', '/tmp/roundtrip')
assert hashlib.sha256(Path('/tmp/roundtrip/bulk').read_bytes()).digest() == hashlib.sha256(payload).digest()
""")

    def test_manual_clone_into_retained_agent_and_restart_preserves_work(self):
        self.initialize("--persist")
        reference = self.root / "reference"
        self.git(self.project, "clone", "-q", str(self.project), str(reference))
        script = """
if [ -e /root/resumed ]; then
    test "$(cat /workspace/uncommitted)" = keep
    test "$(cat /root/home-edit)" = keep
    test -d /context/git/org/library/.git
    exit 0
fi
touch /root/resumed
echo keep > /workspace/uncommitted
echo keep > /root/home-edit
echo READY_FOR_MOUNT
read -r _
test ! -e /context/git/org/library
agentbox-clone org/library
git -C /context/git/org/library switch -c context-work
echo agent-context > /context/git/org/library/source
git -C /context/git/org/library commit -am 'context work'
git -C /context/git/org/library push
"""
        process = subprocess.Popen([sys.executable, str(ROOT / "bin" / "agentbox"), "start", "--session", self.session_id,
                                    "--", "bash", "-ec", script], env=self.env, cwd=self.project,
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        def cleanup_process():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            process.stdin.close()
            process.stdout.close()
        self.addCleanup(cleanup_process)
        output = bytearray()
        deadline = time.monotonic() + 90
        while b"READY_FOR_MOUNT" not in output and time.monotonic() < deadline:
            ready, _, _ = select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                break
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                break
            output.extend(chunk)
        self.assertIn(b"READY_FOR_MOUNT", output, output.decode(errors="replace"))
        agent_name = self.config()["services"]["agent"]["container_name"]
        before = self.run_command(["podman", "inspect", "--format", "{{.Id}}", agent_name]).stdout.strip()
        agent = json.loads(self.run_command(["podman", "inspect", agent_name]).stdout)[0]
        self.assertFalse(any(mount["Type"] == "bind" and mount["RW"] for mount in agent["Mounts"]))
        self.cli("mount", str(reference), "--as", "org/library", "--session", self.session_id)
        rest, _ = process.communicate(input=b"clone now\n", timeout=90)
        self.assertEqual(process.returncode, 0, (output + rest).decode(errors="replace"))
        self.git(reference, "fetch", self.remote)
        self.assertEqual(self.git(reference, "show", f"{self.remote}/context-work:source").stdout, "agent-context\n")
        self.assertEqual((reference / "source").read_text(), "host\n")
        self.cli("start", "--session", self.session_id)
        after = self.run_command(["podman", "inspect", "--format", "{{.Id}}", agent_name]).stdout.strip()
        self.assertEqual(before, after)
        self.cli("remove", "--session", self.session_id)
        self.assertNotIn(self.remote, self.git(reference, "remote").stdout.splitlines())

    def test_no_git_explicitly_bind_mounts_workspace(self):
        self.initialize("--no-git")
        self.assertEqual(self.repositories(), {})
        self.cli("start", "--session", self.session_id, "--", "bash", "-ec",
                 "test -f /workspace/source && echo explicit-mount > /workspace/new-file")
        self.assertEqual((self.project / "new-file").read_text(), "explicit-mount\n")

    def test_agent_cannot_reach_management_peers_or_host_even_with_guest_routes(self):
        self.initialize("--persist")
        git = self.inspect_git()
        network = next(iter(git["NetworkSettings"]["Networks"]))
        info = json.loads(self.run_command(["podman", "network", "inspect", network]).stdout)[0]
        gateway = info["subnets"][0]["gateway"]
        # A real host service bound on every IPv4 interface, not an unused port.
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("0.0.0.0", 0))
        listener.listen()
        host_port = listener.getsockname()[1]
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(udp.close)
        udp.bind(("0.0.0.0", 0))
        udp.settimeout(0.3)
        host_addresses = {gateway}
        for result in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            if not ipaddress.ip_address(result[4][0]).is_loopback:
                host_addresses.add(result[4][0])
        peer = "agentbox-unrelated-" + hashlib.sha256(self.session_id.encode()).hexdigest()[:16]
        self.addCleanup(lambda: self.run_command(["podman", "rm", "-f", peer], check=False))
        self.run_command(["podman", "run", "-d", "--name", peer, "--network", network,
                          "--entrypoint", "python3", self.config()["services"]["git"]["image"],
                          "-m", "http.server", "8080"])
        peer_info = json.loads(self.run_command(["podman", "inspect", peer]).stdout)[0]
        peer_ip = peer_info["NetworkSettings"]["Networks"][network]["IPAddress"]
        script = """
import ipaddress, json, socket, subprocess
from pathlib import Path

def connects(host, port):
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False

for host, port in (('proxy', 8080), ('git', 9418), ('git', 8083)):
    assert connects(host, port), (host, port, 'required service unavailable')
proxy = socket.gethostbyname('proxy')
blocked = [(proxy, 8081), (proxy, 8082), (proxy, 9090),
           (GATEWAY, HOST_PORT), (PEER, 8080), ('::ffff:' + GATEWAY, HOST_PORT)]
blocked.extend((address, HOST_PORT) for address in HOST_ADDRESSES)
for host, port in blocked:
    assert not connects(host, port), (host, port, 'network policy bypass')
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
    for address in HOST_ADDRESSES:
        try:
            udp.sendto(b'AGENTBOX_GUARD_UDP_TEST', (address, UDP_PORT))
        except OSError:
            pass

import http.client
client = http.client.HTTPConnection('proxy', 8080, timeout=5)
client.request('GET', 'http://proxy:8081/')
response = client.getresponse()
assert response.status == 403 and b'not allowed' in response.read()
client.close()

# Root in the guest can add routes; that must not change the outer policy.
if Path('/.krun_config.json').exists() and Path('/sys/class/net/eth0').exists():
    subprocess.run(['ip', 'route', 'replace', 'default', 'via', GATEWAY, 'dev', 'eth0'],
                   check=True)
    for host, port in blocked + [('203.0.113.1', 443)]:
        assert not connects(host, port), (host, port, 'guest route bypass')
if not Path('/.krun_config.json').exists():
    # The explicit ordinary-container test fallback cannot alter the shared namespace either.
    result = subprocess.run(['ip', 'route', 'replace', 'default', 'via', GATEWAY, 'dev', 'eth0'],
                            capture_output=True)
    assert result.returncode != 0, 'Agent acquired outer NET_ADMIN'
    try:
        socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
        raise AssertionError('Agent acquired outer NET_RAW')
    except PermissionError:
        pass
print('SIDECAR_ONLY_EGRESS_VERIFIED', flush=True)
"""
        script = (f"GATEWAY={gateway!r}; HOST_PORT={host_port}; PEER={peer_ip!r}; "
                  f"HOST_ADDRESSES={sorted(host_addresses)!r}; UDP_PORT={udp.getsockname()[1]}\n") + script
        self.cli("start", "--session", self.session_id, "--", "python3", "-c", script)
        with self.assertRaises(socket.timeout):
            udp.recvfrom(128)
        # A new VM boot reuses the same guarded namespace/container contract.
        self.cli("start", "--session", self.session_id)
        with self.assertRaises(socket.timeout):
            udp.recvfrom(128)

    def test_vertex_metadata_is_the_only_optional_proxy_port(self):
        preset = self.home / "custom" / "presets" / "integration" / "proxy.yaml"
        preset.write_text(yaml.safe_dump({"providers": [{
            "name": "vertex", "enabled": True, "metadata_server": True,
            "credential_type": "static", "api_key_env": "TEST_VERTEX_KEY", "request_policy": [],
        }], "environment": {"TEST_VERTEX_KEY": "test-only"}}))
        self.initialize()
        self.cli("start", "--session", self.session_id, "--", "python3", "-c", """
import json, socket, urllib.request
client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with client.open('http://proxy:9090/computeMetadata/v1/instance/service-accounts/default/token', timeout=5) as response:
    assert json.load(response)['access_token'] == 'dummy-replaced-by-proxy'
for port in (8081, 8082):
    try:
        with socket.create_connection(('proxy', port), timeout=1):
            raise AssertionError(('Management port reachable', port))
    except OSError:
        pass
""")

    def test_http_proxy_cannot_forward_to_host_or_its_own_management(self):
        preset = self.home / "custom" / "presets" / "integration" / "proxy.yaml"
        preset.write_text(yaml.safe_dump({"providers": [],
                                          "extra_request_policy": [{"host": ".*", "port": ".*"}]}))
        self.initialize()
        git = self.inspect_git()
        network = next(iter(git["NetworkSettings"]["Networks"]))
        info = json.loads(self.run_command(["podman", "network", "inspect", network]).stdout)[0]
        gateway = info["subnets"][0]["gateway"]
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("0.0.0.0", 0))
        listener.listen()
        listener.settimeout(0.3)
        targets = [f"http://{gateway}:{listener.getsockname()[1]}/",
                   "http://proxy:8081/", "http://127.0.0.1:8082/health",
                   "http://[::1]:8082/health"]
        script = f"TARGETS={targets!r}\n" + """
import http.client
for target in TARGETS:
    client = http.client.HTTPConnection('proxy', 8080, timeout=5)
    client.request('GET', target)
    response = client.getresponse()
    body = response.read()
    assert response.status == 502 and b'Agentbox blocked' in body, (target, response.status, body)
    client.close()
"""
        self.cli("start", "--session", self.session_id, "--", "python3", "-c", script)
        with self.assertRaises(socket.timeout):
            listener.accept()

    def test_http_proxy_refuses_connect_dns_without_an_l7_rule(self):
        self.initialize("--persist")
        name = "agentbox-dns-" + hashlib.sha256(self.session_id.encode()).hexdigest()[:16]
        network = name + "-net"
        self.run_command(["podman", "network", "create", "-o", "isolate=false", network])
        self.addCleanup(lambda: self.run_command(["podman", "network", "rm", network], check=False))
        self.addCleanup(lambda: self.run_command(["podman", "rm", "-f", name], check=False))
        self.run_command(["podman", "run", "-d", "--name", name, "--network", network,
                          "--entrypoint", "python3", self.config()["services"]["proxy"]["image"], "-c", """
import socketserver, threading
from pathlib import Path
class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        with Path('/tmp/dns-connections').open('a') as log:
            log.write(str(self.server.server_address[1]) + '\\n')
        size = self.rfile.read(2)
        if len(size) == 2:
            query = bytearray(self.rfile.read(int.from_bytes(size, 'big')))
            query[2] |= 0x80
            self.wfile.write(size + query)
servers = [socketserver.ThreadingTCPServer(('0.0.0.0', port), Handler) for port in (53, 5353)]
threading.Thread(target=servers[0].serve_forever, daemon=True).start()
servers[1].serve_forever()
"""])
        fixture = json.loads(self.run_command(["podman", "inspect", name]).stdout)[0]
        address = fixture["NetworkSettings"]["Networks"][network]["IPAddress"]
        # Only the address is trusted, not any L7 request. This isolates DNS
        # protocol enforcement from the independent private-address restriction.
        config = self.session / "proxy-config" / "proxy.yaml"
        config.write_text(yaml.safe_dump({"providers": [], "extra_request_policy": [],
                                         "private_upstreams": [address]}))
        script = f"TARGET={address!r}\n" + """
import socket, struct
def read_exact(client, size):
    result = b''
    while len(result) < size:
        part = client.recv(size - len(result))
        assert part, 'Connection closed before the DNS response'
        result += part
    return result
query = struct.pack('!6H', 0x1234, 0x100, 1, 0, 0, 0)
query += b'\\x05hello\\x07example\\x00' + struct.pack('!2H', 1, 1)
for port in (53, 5353):
    with socket.create_connection(('proxy', 8080), timeout=5) as client:
        client.sendall(f'CONNECT {TARGET}:{port} HTTP/1.1\\r\\nHost: {TARGET}:{port}\\r\\n\\r\\n'.encode())
        header = b''
        while not header.endswith(b'\\r\\n\\r\\n'):
            header += read_exact(client, 1)
        assert b' 200 ' in header, header
        for _ in range(2):
            client.sendall(struct.pack('!H', len(query)) + query)
            size = int.from_bytes(read_exact(client, 2), 'big')
            response = read_exact(client, size)
            assert response[:2] == query[:2], response
            assert response[3] & 15 == 5, ('DNS was not refused', port, response)
"""
        self.cli("start", "--session", self.session_id, "--", "python3", "-c", script)
        # No tunneled query may even open an upstream socket. Then prove that
        # both fixture listeners are reachable from the proxy, avoiding a false
        # pass from bridge isolation or a missing DNS server.
        self.run_command(["podman", "exec", name, "python3", "-c",
                          "from pathlib import Path; assert not Path('/tmp/dns-connections').exists()"])
        proxy = self.config()["services"]["proxy"]["container_name"]
        self.run_command(["podman", "start", proxy])
        self.run_command(["podman", "exec", proxy, "python3", "-c",
                          f"import socket\nfor port in (53, 5353):\n"
                          f"    with socket.create_connection(({address!r}, port), timeout=5): pass\n"])

    def test_allowed_http_and_https_still_work_through_the_proxy(self):
        preset = self.home / "custom" / "presets" / "integration" / "proxy.yaml"
        preset.write_text(yaml.safe_dump({"providers": [], "extra_request_policy": [
            {"host": r"upstream\.example", "port": port} for port in (8080, 8443)]}))
        self.initialize("--persist")
        name = "agentbox-upstream-" + hashlib.sha256(self.session_id.encode()).hexdigest()[:16]
        network = name + "-net"
        self.run_command(["podman", "network", "create", "-o", "isolate=false", network])
        self.addCleanup(lambda: self.run_command(["podman", "network", "rm", network], check=False))
        certs = self.root / "tls"
        certs.mkdir()
        certificate, key = certs / "upstream.crt", certs / "upstream.key"
        self.run_command(["podman", "run", "--rm", "--network", "none", "-v", f"{certs}:/tls:z",
                          "--entrypoint", "python3", self.config()["services"]["proxy"]["image"], "-c", """
from datetime import datetime, timedelta, timezone
from pathlib import Path
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
key = rsa.generate_private_key(public_exponent=65537,key_size=2048)
name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'upstream.example')])
now = datetime.now(timezone.utc)
cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(days=1))
        .not_valid_after(now+timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True,path_length=None),critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName('upstream.example')]),critical=False)
        .sign(key,hashes.SHA256()))
Path('/tls/upstream.crt').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
Path('/tls/upstream.key').write_bytes(key.private_bytes(serialization.Encoding.PEM,
    serialization.PrivateFormat.TraditionalOpenSSL,serialization.NoEncryption()))
"""])
        self.addCleanup(lambda: self.run_command(["podman", "rm", "-f", name], check=False))
        self.run_command(["podman", "run", "-d", "--name", name, "--network", network,
                          "-v", f"{certs}:/tls:ro,z", "--entrypoint", "python3",
                          self.config()["services"]["proxy"]["image"], "-c", """
import ssl, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import StreamEnded
class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def handle(self):
        if getattr(self.connection, 'selected_alpn_protocol', lambda: None)() != 'h2':
            return super().handle()
        connection = H2Connection(config=H2Configuration(client_side=False))
        connection.initiate_connection()
        self.connection.sendall(connection.data_to_send())
        while data := self.connection.recv(65536):
            for event in connection.receive_data(data):
                if isinstance(event, StreamEnded):
                    body = b'EXTERNAL_FIXTURE_OK H2 ' + str(self.client_address[1]).encode()
                    connection.send_headers(event.stream_id, [(':status', '200'), ('content-length', str(len(body)))])
                    connection.send_data(event.stream_id, body, end_stream=True)
            self.connection.sendall(connection.data_to_send())
    def do_GET(self):
        body = b'EXTERNAL_FIXTURE_OK ' + str(self.client_address[1]).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
plain = ThreadingHTTPServer(('0.0.0.0',8080),Handler)
secure = ThreadingHTTPServer(('0.0.0.0',8443),Handler)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain('/tls/upstream.crt','/tls/upstream.key')
context.set_alpn_protocols(['h2', 'http/1.1'])
secure.socket = context.wrap_socket(secure.socket,server_side=True)
threading.Thread(target=plain.serve_forever,daemon=True).start()
secure.serve_forever()
"""])
        fixture = json.loads(self.run_command(["podman", "inspect", name]).stdout)[0]
        address = fixture["NetworkSettings"]["Networks"][network]["IPAddress"]
        proxy_config = self.session / "proxy-config" / "proxy.yaml"
        policy = yaml.safe_load(proxy_config.read_text())
        policy["private_upstreams"] = [address]  # Explicitly trust only this remote fixture IP.
        proxy_config.write_text(yaml.safe_dump(policy))
        cfg = self.config()
        cfg["services"]["proxy"]["extra_hosts"] = [f"upstream.example:{address}"]
        cfg["services"]["proxy"]["volumes"].append(f"{certificate}:/custom-certs/upstream.crt:ro,z")
        (self.session / "compose.yaml").write_text(yaml.safe_dump(cfg))
        self.cli("start", "--session", self.session_id, "--", "python3", "-c", """
import http.client, ssl
for secure in (False, True):
    if secure:
        client = http.client.HTTPSConnection('proxy', 8080, context=ssl.create_default_context(), timeout=10)
        client.set_tunnel('upstream.example', 8443)
        target = '/'
    else:
        client = http.client.HTTPConnection('proxy', 8080, timeout=10)
        target = 'http://upstream.example:8080/'
    ports = []
    for _ in range(8):
        client.request('GET', target)
        response = client.getresponse()
        body = response.read()
        assert response.status == 200 and body.startswith(b'EXTERNAL_FIXTURE_OK '), (response.status, body)
        ports.append(body.split()[-1])
    assert len(set(ports)) == 1, ('upstream connection not reused', ports)
    client.close()
""")
        # Exercise the production proxy with an HTTP/2 client as well. The
        # proxy image supplies h2; the minimal agent image deliberately does not.
        proxy = cfg["services"]["proxy"]["container_name"]
        self.run_command(["podman", "start", proxy])
        self.cli("proxy", "reload", "--session", self.session_id)
        self.run_command(["podman", "exec", proxy, "python3", "-c", """
import socket, ssl
from h2.connection import H2Connection
from h2.events import DataReceived, ResponseReceived, StreamEnded
raw = socket.create_connection(('127.0.0.1', 8080), timeout=10)
raw.sendall(b'CONNECT upstream.example:8443 HTTP/1.1\\r\\nHost: upstream.example:8443\\r\\n\\r\\n')
header = b''
while not header.endswith(b'\\r\\n\\r\\n'):
    header += raw.recv(1)
assert b' 200 ' in header, header
context = ssl.create_default_context(cafile='/root/.mitmproxy/mitmproxy-ca-cert.pem')
context.set_alpn_protocols(['h2'])
with context.wrap_socket(raw, server_hostname='upstream.example') as client:
    assert client.selected_alpn_protocol() == 'h2'
    connection = H2Connection()
    connection.initiate_connection()
    bodies = {stream: b'' for stream in range(1, 17, 2)}
    for stream in bodies:
        connection.send_headers(stream, [(':method', 'GET'), (':scheme', 'https'),
            (':authority', 'upstream.example:8443'), (':path', '/')], end_stream=True)
    client.sendall(connection.data_to_send())
    completed = set()
    while len(completed) < len(bodies):
        data = client.recv(65536)
        assert data, 'HTTP/2 connection closed prematurely'
        for event in connection.receive_data(data):
            if isinstance(event, ResponseReceived):
                assert (b':status', b'200') in event.headers, event.headers
            elif isinstance(event, DataReceived):
                bodies[event.stream_id] += event.data
                connection.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
            elif isinstance(event, StreamEnded):
                completed.add(event.stream_id)
        client.sendall(connection.data_to_send())
    assert all(body.startswith(b'EXTERNAL_FIXTURE_OK H2 ') for body in bodies.values()), bodies
    assert len({body.split()[-1] for body in bodies.values()}) == 1, bodies
"""])


if __name__ == "__main__":
    unittest.main()
