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

    def test_namespaced_mount_matches_resolver_path(self):
        key_file = "~/.keys/openai/token"
        secret_name = self.agentbox["injection_secret_name"](key_file)

        self.assertEqual(_secret_path(key_file, True), f"/run/secrets/{secret_name}")
        self.assertEqual(_secret_path(key_file, False), "/run/secrets/token")

    def test_legacy_and_rule_files_with_same_basename_get_distinct_mounts(self):
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

            generate_compose(
                session_dir=root,
                project_dir=root,
                name="test",
                port=8081,
                proxy_cfg={"providers": [{
                    "name": "openai",
                    "enabled": True,
                    "api_key_file": str(first),
                    "request_policy": [{
                        "host": r"api\.openai\.com",
                        "injection_policy": [{"api_key_file": str(second)}],
                    }],
                }, {
                    "name": "openai2",
                    "enabled": True,
                    "api_key_file": str(third),
                }]},
                agent_cfg={},
                context_mounts=[],
            )

            mounts = generated["services"]["proxy"]["volumes"]
            secret_mounts = [mount for mount in mounts if "/run/secrets/" in mount]
            self.assertEqual(len(secret_mounts), 3)
            self.assertEqual(len({mount.split(":")[1] for mount in secret_mounts}), 3)

    def test_legacy_resolver_falls_back_to_basename_mount(self):
        attempted_paths = []

        def open_secret(path):
            attempted_paths.append(path)
            if path == "/run/secrets/token":
                return io.StringIO("legacy-token")
            raise FileNotFoundError(path)

        with patch("resolvers.open", side_effect=open_secret, create=True):
            resolver = StaticKeyResolver({"api_key_file": "/old/location/token"})

        self.assertEqual(resolver.resolve(), "legacy-token")
        self.assertEqual(attempted_paths, [
            _secret_path("/old/location/token", True),
            "/run/secrets/token",
        ])

    def test_scoped_resolver_does_not_fall_back_to_legacy_mount(self):
        attempted_paths = []

        def open_secret(path):
            attempted_paths.append(path)
            raise FileNotFoundError(path)

        with self.assertLogs("proxy", level="ERROR"):
            with patch("resolvers.open", side_effect=open_secret, create=True):
                resolver = StaticKeyResolver({
                    "api_key_file": "/new/location/token",
                    "_require_namespaced_secret": True,
                })

        self.assertIsNone(resolver.resolve())
        self.assertEqual(attempted_paths, [_secret_path("/new/location/token", True)])


if __name__ == "__main__":
    unittest.main()
