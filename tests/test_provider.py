import sys
import types
import unittest
from pathlib import Path


try:
    from mitmproxy import http  # noqa: F401
except ModuleNotFoundError:
    mitmproxy = types.ModuleType("mitmproxy")
    mitmproxy.http = types.SimpleNamespace(HTTPFlow=object)
    sys.modules["mitmproxy"] = mitmproxy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy" / "addons"))

from provider import Provider, inject_matching_providers


class FakeResolver:
    def __init__(self, config):
        self._value = (
            config.get("api_key_file")
            or config.get("api_key_env")
            or config.get("test_value")
        )

    def resolve(self):
        return self._value


class Headers(dict):
    def __init__(self, values=()):
        super().__init__()
        for key, value in dict(values).items():
            self[key] = value

    def __getitem__(self, key):
        return super().__getitem__(key.lower())

    def __setitem__(self, key, value):
        super().__setitem__(key.lower(), value)

    def get(self, key, default=None):
        return super().get(key.lower(), default)

    def copy(self):
        return Headers(self)


def make_flow(headers, path="/v1/responses", method="POST"):
    request = types.SimpleNamespace(
        pretty_host="api.openai.com",
        port=443,
        path=path,
        method=method,
        headers=Headers(headers),
    )
    return types.SimpleNamespace(request=request)


def provider_config(**overrides):
    config = {
        "name": "openai",
        "request_policy": [{
            "host": r"api\.openai\.com",
            "paths": [r"/v1/responses$"],
            "methods": ["POST"],
        }],
    }
    config.update(overrides)
    return config


