"""Nested proxy commands and saved session provider configuration."""

import io
import json
import os
import runpy
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[1]


class ProxyCliTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        env = patch.dict(os.environ, AGENTBOX_HOME=str(ROOT), AGENTBOX_STATE=str(self.root / "state"))
        env.start()
        self.addCleanup(env.stop)
        self.agentbox = runpy.run_path(str(ROOT / "bin" / "agentbox"))
        self.g = self.agentbox["main"].__globals__
        project = patch.dict(self.g, find_project_dir=lambda: self.project)
        project.start()
        self.addCleanup(project.stop)
        self.session_id = self.agentbox["_session_id"]("feature", self.project)
        self.session = self.root / "state" / "sessions" / self.session_id
        (self.session / "proxy-config").mkdir(parents=True)
        (self.session / "compose.yaml").write_text(yaml.safe_dump({
            "x-metadata": {"name": "feature", "project-dir": str(self.project)},
        }))
        self.config_path = self.session / "proxy-config" / "proxy.yaml"
        self.cfg = {
            "providers": [
                {"name": "openai", "enabled": False, "api_key_file": "private-token",
                 "request_policy": [{"host": r"api\.openai\.com"}]},
                {"name": "anthropic", "enabled": True, "api_key_env": "PRIVATE_ENV"},
                {"name": "vertex"},
            ],
            "extra_request_policy": [{"host": r"example\.com"}],
            "logging": {"log_bodies": False},
        }
        self.config_path.write_text(yaml.safe_dump(self.cfg))

    def cli(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        status = 0
        with (patch.object(sys, "argv", ["agentbox", *args]),
              redirect_stdout(output), redirect_stderr(errors)):
            try:
                self.agentbox["main"]()
            except SystemExit as exc:
                status = exc.code
        return output.getvalue(), errors.getvalue(), status

    def test_help_describes_nested_commands(self):
        for args, expected in (((), ["proxy"]),
                               (("proxy",), ["reload", "restart", "provider"]),
                               (("proxy", "provider"), ["list", "enable", "disable"])):
            with self.subTest(args=args):
                output, errors, status = self.cli(*args, "--help")
                self.assertEqual(status, 0, errors)
                for command in expected:
                    self.assertIn(command, output)
                self.assertNotIn("proxy-reload", output)
                self.assertNotIn("proxy-restart", output)

    def test_missing_or_invalid_commands_and_provider_names_are_usage_errors(self):
        for args in (("proxy",), ("proxy", "provider"), ("proxy", "unknown"),
                     ("proxy", "provider", "enable"), ("proxy", "provider", "disable"),
                     ("proxy-reload",), ("proxy-restart",)):
            with self.subTest(args=args):
                output, errors, status = self.cli(*args)
                self.assertEqual(status, 2)
                self.assertIn("usage:", errors)

    def test_reload_and_restart_dispatch_to_existing_synchronization(self):
        for action in ("reload", "restart"):
            for selector in ((), ("--name", "feature"), ("--session", self.session_id)):
                preflight = Mock(return_value=("full", "providers", []))
                synchronize = Mock()
                with (self.subTest(action=action, selector=selector),
                      patch.dict(self.g, _secret_preflight=preflight, _synchronize_proxy=synchronize),
                      patch.object(subprocess, "run") as run):
                    output, errors, status = self.cli("proxy", action, *selector)
                    self.assertEqual(status, 0, errors)
                    preflight.assert_called_once_with(self.session)
                    synchronize.assert_called_once_with(self.session, "full", "providers", [])
                    if action == "restart":
                        self.assertEqual(run.call_count, 1)
                        self.assertEqual(run.call_args.args[0][-2:], ["restart", "proxy"])
                    else:
                        run.assert_not_called()
                        self.assertIn("reloaded", output)

    def test_proxy_dispatches_through_top_level_command_handler(self):
        proxy = Mock()
        with patch.dict(self.g, cmd_proxy=proxy):
            output, errors, status = self.cli("proxy", "provider", "enable", "openai", "vertex")
        self.assertEqual(status, 0, errors)
        proxy.assert_called_once()
        args = proxy.call_args.args[0]
        self.assertEqual(args.subcmd, "provider")
        self.assertEqual(args.provider_cmd, "enable")
        self.assertEqual(args.providers, ["openai", "vertex"])

    def test_provider_list_reports_saved_status_without_credentials_or_podman(self):
        before = self.config_path.read_bytes()
        with patch.object(subprocess, "run") as run:
            output, errors, status = self.cli("proxy", "provider", "list")
        self.assertEqual(status, 0, errors)
        self.assertIn("NAME", output)
        self.assertIn("STATUS", output)
        self.assertRegex(output, r"openai\s+disabled")
        self.assertRegex(output, r"anthropic\s+enabled")
        self.assertRegex(output, r"vertex\s+disabled")
        self.assertNotIn("private-token", output)
        self.assertNotIn("PRIVATE_ENV", output)
        self.assertEqual(self.config_path.read_bytes(), before)
        run.assert_not_called()

    def test_provider_list_json_with_global_session_selector(self):
        with patch.dict(self.g, find_project_dir=lambda: self.root / "unrelated"):
            output, errors, status = self.cli("proxy", "provider", "list", "--json", "-s", self.session_id)
        self.assertEqual(status, 0, errors)
        self.assertEqual(json.loads(output), [
            {"name": "openai", "enabled": False},
            {"name": "anthropic", "enabled": True},
            {"name": "vertex", "enabled": False},
        ])

    def test_provider_enable_disable_only_change_selected_flag(self):
        compose_before = (self.session / "compose.yaml").read_bytes()
        for action, name, enabled in (("enable", "openai", True),
                                      ("disable", "anthropic", False),
                                      ("enable", "vertex", True)):
            with self.subTest(action=action, name=name), patch.object(subprocess, "run") as run:
                output, errors, status = self.cli("proxy", "provider", action, name, "-n", "feature")
                self.assertEqual(status, 0, errors)
                self.assertIn("saved configuration", output)
                self.assertIn("agentbox proxy reload", output)
                self.assertIn("start", output)
                for provider in self.cfg["providers"]:
                    if provider["name"] == name:
                        provider["enabled"] = enabled
                self.assertEqual(yaml.safe_load(self.config_path.read_text()), self.cfg)
                self.assertEqual((self.session / "compose.yaml").read_bytes(), compose_before)
                run.assert_not_called()

    def test_already_enabled_or_disabled_does_not_rewrite_config(self):
        before = self.config_path.read_bytes()
        for action, names in (("enable", ("anthropic",)), ("disable", ("openai",)),
                              ("disable", ("vertex",)), ("disable", ("openai", "vertex"))):
            with self.subTest(action=action, names=names):
                output, errors, status = self.cli("proxy", "provider", action, *names)
                self.assertEqual(status, 0, errors)
                self.assertIn("already", output)
                self.assertIn("agentbox proxy reload", output)
                self.assertEqual(self.config_path.read_bytes(), before)

    def test_enable_disable_multiple_providers_save_once_and_preserve_other_settings(self):
        compose_before = (self.session / "compose.yaml").read_bytes()
        for action, names, enabled in (("enable", ("openai", "vertex"), True),
                                       ("disable", ("openai", "anthropic"), False)):
            save = Mock(wraps=self.g["save_yaml"])
            with (self.subTest(action=action, names=names), patch.dict(self.g, save_yaml=save),
                  patch.object(subprocess, "run") as run):
                output, errors, status = self.cli("proxy", "provider", action, *names, "-s", self.session_id)
                self.assertEqual(status, 0, errors)
                for provider in self.cfg["providers"]:
                    if provider["name"] in names:
                        provider["enabled"] = enabled
                self.assertEqual(yaml.safe_load(self.config_path.read_text()), self.cfg)
                self.assertEqual((self.session / "compose.yaml").read_bytes(), compose_before)
                save.assert_called_once_with(self.config_path, self.cfg)
                self.assertEqual(output.count("saved configuration"), len(names))
                run.assert_not_called()

    def test_repeated_provider_names_are_only_updated_and_reported_once(self):
        for action in ("enable", "disable"):
            save = Mock(wraps=self.g["save_yaml"])
            with self.subTest(action=action), patch.dict(self.g, save_yaml=save):
                output, errors, status = self.cli("proxy", "provider", action,
                                                 "openai", "vertex", "openai", "vertex")
                self.assertEqual(status, 0, errors)
                save.assert_called_once()
                self.assertEqual(output.count("Provider 'openai'"), 1)
                self.assertEqual(output.count("Provider 'vertex'"), 1)
                self.assertLess(output.index("Provider 'openai'"), output.index("Provider 'vertex'"))

    def test_mixed_changed_and_unchanged_providers(self):
        output, errors, status = self.cli("proxy", "provider", "enable", "anthropic", "openai")
        self.assertEqual(status, 0, errors)
        self.assertIn("Provider 'anthropic' already enabled", output)
        self.assertIn("Provider 'openai' enabled", output)
        saved = yaml.safe_load(self.config_path.read_text())
        self.assertTrue(saved["providers"][0]["enabled"])
        self.assertTrue(saved["providers"][1]["enabled"])
        self.assertNotIn("enabled", saved["providers"][2])

    def test_unknown_provider_fails_without_changing_config(self):
        before = self.config_path.read_bytes()
        for action in ("enable", "disable"):
            output, errors, status = self.cli("proxy", "provider", action, "missing")
            self.assertEqual(status, 1)
            self.assertIn("provider 'missing' not found", errors)
            self.assertIn("agentbox proxy provider list", errors)
            self.assertEqual(self.config_path.read_bytes(), before)

    def test_unknown_name_in_batch_does_not_save_partial_changes(self):
        before = self.config_path.read_bytes()
        for action, names in (("enable", ("openai", "missing", "vertex")),
                              ("disable", ("anthropic", "missing")),
                              ("enable", ("missing", "openai"))):
            save = Mock(wraps=self.g["save_yaml"])
            with self.subTest(action=action, names=names), patch.dict(self.g, save_yaml=save):
                output, errors, status = self.cli("proxy", "provider", action, *names)
                self.assertEqual(status, 1)
                self.assertIn("provider 'missing' not found", errors)
                self.assertEqual(output, "")
                self.assertEqual(self.config_path.read_bytes(), before)
                save.assert_not_called()

    def test_empty_providers(self):
        for cfg in ({}, {"providers": []}):
            self.config_path.write_text(yaml.safe_dump(cfg))
            output, errors, status = self.cli("proxy", "provider", "list")
            self.assertEqual(status, 0, errors)
            self.assertIn("No providers configured", output)
            output, errors, status = self.cli("proxy", "provider", "list", "--json")
            self.assertEqual(status, 0, errors)
            self.assertEqual(json.loads(output), [])

    def test_invalid_config_fails_cleanly_without_rewriting(self):
        for text in ("providers: [", "[]", "", "providers: {}", "providers: null",
                     "providers: [invalid]", "providers: [{}]", "providers: [{name: 3}]",
                     "providers: [{name: ' '}]", "providers: [{name: test, enabled: 'false'}]",
                     "providers: [{name: test}, {name: test}]"):
            self.config_path.write_text(text)
            for args in (("list",), ("enable", "test"), ("disable", "test")):
                with self.subTest(text=text, args=args):
                    output, errors, status = self.cli("proxy", "provider", *args)
                    self.assertEqual(status, 1)
                    self.assertIn("Invalid session provider configuration", errors)
                    self.assertEqual(self.config_path.read_text(), text)

    def test_missing_config_fails_cleanly(self):
        self.config_path.unlink()
        output, errors, status = self.cli("proxy", "provider", "list")
        self.assertEqual(status, 1)
        self.assertIn("Invalid session provider configuration", errors)

    def test_session_selectors_remain_mutually_exclusive(self):
        for args in (("reload",), ("restart",), ("provider", "list"),
                     ("provider", "enable", "openai"), ("provider", "disable", "openai")):
            with self.subTest(args=args):
                output, errors, status = self.cli("proxy", *args, "-n", "feature", "-s", self.session_id)
                self.assertEqual(status, 1)
                self.assertIn("mutually exclusive", errors)

    def test_provider_edit_waits_for_synchronization_lock(self):
        before = self.config_path.read_bytes()
        started, finished = threading.Event(), threading.Event()
        results = []

        def enable():
            started.set()
            results.append(self.cli("proxy", "provider", "enable", "openai"))
            finished.set()

        with self.agentbox["_secret_sync_lock"](self.session):
            thread = threading.Thread(target=enable)
            thread.start()
            self.assertTrue(started.wait(2))
            self.assertFalse(finished.wait(0.05))
            self.assertEqual(self.config_path.read_bytes(), before)
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0][2], 0, results[0][1])
        self.assertTrue(yaml.safe_load(self.config_path.read_text())["providers"][0]["enabled"])


if __name__ == "__main__":
    unittest.main()
