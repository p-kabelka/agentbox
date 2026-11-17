"""Opt-in migration test; build base/proxy at dc4dfe0 under AGENTBOX_TEST_LEGACY_IMAGE_TAG."""

import json
import os
import shutil
import sys
import unittest

import yaml

from integration_support import AgentboxIntegrationTest, ROOT


@unittest.skipUnless(os.environ.get("AGENTBOX_TEST_LEGACY_IMAGE_TAG"), "Requires images from dc4dfe0")
class LegacyMigrationIntegrationTest(AgentboxIntegrationTest):
    def setUp(self):
        super().setUp()
        self.legacy = self.root / "legacy"
        self.legacy.mkdir()
        archive = self.root / "legacy.tar"
        self.run_command(["git", "-C", str(ROOT), "archive", "-o", str(archive), "dc4dfe0"])
        self.run_command(["tar", "-xf", str(archive), "-C", str(self.legacy)])
        shutil.copytree(self.home / "custom" / "presets" / "integration",
                        self.legacy / "custom" / "presets" / "integration")
        self.addCleanup(self.legacy_cli, "remove", "--name", "feature", check=False)

    def legacy_cli(self, *args, **kwargs):
        env = self.env
        self.env = {**env, "AGENTBOX_HOME": str(self.legacy),
                    "AGENTBOX_IMAGE_TAG": env["AGENTBOX_TEST_LEGACY_IMAGE_TAG"]}
        try:
            return self.run_command([sys.executable, str(self.legacy / "bin" / "agentbox"), *args], **kwargs)
        finally:
            self.env = env

    def migrate(self, *args, **kwargs):
        return self.run_command([sys.executable, str(ROOT / "bin" / "agentbox-migrate-dc4dfe0"),
                                 "--session", self.session_id, "--new-name", "upgraded", *args], **kwargs)

    def cleanup_upgrade(self):
        sessions = json.loads(self.cli("list", "--json").stdout)
        upgraded = next((s["session_id"] for s in sessions if s["name"] == "upgraded"), None)
        if upgraded:
            config = self.session.with_name(upgraded) / "compose.yaml"
            image = yaml.safe_load(config.read_text())["services"]["agent"]["image"]
            self.cli("remove", "--session", upgraded)
            if image.endswith(":migrated-dc4dfe0"):
                self.run_command(["podman", "rmi", image])

    def test_migrate_stopped_agent_and_git_history(self):
        self.legacy_cli("init", str(self.project), "--name", "feature", "--preset", "integration", "--persist")
        self.legacy_cli("start", "--name", "feature", "--", "bash", "-ec", """
if [ ! -e /root/migrated-home ]; then
    git switch -c legacy-output
    echo published > source
    git commit -am published
    git tag legacy-tag
    git push origin HEAD --tags
    git commit --allow-empty -m local-only
    echo dirty >> source
    echo untracked > untracked
    echo saved > /root/migrated-home
    install -d -m 750 /opt/migrated
    echo settings > /etc/migrated.conf
    ln -s /etc/migrated.conf /opt/migrated/settings
    printf '#!/bin/sh\\ncat /opt/migrated/settings\\n' > /usr/local/bin/migrated-tool
    chmod 755 /usr/local/bin/migrated-tool
    rpm -e jq
else
    test "$(cat /root/migrated-home)" = saved
    test "$(cat untracked)" = untracked
    test "$(git branch --show-current)" = legacy-output
    test "$(git log -1 --format=%s)" = local-only
    test "$(git status --porcelain)" = "$(printf ' M source\\n?? untracked')"
    test "$(stat -c %a /opt/migrated)" = 750
    test -L /opt/migrated/settings
    test "$(migrated-tool)" = settings
    if rpm -q jq; then exit 1; fi
    test ! -e /usr/bin/jq
    rpm -q git
    test ! -e /usr/bin/ip
    command -v agentbox-network
    command -v agentbox-clone
    test "$(git remote get-url origin)" = git://git/workspace.git
    git ls-remote --exit-code origin refs/heads/legacy-output
    git ls-remote --exit-code origin refs/tags/legacy-tag
    git push origin HEAD:refs/heads/after-migration
fi
""")
        original_cfg = (self.session / "compose.yaml").read_bytes()
        original_agent = self.config()["services"]["agent"]["container_name"]
        self.cli("stop", "--session", self.session_id)
        self.addCleanup(self.cleanup_upgrade)
        self.migrate()
        self.assertEqual((self.session / "compose.yaml").read_bytes(), original_cfg)
        old_status = self.run_command(["podman", "inspect", "--format", "{{.State.Status}}", original_agent])
        self.assertEqual(old_status.stdout.strip(), "exited")
        self.assertNotEqual(self.migrate(check=False).returncode, 0)

        sessions = json.loads(self.cli("list", "--json").stdout)
        upgraded = next(s["session_id"] for s in sessions if s["name"] == "upgraded")
        containers = json.loads(self.cli("containers", "--session", upgraded, "--json").stdout)
        agent = next(c for c in containers if c["name"].endswith("-agent"))
        holder = next(c for c in containers if c["name"].endswith("-network"))
        info = json.loads(self.run_command(["podman", "inspect", agent["name"]]).stdout)[0]
        self.assertTrue(info["HostConfig"]["NetworkMode"].startswith("container:"))
        namespace_owner = info["HostConfig"]["NetworkMode"].partition(":")[2]
        actual_holder = self.run_command(["podman", "inspect", "--format", "{{.Name}}", namespace_owner])
        self.assertEqual(actual_holder.stdout.strip(), holder["name"])
        self.assertFalse({"/source/project.bundle", "/output/repo.git"} & {m["Destination"] for m in info["Mounts"]})
        self.cli("start", "--name", "upgraded")
        remote = f"agentbox-{upgraded}"
        self.git(self.project, "fetch", remote, "--tags")
        self.assertEqual(self.git(self.project, "show", f"{remote}/legacy-output:source").stdout, "published\n")
        self.assertEqual(self.git(self.project, "log", "-1", "--format=%s", f"{remote}/after-migration").stdout,
                         "local-only\n")
        self.assertEqual(self.git(self.project, "show", "legacy-tag:source").stdout, "published\n")
        self.assertEqual((self.project / "source").read_text(), "host\n")

    def test_unstarted_bind_requires_explicit_opt_in(self):
        self.legacy_cli("init", str(self.project), "--name", "feature", "--preset", "integration",
                        "--persist", "--no-git")
        self.addCleanup(self.cli, "remove", "--name", "upgraded", check=False)
        refused = self.migrate(check=False)
        self.assertIn("explicit --no-git", refused.stderr)
        failed = self.migrate("--no-git", "--agent-image", "localhost/agentbox-test-missing:no-such-tag", check=False)
        self.assertIn("is not available locally", failed.stderr)
        self.assertEqual([self.session], sorted(self.session.parent.iterdir()))
        self.migrate("--no-git")
        self.cli("start", "--name", "upgraded", "--", "bash", "-ec", 'test "$(cat source)" = host')
