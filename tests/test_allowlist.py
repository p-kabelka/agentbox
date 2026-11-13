"""Allow/deny CLI parsing and saved host patterns, without running containers."""

import io
import re
import runpy
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[1]


class AllowlistTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.session = Path(temp.name)
        (self.session / "proxy-config").mkdir()
        self.config_path = self.session / "proxy-config" / "proxy.yaml"
        self.config_path.write_text("extra_request_policy: []\n")
        (self.session / "compose.yaml").write_text("x-metadata:\n  name: testing\n")
        self.agentbox = runpy.run_path(str(ROOT / "bin" / "agentbox"))
        self.reload = Mock()
        p = patch.dict(self.agentbox["main"].__globals__,
                       resolve_session=lambda args: (self.session, self.session),
                       _reload_proxy=self.reload)
        p.start()
        self.addCleanup(p.stop)

    def command(self, *args):
        with patch.object(sys, "argv", ["agentbox", *args]), redirect_stdout(io.StringIO()):
            self.agentbox["main"]()
        self.reload.assert_called_with(self.session)

    def rules(self):
        return yaml.safe_load(self.config_path.read_text())["extra_request_policy"]

    def test_literal_hosts_are_escaped_by_default(self):
        for host in ("pypi.org", r".*\.example\.com"):
            with self.subTest(host=host):
                self.command("allow", host)
                self.assertEqual(self.rules(), [{"host": re.escape(host)}])
                self.command("deny", host)
                self.assertEqual(self.rules(), [])

    def test_both_regex_flags_preserve_patterns_verbatim(self):
        pattern = r"^(?:api|cdn)[0-9]+\.example.com$"
        for flag in ("-r", "--regex"):
            with self.subTest(flag=flag):
                self.command("allow", flag, pattern)
                self.assertEqual(self.rules(), [{"host": pattern}])
                self.command("deny", pattern, flag)
                self.assertEqual(self.rules(), [])

    def test_regex_and_literal_rules_are_distinct_and_not_duplicated(self):
        pattern = r".*\.example\.com"
        self.command("allow", pattern)
        self.command("allow", "-r", pattern)
        expected = [{"host": re.escape(pattern)}, {"host": pattern}]
        self.assertEqual(self.rules(), expected)
        self.command("allow", "--regex", pattern)
        self.assertEqual(self.rules(), expected)
        self.command("deny", pattern)
        self.assertEqual(self.rules(), [{"host": pattern}])
        self.command("deny", "-r", pattern)
        self.assertEqual(self.rules(), [])

    def test_regex_deny_removes_the_pattern_not_matching_literal_rules(self):
        pattern = r".*\.example\.com"
        self.command("allow", "-r", pattern)
        self.command("allow", "api.example.com")
        self.command("deny", "--regex", pattern)
        self.assertEqual(self.rules(), [{"host": r"api\.example\.com"}])

    def test_host_port_syntax_works_with_and_without_regex(self):
        for flags in ([], ["-r"], ["--regex"]):
            with self.subTest(flags=flags):
                host = r"(?:api|cdn)\.example\.com" if flags else "api.example.com"
                pattern = host if flags else re.escape(host)
                self.command("allow", *flags, host + ":8443")
                self.assertEqual(self.rules(), [{"host": pattern, "port": 8443}])
                self.command("deny", *flags, host + ":8443")
                self.assertEqual(self.rules(), [])

    def test_regex_default_port_matching_is_unchanged(self):
        pattern = r".*\.example\.com"
        rules = [{"host": pattern}, {"host": pattern, "port": 443},
                 {"host": pattern, "port": 8443}]
        self.config_path.write_text(yaml.safe_dump({"extra_request_policy": rules}))
        self.command("allow", "-r", pattern)
        self.assertEqual(self.rules(), rules)
        self.command("deny", "--regex", pattern)
        self.assertEqual(self.rules(), [{"host": pattern, "port": 8443}])


if __name__ == "__main__":
    unittest.main()
