"""Opt-in, real Git sharing and persistent workspace integration tests.

Build first: AGENTBOX_IMAGE_TAG=git-sidecar-test agentbox build base network git proxy
Run: AGENTBOX_INTEGRATION=1 AGENTBOX_IMAGE_TAG=git-sidecar-test python3 -m unittest discover -s tests -p test_git_sidecar_integration.py -v
On systems without KVM only, explicitly set AGENTBOX_TEST_RUNTIME=crun.
Set AGENTBOX_TEST_USE_PASST=1 to use krun's passt backend.
"""

import json
import os
import select
import subprocess
import sys
import time
import unittest

from integration_support import AgentboxIntegrationTest, ROOT


class GitSidecarIntegrationTest(AgentboxIntegrationTest):
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
        manifest = self.run_command(["podman", "exec", sidecar, "python3", "-c",
                                     "import urllib.request; print(urllib.request.urlopen('http://localhost:8083/manifest.json').read().decode())"])
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
        self.run_command(["podman", "exec", sidecar, "python3", "/app/server.py",
                          "init", "unpublished", "main", "sha1"])
        # Even an administrator-created server hook is ignored by Git services.
        self.run_command(["podman", "exec", sidecar, "python3", "-c", """
from pathlib import Path
hook = Path('/srv/git/repos/workspace.git/hooks/pre-receive')
hook.parent.mkdir(exist_ok=True)
hook.write_text('#!/bin/sh\\nexit 1\\n')
hook.chmod(0o755)
"""])
        self.cli("start", "--session", self.session_id, "--", "bash", "-ec", """
if git ls-remote git://git/unpublished.git; then exit 4; fi
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

    def test_catalog_ignores_marker_named_refs_pushed_by_agent(self):
        self.initialize()
        self.cli("start", "--session", self.session_id, "--", "bash", "-ec", """
git push origin HEAD:refs/heads/fake.git/git-daemon-export-ok
python3 -c "import json, urllib.request; assert json.load(urllib.request.urlopen('http://git:8083/manifest.json')) == ['workspace']"
agentbox-clone
""")
        # A new guest must still clone successfully after the ref is retained.
        self.cli("start", "--session", self.session_id, "--", "bash", "-ec", "test -f /workspace/source")

    def test_overlapping_roots_are_rejected_without_rejecting_shared_namespaces(self):
        self.initialize()
        references = {}
        for index, name in enumerate(("forge/org/repo1", "forge/org/repo2", "forge.git/org/repo1")):
            reference = self.root / f"reference-{index}"
            self.git(self.project, "clone", "-q", str(self.project), str(reference))
            self.cli("mount", str(reference), "--as", name, "--session", self.session_id)
            references[name] = reference
        rejected = self.root / "rejected"
        self.git(self.project, "clone", "-q", str(self.project), str(rejected))
        registry = (self.session / "git-repositories.json").read_bytes()
        for name in ("forge/org/repo1.git/child", "forge"):
            with self.subTest(name=name):
                result = self.cli("mount", str(rejected), "--as", name, "--session", self.session_id,
                                  check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("overlap", result.stderr)
                self.assertEqual((self.session / "git-repositories.json").read_bytes(), registry)
                self.assertNotIn(self.remote, self.git(rejected, "remote").stdout.splitlines())
        self.cli("start", "--session", self.session_id, "--", "bash", "-ec", """
for repo in forge/org/repo1 forge/org/repo2 forge.git/org/repo1; do
    git -C "/context/git/$repo" switch -c from-agent
    git -C "/context/git/$repo" commit --allow-empty -m 'agent work'
    git -C "/context/git/$repo" push
done
""")
        self.cli("unmount", "forge/org/repo1", "--session", self.session_id)
        for name in ("forge/org/repo2", "forge.git/org/repo1"):
            reference = references[name]
            self.git(reference, "fetch", self.remote)
            self.assertEqual(self.git(reference, "log", "-1", "--format=%s",
                                      f"{self.remote}/from-agent").stdout.strip(), "agent work")

    def test_unmount_reclaims_sidecar_space_and_preserves_retained_sandbox_clone(self):
        self.initialize("--persist")
        reference = self.root / "reference"
        self.git(self.project, "clone", "-q", str(self.project), str(reference))
        (reference / "bulk").write_bytes(os.urandom(1024 * 1024))
        self.git(reference, "add", "bulk")
        self.git(reference, "commit", "-qm", "reference payload")
        self.cli("mount", str(reference), "--as", "org/library", "--session", self.session_id)
        sidecar = self.config()["services"]["git"]["container_name"]
        before = int(self.run_command(["podman", "exec", sidecar, "du", "-sb", "/srv/git"]).stdout.split()[0])
        script = """
if [ -e /root/resumed ]; then
    test "$(cat /context/git/org/library/uncommitted)" = keep
    test "$(wc -c < /context/git/org/library/bulk)" = 1048576
    if git ls-remote git://git/org/library.git; then exit 3; fi
    python3 -c "import json, urllib.request; assert json.load(urllib.request.urlopen('http://git:8083/manifest.json')) == ['workspace']"
else
    touch /root/resumed
    echo keep > /context/git/org/library/uncommitted
fi
"""
        self.cli("start", "--session", self.session_id, "--", "bash", "-ec", script)
        compose = (self.session / "compose.yaml").read_bytes()
        agent_name = self.config()["services"]["agent"]["container_name"]
        agent_id = self.run_command(["podman", "inspect", "--format", "{{.Id}}", agent_name]).stdout
        self.cli("unmount", "org/library", "--session", self.session_id)
        self.assertEqual(set(self.repositories()), {"workspace"})
        self.assertNotIn(self.remote, self.git(reference, "remote").stdout.splitlines())
        self.assertIn(self.remote, self.git(self.project, "remote").stdout.splitlines())
        self.assertEqual((self.session / "compose.yaml").read_bytes(), compose)
        self.assertNotEqual(self.run_command(["podman", "exec", sidecar, "test", "-e",
                                              "/srv/git/repos/org/library.git"], check=False).returncode, 0)
        after = int(self.run_command(["podman", "exec", sidecar, "du", "-sb", "/srv/git"]).stdout.split()[0])
        self.assertGreater(before - after, 1024 * 1024)
        self.git(self.project, "fetch", self.remote)
        self.cli("start", "--session", self.session_id)
        self.assertEqual(self.run_command(["podman", "inspect", "--format", "{{.Id}}", agent_name]).stdout, agent_id)
        self.assertEqual((reference / "bulk").stat().st_size, 1024 * 1024)

    def test_mount_reuses_namespace_directory_left_after_unmount(self):
        self.initialize()
        reference = self.root / "reference"
        self.git(self.project, "clone", "-q", str(self.project), str(reference))
        self.cli("mount", str(reference), "--as", "forge.git/org/repo", "--session", self.session_id)
        self.cli("unmount", "forge.git/org/repo", "--session", self.session_id)
        sidecar = self.config()["services"]["git"]["container_name"]
        namespace = "/srv/git/repos/forge.git/org"
        self.run_command(["podman", "exec", sidecar, "test", "-d", namespace])
        self.cli("mount", str(reference), "--as", "forge", "--session", self.session_id)
        self.run_command(["podman", "exec", sidecar, "test", "-d", namespace])
        self.cli("start", "--session", self.session_id, "--", "bash", "-ec", """
test -f /context/git/forge/source
test ! -e /context/git/forge.git/org/repo
""")
        self.assertEqual(set(self.repositories()), {"workspace", "forge"})

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