class ProviderTest(unittest.TestCase):
    def test_injects_multiple_credentials(self):
        provider = Provider(provider_config(injection_policy=[
            {
                "inject_header": "Authorization",
                "inject_prefix": "Bearer ",
                "replace_token": "dummy",
                "test_value": "auth-token",
            },
            {
                "inject_header": "Cookie",
                "inject_prefix": "d=",
                "replace_token": "dummy-cookie",
                "test_value": "cookie-token",
            },
        ]), FakeResolver)
        flow = make_flow({
            "Authorization": "Bearer dummy",
            "Cookie": "d=dummy-cookie",
        })

        self.assertTrue(provider.matches(flow))
        self.assertTrue(provider.inject(flow, flow.request.headers.copy()))
        self.assertEqual(flow.request.headers["Authorization"], "Bearer auth-token")
        self.assertEqual(flow.request.headers["Cookie"], "d=cookie-token")

    def test_skips_only_injection_policy_with_wrong_placeholder(self):
        provider = Provider(provider_config(injection_policy=[
            {
                "inject_header": "Authorization",
                "inject_prefix": "Bearer ",
                "replace_token": "expected",
                "test_value": "auth-token",
            },
            {
                "inject_header": "Cookie",
                "replace_token": "dummy-cookie",
                "test_value": "cookie-token",
            },
        ]), FakeResolver)
        flow = make_flow({
            "Authorization": "Bearer other",
            "Cookie": "dummy-cookie",
        })

        self.assertTrue(provider.inject(flow, flow.request.headers.copy()))
        self.assertEqual(flow.request.headers["Authorization"], "Bearer other")
        self.assertEqual(flow.request.headers["Cookie"], "cookie-token")

    def test_last_injection_policy_for_header_wins(self):
        provider = Provider(provider_config(injection_policy=[
            {
                "inject_header": "x-token",
                "replace_token": "dummy",
                "test_value": "first",
            },
            {
                "inject_header": "x-token",
                "replace_token": "dummy",
                "test_value": "second",
            },
        ]), FakeResolver)
        flow = make_flow({"x-token": "dummy"})

        self.assertTrue(provider.inject(flow, flow.request.headers.copy()))
        self.assertEqual(flow.request.headers["x-token"], "second")

    def test_last_provider_for_header_wins(self):
        first = Provider(provider_config(
            inject_header="x-token",
            replace_token="dummy",
            test_value="first",
        ), FakeResolver)
        second = Provider(provider_config(
            name="openai2",
            inject_header="x-token",
            replace_token="dummy",
            test_value="second",
        ), FakeResolver)
        flow = make_flow({"x-token": "dummy"})

        injected = inject_matching_providers([first, second], flow)

        self.assertEqual(injected, ["openai", "openai2"])
        self.assertEqual(flow.request.headers["x-token"], "second")

    def test_nested_policy_does_not_inherit_legacy_secret_source(self):
        provider = Provider(provider_config(
            api_key_file="legacy-file",
            injection_policy=[{
                "api_key_env": "nested-env",
                "inject_header": "x-token",
            }],
        ), FakeResolver)
        flow = make_flow({})

        self.assertTrue(provider.inject(flow))
        self.assertEqual(flow.request.headers["x-token"], "nested-env")

    def test_provider_policy_and_request_rule_policy_have_different_scopes(self):
        provider = Provider(provider_config(
            injection_policy=[{
                "inject_header": "Cookie",
                "replace_token": "dummy-cookie",
                "test_value": "cookie-token",
            }],
            request_policy=[
                {
                    "host": r"api\.openai\.com",
                    "paths": [r"/v1/public$"],
                    "methods": ["GET"],
                },
                {
                    "host": r"api\.openai\.com",
                    "paths": [r"/v1/private$"],
                    "methods": ["POST"],
                    "injection_policy": [{
                        "inject_header": "Authorization",
                        "inject_prefix": "Bearer ",
                        "replace_token": "dummy-auth",
                        "test_value": "auth-token",
                    }],
                },
            ],
        ), FakeResolver)

        public_flow = make_flow({
            "Cookie": "dummy-cookie",
            "Authorization": "Bearer dummy-auth",
        }, path="/v1/public", method="GET")
        private_flow = make_flow({
            "Cookie": "dummy-cookie",
            "Authorization": "Bearer dummy-auth",
        }, path="/v1/private", method="POST")

        self.assertEqual(inject_matching_providers([provider], public_flow), ["openai"])
        self.assertEqual(public_flow.request.headers["Cookie"], "cookie-token")
        self.assertEqual(public_flow.request.headers["Authorization"], "Bearer dummy-auth")

        self.assertEqual(inject_matching_providers([provider], private_flow), ["openai"])
        self.assertEqual(private_flow.request.headers["Cookie"], "cookie-token")
        self.assertEqual(private_flow.request.headers["Authorization"], "Bearer auth-token")

    def test_rule_scoped_injection_selects_path_and_method(self):
        provider = Provider(provider_config(
            injection_policy=[],
            request_policy=[
                {
                    "host": r"api\.openai\.com",
                    "paths": [r"/v1/responses$"],
                    "methods": ["GET"],
                    "injection_policy": [{
                        "inject_header": "x-route-token",
                        "replace_token": "dummy",
                        "test_value": "responses-get",
                    }],
                },
                {
                    "host": r"api\.openai\.com",
                    "paths": [r"/v1/responses$"],
                    "methods": ["POST"],
                    "injection_policy": [{
                        "inject_header": "x-route-token",
                        "replace_token": "dummy",
                        "test_value": "responses-post",
                    }],
                },
                {
                    "host": r"api\.openai\.com",
                    "paths": [r"/v1/embeddings$"],
                    "methods": ["POST"],
                    "injection_policy": [{
                        "inject_header": "x-route-token",
                        "replace_token": "dummy",
                        "test_value": "embeddings-post",
                    }],
                },
            ],
        ), FakeResolver)

        cases = [
            ("/v1/responses", "GET", "responses-get"),
            ("/v1/responses", "POST", "responses-post"),
            ("/v1/embeddings", "POST", "embeddings-post"),
        ]
        for path, method, expected in cases:
            with self.subTest(path=path, method=method):
                flow = make_flow({"x-route-token": "dummy"}, path=path, method=method)
                self.assertEqual(inject_matching_providers([provider], flow), ["openai"])
                self.assertEqual(flow.request.headers["x-route-token"], expected)

    def test_last_matching_request_rule_for_header_wins(self):
        provider = Provider(provider_config(
            injection_policy=[],
            request_policy=[
                {
                    "host": r"api\.openai\.com",
                    "paths": [r"/v1/responses$"],
                    "methods": ["POST"],
                    "injection_policy": [{
                        "inject_header": "x-route-token",
                        "replace_token": "dummy",
                        "test_value": "exact-rule",
                    }],
                },
                {
                    "host": r"api\.openai\.com",
                    "paths": [r"/v1/.*"],
                    "methods": ["POST"],
                    "injection_policy": [{
                        "inject_header": "x-route-token",
                        "replace_token": "dummy",
                        "test_value": "later-rule",
                    }],
                },
            ],
        ), FakeResolver)
        flow = make_flow({"x-route-token": "dummy"})

        self.assertEqual(inject_matching_providers([provider], flow), ["openai"])
        self.assertEqual(flow.request.headers["x-route-token"], "later-rule")


if __name__ == "__main__":
    unittest.main()
