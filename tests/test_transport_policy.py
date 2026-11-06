import sys
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy" / "addons"))
from transport_policy import compile_http1_hosts


class HTTP1HostsTest(unittest.TestCase):
    def test_empty_list_and_default_port(self):
        self.assertEqual(compile_http1_hosts([]), [])
        rule, = compile_http1_hosts([{"host": r"api\.example"}])
        self.assertTrue(rule.matches("api.example", 443))
        self.assertFalse(rule.matches("api.example", 8443))
        self.assertFalse(rule.matches("api.example.evil", 443))

    def test_regex_hosts_ports_and_environment_expansion(self):
        with patch.dict("os.environ", TEST_HTTP1_HOST="api", TEST_HTTP1_PORT="844[34]"):
            rule, = compile_http1_hosts([{"host": r"${TEST_HTTP1_HOST}\.example",
                                         "port": "${TEST_HTTP1_PORT}"}])
        for port in (8443, 8444):
            self.assertTrue(rule.matches("api.example", port))
        self.assertFalse(rule.matches("api.example", 443))

    def test_invalid_entries_and_regexes_are_rejected(self):
        invalid = [None, {}, "api.example", [None], ["api.example"], [{}],
                   [{"host": ""}], [{"host": 123}], [{"host": "api.example", "paths": ["/"]}]]
        invalid += [[{"host": "api.example", "port": port}]
                    for port in (True, None, [], 0, 65536, "")]
        invalid += [[{"host": "["}], [{"host": "api.example", "port": "["}]]
        for config in invalid:
            with self.subTest(config=config), self.assertRaises(ValueError):
                compile_http1_hosts(config)


if __name__ == "__main__":
    unittest.main()
