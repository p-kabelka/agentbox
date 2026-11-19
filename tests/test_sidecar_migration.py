"""Temporary upgrade from direct proxy/Git attachments to network holders."""

import copy
import io
import runpy
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import call, patch

import yaml


ROOT = Path(__file__).resolve().parents[1]


class SidecarMigrationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.session = Path(temporary.name)
        self.compose = self.session / "compose.yaml"
        self.backup = self.session / "compose.yaml.pre-sidecar-holders"
        self.g = runpy.run_path(str(ROOT / "bin/agentbox"))["main"].__globals__
        with patch.dict(self.g, AGENTBOX_HOME=ROOT, _resolve_images=lambda images: images):
            self.g["generate_compose"](self.session, self.session, "feature", 9876, {}, {}, [], persist=True)
        self.template = yaml.safe_load(self.compose.read_text())

    def legacy(self):
        saved = copy.deepcopy(self.template)
        services = saved["services"]
        for name in ("proxy", "git"):
            holder = services.pop(f"{name}-network")
            services[name].pop("depends_on")
            services[name].pop("network_mode")
            services[name]["networks"] = list(holder["networks"])
        services["proxy"]["ports"] = ["127.0.0.1:9876:8081"]
        services["proxy"]["extra_hosts"] = ["api.test:192.0.2.1"]
        services["proxy"]["dns"] = ["192.0.2.53"]
        services["proxy"]["environment"]["CUSTOM"] = "kept in proxy"
        services["agent"]["command"] = ["bash", "-l"]
        services["network"]["image"] = "localhost/agentbox-network:retained"
        return saved

    def write(self, cfg):
        self.compose.write_text(yaml.safe_dump(cfg))
        return self.compose.read_bytes()

    def migrate(self):
        with patch.dict(self.g, AGENTBOX_HOME=ROOT), redirect_stdout(io.StringIO()):
            self.g["_migrate_sidecar_holders"](self.session)

    def test_only_the_sidecars_are_replaced_and_settings_and_backup_are_preserved(self):
        legacy = self.legacy()
        legacy["services"]["git"]["networks"] = {"agent-net": {"aliases": ["extra-git", "git"]}}
        original = self.write(legacy)
        with patch.object(subprocess, "run") as run:
            self.migrate()
            first_migration = self.compose.read_bytes()
            self.migrate()
        names = [legacy["services"][name]["container_name"] for name in ("proxy", "git")]
        self.assertEqual(run.call_args_list, [
            call(["podman", "stop", "--ignore", *names], check=True),
            call(["podman", "rm", "--ignore", *names], check=True),
        ])
        self.assertEqual(self.compose.read_bytes(), first_migration)
        self.assertEqual(self.backup.read_bytes(), original)
        migrated = yaml.safe_load(first_migration)
        for key in ("volumes", "networks", "x-metadata"):
            self.assertEqual(migrated[key], legacy[key])
        for name in ("agent", "guard", "network"):
            self.assertEqual(migrated["services"][name], legacy["services"][name])
        for name in ("proxy", "git"):
            service = migrated["services"][name]
            holder = migrated["services"][f"{name}-network"]
            self.assertEqual(service["network_mode"], f"service:{name}-network")
            self.assertEqual(service["depends_on"], [f"{name}-network"])
            self.assertEqual(service["volumes"], legacy["services"][name]["volumes"])
            self.assertEqual(holder["image"], "localhost/agentbox-network:retained")
            self.assertNotIn("volumes", holder)
        proxy = migrated["services"]["proxy"]
        proxy_holder = migrated["services"]["proxy-network"]
        self.assertEqual(proxy["environment"], legacy["services"]["proxy"]["environment"])
        for key in ("ports", "dns", "extra_hosts"):
            self.assertEqual(proxy_holder[key], legacy["services"]["proxy"][key])
            self.assertNotIn(key, proxy)
        self.assertEqual(migrated["services"]["git-network"]["networks"], {
            "agent-net": {"aliases": ["extra-git", "git"]},
        })

    def test_failure_keeps_original_compose_and_allows_retry(self):
        original = self.write(self.legacy())
        with patch.object(subprocess, "run", side_effect=[None, subprocess.CalledProcessError(1, "rm")]):
            with self.assertRaises(subprocess.CalledProcessError):
                self.migrate()
        self.assertEqual(self.compose.read_bytes(), original)
        self.assertEqual(self.backup.read_bytes(), original)
        self.assertEqual(list(self.session.glob(".compose-migration-*")), [])
        with patch.object(subprocess, "run"):
            self.migrate()
        self.assertEqual(self.backup.read_bytes(), original)
        self.assertTrue(self.g["_has_sidecar_holders"](yaml.safe_load(self.compose.read_text())["services"]))

    def test_unrecognized_layout_is_not_modified(self):
        legacy = self.legacy()
        legacy["services"]["git"]["networks"].append("proxy-net")
        original = self.write(legacy)
        with (patch.object(subprocess, "run") as run, redirect_stderr(io.StringIO()),
              self.assertRaises(SystemExit)):
            self.migrate()
        run.assert_not_called()
        self.assertEqual(self.compose.read_bytes(), original)
        self.assertFalse(self.backup.exists())

    def test_already_migrated_is_left_untouched(self):
        original = self.compose.read_bytes()
        with patch.object(subprocess, "run") as run:
            self.migrate()
        run.assert_not_called()
        self.assertEqual(self.compose.read_bytes(), original)
        self.assertFalse(self.backup.exists())

    def test_host_git_cannot_recreate_a_legacy_sidecar(self):
        self.write(self.legacy())
        with (patch.dict(self.g, compose=lambda *args, **kwargs: self.fail("started Git")),
              redirect_stderr(io.StringIO()) as error, self.assertRaises(SystemExit)):
            self.g["_start_git"](self.session)
        self.assertIn("agentbox start --session", error.getvalue())


if __name__ == "__main__":
    unittest.main()
