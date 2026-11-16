"""Opt-in, real Podman/Compose tests. Defaults to the production krun runtime.

Build first: AGENTBOX_IMAGE_TAG=git-sidecar-test agentbox build base git proxy
Run: AGENTBOX_INTEGRATION=1 AGENTBOX_IMAGE_TAG=git-sidecar-test python3 -m unittest discover -s tests -p test_git_sidecar_integration.py -v
On systems without KVM only, explicitly set AGENTBOX_TEST_RUNTIME=crun.
Set AGENTBOX_TEST_USE_PASST=1 to exercise krun's passt backend and IPv4 workaround.
"""

import hashlib
import ipaddress
import json
import os
import select
import shlex
import shutil
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
        for name in ("agent", "proxy", "git", "compose-base.yaml", "presets"):
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
        container = self.inspect_git()
        network = next(iter(container["NetworkSettings"]["Networks"]))
        sidecar = self.config()["services"]["git"]["container_name"]
        # Even an administrator-created server hook is ignored by Git services.
        self.run_command(["podman", "exec", sidecar, "python3", "-c", """
from pathlib import Path
hook = Path('/srv/git/repos/workspace.git/hooks/pre-receive')
hook.parent.mkdir(exist_ok=True)
hook.write_text('#!/bin/sh\\nexit 1\\n')
hook.chmod(0o755)
"""])
        name = "agentbox-direct-" + hashlib.sha256(self.session_id.encode()).hexdigest()[:20]
        self.addCleanup(lambda: self.run_command(["podman", "rm", "-f", name], check=False))
        image = self.config()["services"]["agent"]["image"]
        annotations = ["--annotation", "krun.use_passt=1"] if self.use_passt else []
        network_setup = ""
        if self.use_passt:
            info = json.loads(self.run_command(["podman", "network", "inspect", network]).stdout)[0]
            subnets = [entry["subnet"] for entry in info["subnets"]
                       if ipaddress.ip_network(entry["subnet"]).version == 4]
            self.assertTrue(subnets)
            # This raw podman run intentionally bypasses the image entrypoint.
            subnet_args = [arg for subnet in subnets for arg in ("--subnet", subnet)]
            network_setup = shlex.join(["agentbox-network", *subnet_args]) + "\n"
        self.run_command(["podman", "run", "--rm", "--name", name, "--runtime", os.environ.get("AGENTBOX_TEST_RUNTIME", "krun"),
                          *annotations, "--network", network, "--entrypoint", "/bin/bash", image, "-ec", network_setup + """
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
"""])
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
assert any(route[0] == 'eth0' and route[1] != '00000000' for route in routes), routes
print('PASST_GUEST_ROUTES=' + json.dumps(routes), flush=True)
"""
        def assert_workaround_routes(result):
            # A repair must provide the direct subnet route. Existing DHCP
            # routes, including a default route, are not deliberately removed.
            if "[agent] Configured passt IPv4" in result.stdout + result.stderr:
                report = next(line for line in result.stdout.splitlines() if line.startswith("PASST_GUEST_ROUTES="))
                routes = json.loads(report.partition("=")[2])
                self.assertTrue(any(route[0] == 'eth0' and route[1] != '00000000' and route[2] == '00000000'
                                    for route in routes), routes)

        assert_workaround_routes(self.cli("start", "--session", self.session_id, "--", "python3", "-c", script))
        name = self.config()["services"]["agent"]["container_name"]
        before = self.run_command(["podman", "inspect", "--format", "{{.Id}}", name]).stdout.strip()
        # Reuse the exact retained container, but boot a new guest kernel.
        assert_workaround_routes(self.cli("start", "--session", self.session_id))
        self.assertEqual(self.run_command(["podman", "inspect", "--format", "{{.Id}}", name]).stdout.strip(), before)
        self.assert_isolation()

    def test_passt_repairs_existing_ipv4_before_resolving_sidecars(self):
        if not self.use_passt:
            self.skipTest("Set AGENTBOX_TEST_USE_PASST=1 to exercise passt guest networking")
        if os.environ.get("AGENTBOX_TEST_RUNTIME", "krun") == "crun":
            self.skipTest("This regression requires a krun VM, not the explicit crun fallback")
        self.initialize()
        self.cli("start", "--session", self.session_id, "--", "python3", "-c", """
import fcntl, json, socket, struct, subprocess, urllib.request
from pathlib import Path

request = struct.pack('16s24x', b'eth0')
resolv = Path('/etc/resolv.conf').read_bytes()
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
    original_ip = fcntl.ioctl(control, 0x8915, request)[20:24]
    original_mask = fcntl.ioctl(control, 0x891b, request)[20:24]
    for address, mask in ((original_ip, socket.inet_aton('255.255.255.255')),
                          (socket.inet_aton('169.254.2.2'), socket.inet_aton('255.255.0.0'))):
        for operation, value in ((0x8916, address), (0x891c, mask)):
            fcntl.ioctl(control, operation, struct.pack('16sH2x4s16x', b'eth0', socket.AF_INET, value))
        result = subprocess.run(['agentbox-network'], capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
        assert '[agent] Configured passt IPv4' in result.stderr, result.stderr
        assert fcntl.ioctl(control, 0x8915, request)[20:24] == original_ip
        assert fcntl.ioctl(control, 0x891b, request)[20:24] == original_mask
        for service in ('proxy', 'git'):
            assert socket.getaddrinfo(service, 8080, socket.AF_INET, socket.SOCK_STREAM)
        with socket.create_connection(('proxy', 8080), timeout=5):
            pass
        client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with client.open('http://git:8083/manifest.json', timeout=5) as response:
            assert 'workspace' in json.load(response)
assert Path('/etc/resolv.conf').read_bytes() == resolv
""")
        self.assert_isolation()

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


if __name__ == "__main__":
    unittest.main()
