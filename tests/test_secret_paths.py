import runpy
import io
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


try:
    import yaml  # noqa: F401
except ModuleNotFoundError:
    yaml = types.ModuleType("yaml")
    yaml.safe_load = lambda stream: {}
    yaml.dump = lambda *args, **kwargs: None
    sys.modules["yaml"] = yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "proxy" / "addons"))

from resolvers import StaticKeyResolver, _secret_path


class SecretPathTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agentbox = runpy.run_path(str(ROOT / "bin" / "agentbox"))

    def test_target_matches_resolver_path(self):
        import hashlib
        for key_file in ("~/.keys/openai/token", "./a/token", "/a/token", "/clé/token", "a" * 242):
            with self.subTest(source=key_file):
                expected = hashlib.sha256(key_file.encode()).hexdigest()[:12] + "-" + Path(key_file).name
                self.assertEqual(self.agentbox["injection_secret_name"](key_file), expected)
                self.assertEqual(_secret_path(key_file), f"/run/secrets/{expected}")

    def test_invalid_target_contract(self):
        for source in (".", "..", "/", "/a/.", "/a/..", "a" * 243, "/a/key name", "/a/clé", 123):
            for implementation in (self.agentbox["injection_secret_name"], _secret_path):
                with self.subTest(source=source), self.assertRaises((ValueError, TypeError)):
                    implementation(source)

    def test_top_level_and_rule_files_with_same_basename_get_distinct_targets_without_mounts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "first" / "token"
            second = root / "second" / "token"
            third = root / "third" / "token"
            first.parent.mkdir()
            second.parent.mkdir()
            third.parent.mkdir()
            first.write_text("first")
            second.write_text("second")
            third.write_text("third")

            generated = {}
            generate_compose = self.agentbox["generate_compose"]
            generate_compose.__globals__["load_yaml"] = lambda path: {
                "services": {
                    "proxy": {"volumes": [], "environment": {}},
                    "agent": {"volumes": [], "environment": {}},
                },
            }
            generate_compose.__globals__["save_yaml"] = (
                lambda path, config: generated.update(config)
            )

            config = {"providers": [{
                "name": "openai", "enabled": True, "api_key_file": str(first),
                "request_policy": [{"host": r"api\.openai\.com",
                                    "injection_policy": [{"api_key_file": str(second)}]}],
            }, {"name": "openai2", "enabled": True, "api_key_file": str(third)}]}
            references = self.agentbox["discover_secrets"](config, root)
            self.assertEqual(len({ref.target for ref in references}), 3)
            generate_compose(
                session_dir=root,
                project_dir=root,
                name="test",
                port=8081,
                proxy_cfg=config,
                agent_cfg={},
                context_mounts=[],
            )

            mounts = generated["services"]["proxy"]["volumes"]
            secret_mounts = [mount for mount in mounts if "/run/secrets/" in mount]
            self.assertEqual(secret_mounts, [])
            for ref in references:
                self.assertNotIn(str(ref.host_path), str(generated))
                self.assertNotIn(ref.target, str(generated))

    def test_resolver_reads_synchronized_target_and_strips_whitespace(self):
        attempted_paths = []

        def open_secret(path, **kwargs):
            attempted_paths.append(path)
            if path == _secret_path("/keys/token"):
                return io.StringIO(" synchronized-token\n")
            raise FileNotFoundError(path)

        with patch("resolvers.open", side_effect=open_secret, create=True):
            resolver = StaticKeyResolver({"api_key_file": "/keys/token"})

        self.assertEqual(resolver.resolve(), "synchronized-token")
        self.assertEqual(attempted_paths, [_secret_path("/keys/token")])

    def test_missing_synchronized_target_is_unavailable(self):
        attempted_paths = []

        def open_secret(path, **kwargs):
            attempted_paths.append(path)
            raise FileNotFoundError(path)

        with self.assertLogs("proxy", level="WARNING"):
            with patch("resolvers.open", side_effect=open_secret, create=True):
                resolver = StaticKeyResolver({
                    "api_key_file": "/new/location/token",
                })

        self.assertIsNone(resolver.resolve())
        self.assertEqual(attempted_paths, [_secret_path("/new/location/token")])


if __name__ == "__main__":
    unittest.main()
