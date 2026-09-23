import asyncio
import importlib.util
import json
import sys
import tempfile
import threading
import time
import types
import unittest
import yaml
from pathlib import Path
from unittest.mock import mock_open, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "proxy" / "addons"))


class FakeResponse:
    @staticmethod
    def make(status_code, content, headers):
        return types.SimpleNamespace(
            status_code=status_code,
            content=content,
            headers=headers,
        )


mitmproxy = types.ModuleType("mitmproxy")
mitmproxy.http = types.SimpleNamespace(HTTPFlow=object, Response=FakeResponse)
sys.modules["mitmproxy"] = mitmproxy

spec = importlib.util.spec_from_file_location(
    "agentbox_addon_test", ROOT / "proxy" / "addons" / "addon.py"
)
addon_module = importlib.util.module_from_spec(spec)
with patch("builtins.open", mock_open(read_data="providers: []\nlogging: {}")), patch("logging.Logger.info"):
    spec.loader.exec_module(addon_module)

from resolvers import CursorApiKeyResolver
from secret_contract import fingerprint, injection_secret_name
from test_provider import Headers


def make_flow(body="denied body"):
    request = types.SimpleNamespace(
        pretty_host="denied.example.com",
        pretty_url="https://denied.example.com/private?value=1",
        port=443,
        path="/private?value=1",
        method="POST",
        headers={"Content-Type": "text/plain", "X-Test": "denied"},
        get_text=lambda strict=False: body,
    )
    return types.SimpleNamespace(request=request, response=None, metadata={})


class DeniedRequestLoggingTest(unittest.TestCase):
    def setUp(self):
        self.addon = addon_module.addons[0]
        self.addon._cfg = addon_module.AgentboxAddon._build_candidate({"providers": []})
        addon_module._log_req_hdr = True

    def test_logs_headers_immediately_when_body_logging_is_disabled(self):
        addon_module._log_bodies = False
        flow = make_flow()

        with patch.object(addon_module.log, "info") as log_info:
            self.addon.requestheaders(flow)

        self.assertEqual(flow.response.status_code, 403)
        self.assertNotIn("agentbox_block_pending", flow.metadata)
        entry = log_info.call_args.args[0]
        self.assertEqual(entry["req_headers"], flow.request.headers)
        self.assertNotIn("req_body", entry)

    def test_defers_denial_and_logs_body_after_request_is_read(self):
        addon_module._log_bodies = True
        flow = make_flow()

        with patch.object(addon_module.log, "info") as log_info:
            self.addon.requestheaders(flow)
            self.assertIsNone(flow.response)
            self.assertFalse(flow.request.stream)
            self.assertTrue(flow.metadata["agentbox_block_pending"])
            log_info.assert_not_called()

            self.addon.request(flow)
            self.addon.response(flow)

        self.assertEqual(flow.response.status_code, 403)
        self.assertNotIn("agentbox_block_pending", flow.metadata)
        log_info.assert_called_once()
        entry = log_info.call_args.args[0]
        self.assertEqual(entry["req_headers"], flow.request.headers)
        self.assertEqual(entry["req_body"], "denied body")

    def test_logs_failed_deferred_request_once_from_error_hook(self):
        addon_module._log_bodies = True
        flow = make_flow("partial body")

        with patch.object(addon_module.log, "info") as log_info:
            self.addon.requestheaders(flow)
            self.addon.error(flow)

        self.assertIsNone(flow.response)
        self.assertNotIn("agentbox_block_pending", flow.metadata)
        log_info.assert_called_once()
        entry = log_info.call_args.args[0]
        self.assertIsNone(entry["status"])
        self.assertEqual(entry["req_body"], "partial body")
        self.assertIn("request body failed", entry["blocked_reason"])

    def test_reload_updates_logging_flags(self):
        class Reader:
            async def readuntil(self, separator):
                return b"GET /reload HTTP/1.1\r\n\r\n"

        class Writer:
            def write(self, data):
                self.data = data

            async def drain(self):
                pass

            def close(self):
                pass

        config = {
            "providers": [],
            "logging": {
                "log_request_headers": False,
                "log_response_headers": True,
                "log_bodies": True,
            },
        }

        with (
            patch.object(addon_module, "_read_config", return_value=config),
            patch.object(addon_module.log, "info"),
        ):
            asyncio.run(self.addon._handle_reload_conn(Reader(), Writer()))

        self.assertFalse(addon_module._log_req_hdr)
        self.assertTrue(addon_module._log_resp_hdr)
        self.assertTrue(addon_module._log_bodies)


class ReloadTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Path(self.temp.name)
        self.cfg = {"providers": [{
            "name": "test", "enabled": True, "credential_type": "static",
            "api_key_file": "/host/key", "inject_prefix": "Bearer ",
            "request_policy": [{"host": r"api\.example", "paths": ["/private"]}],
        }], "extra_request_policy": [{"host": "public.example"}]}
        self.secret = self.store / injection_secret_name("/host/key")
        self.secret.write_text("old-key")
        self.config_patch = patch.object(addon_module, "_read_config", side_effect=lambda: self.cfg)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.path_patch = patch("resolvers._secret_path", side_effect=lambda source:
                               str(self.store / injection_secret_name(source)))
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        self.addon = addon_module.AgentboxAddon()

    async def reload(self, path="/reload/providers", expected=None):
        if expected is None:
            expected = fingerprint(self.cfg)
        reader = asyncio.StreamReader()
        reader.feed_data(f"GET {path} HTTP/1.1\r\nX-Agentbox-Config-Fingerprint: {expected}\r\n\r\n".encode())
        reader.feed_eof()
        class Writer:
            def write(self, data):
                self.data = data
            async def drain(self):
                pass
            def close(self):
                pass
        writer = Writer()
        await self.addon._handle_reload_conn(reader, writer)
        header, body = writer.data.split(b"\r\n\r\n")
        return int(header.split()[1]), json.loads(body)

    def flow(self, host="api.example", path="/private", headers=None):
        request = types.SimpleNamespace(pretty_host=host, port=443, path=path, method="POST",
                                        pretty_url=f"https://{host}{path}", timestamp_start=time.time(),
                                        headers=Headers(headers or {}), get_text=lambda **kw: "")
        return types.SimpleNamespace(request=request, response=None, metadata={})

    async def test_static_rotation_is_explicit_stream_survives_and_headers_are_redacted(self):
        streaming = self.flow(headers={"content-type": "application/connect+json"})
        self.addon.requestheaders(streaming)
        streaming.response = types.SimpleNamespace(headers=Headers({"content-type": "text/event-stream"}),
                                                    status_code=200, timestamp_end=None)
        self.addon.responseheaders(streaming)
        self.assertTrue(streaming.response.stream)
        replacement = self.store / "replacement"
        replacement.write_text("new-key")
        replacement.replace(self.secret)
        # Even a fast reload cannot rotate files, and requests perform no file I/O.
        old_provider = self.addon._cfg.providers[0]
        status, _ = await self.reload("/reload")
        self.assertEqual(status, 200)
        self.assertIs(self.addon._cfg.providers[0], old_provider)
        with patch("resolvers.open", side_effect=AssertionError("request-path file read"), create=True):
            before = self.flow()
            self.addon.requestheaders(before)
        self.assertEqual(before.request.headers["authorization"], "Bearer old-key")
        status, body = await self.reload()
        self.assertEqual(status, 200)
        self.assertEqual(body["fingerprint"], fingerprint(self.cfg))
        later = self.flow()
        self.addon.requestheaders(later)
        self.assertEqual(later.request.headers["authorization"], "Bearer new-key")
        self.assertEqual(streaming.request.headers["authorization"], "Bearer old-key")
        self.assertTrue(streaming.response.stream)
        with patch.object(addon_module.log, "info") as logger:
            self.addon.response(streaming)
        self.assertNotIn("old-key", repr(logger.call_args))
        self.assertEqual(logger.call_args.args[0]["req_headers"]["authorization"], "[redacted]")

    async def test_cursor_rotation_rebuilds_empty_exchange_cache(self):
        self.cfg["providers"][0]["credential_type"] = "cursor_api_key"
        def exchange(resolver):
            resolver._token = "exchanged-" + resolver._api_key
            resolver._token_exp = time.time() + 3600
        with patch.object(CursorApiKeyResolver, "_exchange", exchange):
            self.assertEqual((await self.reload())[0], 200)
            provider = self.addon._cfg.providers[0]
            first = self.flow()
            self.addon.requestheaders(first)
            self.assertEqual(first.request.headers["authorization"], "Bearer exchanged-old-key")
            self.secret.write_text("rotated-key")
            before = self.flow()
            self.addon.requestheaders(before)
            self.assertEqual(before.request.headers["authorization"], "Bearer exchanged-old-key")
            self.assertEqual((await self.reload())[0], 200)
            new = self.addon._cfg.providers[0]
            self.assertIsNot(provider, new)
            self.assertIsNone(new._injection_policies[0].resolver._token)
            later = self.flow()
            self.addon.requestheaders(later)
            self.assertEqual(later.request.headers["authorization"], "Bearer exchanged-rotated-key")

    async def test_fast_provider_conflict_and_exact_full_fingerprint_leave_entire_state(self):
        previous = self.addon._cfg
        self.cfg = {**self.cfg, "providers": [], "logging": {"log_bodies": True},
                    "extra_request_policy": [{"host": "new.example"}]}
        self.assertEqual((await self.reload("/reload"))[0], 409)
        self.assertIs(self.addon._cfg, previous)
        self.assertEqual((await self.reload(expected=previous.fingerprint))[0], 409)
        self.assertIs(self.addon._cfg, previous)
        self.assertEqual((await self.reload(expected=""))[0], 400)
        self.assertEqual((await self.reload(expected=fingerprint(self.cfg).upper()))[0], 400)
        self.assertFalse(addon_module._log_bodies)
        self.assertEqual((await self.reload())[0], 200)
        self.assertTrue(addon_module._log_bodies)

    async def test_structural_failure_and_invalid_yaml_roll_back_every_runtime_field(self):
        previous = self.addon._cfg
        for bad in ({"credential_type": "unknown"}, {"injection_policy": "wrong"},
                    {"injection_policy": [{"inject_header": "bad\nheader"}]}):
            config = {**self.cfg, "providers": [{**self.cfg["providers"][0], **bad}],
                      "logging": {"log_bodies": True}}
            with patch.object(addon_module, "_read_config", return_value=config):
                self.assertEqual((await self.reload(expected=fingerprint(config)))[0], 500)
            self.assertIs(self.addon._cfg, previous)
            self.assertFalse(addon_module._log_bodies)
        # Failure after provider construction must also leave logging/providers/rules alone.
        broken = {**self.cfg, "extra_request_policy": [None]}
        with patch.object(addon_module, "_read_config", return_value=broken):
            self.assertEqual((await self.reload(expected=fingerprint(broken)))[0], 500)
        with patch.object(addon_module, "_read_config", side_effect=yaml.YAMLError("sensitive-context")):
            status, body = await self.reload()
            self.assertEqual(status, 500)
            self.assertNotIn("sensitive-context", repr(body))
        self.assertIs(self.addon._cfg, previous)

    async def test_unsynchronized_startup_only_gates_matching_policies_and_env_fallback_works(self):
        self.secret.unlink()
        self.cfg["providers"].append({"name": "healthy", "enabled": True, "credential_type": "static",
                                     "api_key_env": "HEALTHY_TEST_KEY",
                                     "request_policy": [{"host": "healthy.example"}]})
        with patch.dict("os.environ", HEALTHY_TEST_KEY="healthy-value"):
            self.addon = addon_module.AgentboxAddon()  # raw/automatic startup: empty tmpfs
            missing = self.flow()
            self.addon.requestheaders(missing)
            self.assertEqual(missing.response.status_code, 503)
            self.assertNotIn("authorization", missing.request.headers)
            for host in ("healthy.example", "public.example"):
                flow = self.flow(host=host)
                self.addon.requestheaders(flow)
                self.assertIsNone(flow.response)
            denied = self.flow(host="denied.example")
            self.addon.requestheaders(denied)
            self.assertEqual(denied.response.status_code, 403)
            self.cfg["providers"][0]["api_key_env"] = "HEALTHY_TEST_KEY"
            status, body = await self.reload()
            self.assertEqual((status, body["unavailable"]), (200, []))
            fallback = self.flow()
            self.addon.requestheaders(fallback)
            self.assertEqual(fallback.request.headers["authorization"], "Bearer healthy-value")

    async def test_invalid_rule_secret_never_creates_unavailable_runtime_policy(self):
        self.cfg["providers"][0].update(injection_policy=[], request_policy=[
            {"host": "[", "injection_policy": [{"api_key_file": "/host/absent"}]},
            {"host": "api.example"},
        ])
        status, body = await self.reload()
        self.assertEqual((status, body["unavailable"]), (200, []))
        flow = self.flow()
        self.addon.requestheaders(flow)
        self.assertIsNone(flow.response)

    async def test_reload_operations_serialize_snapshot_and_candidate_build(self):
        entered, release = threading.Event(), threading.Event()
        build = self.addon._build_candidate
        reads = []
        def blocked_build(*args):
            entered.set()
            if not release.wait(3):
                raise TimeoutError
            return build(*args)
        def read():
            reads.append(True)
            return self.cfg
        with (patch.object(self.addon, "_build_candidate", side_effect=blocked_build),
              patch.object(addon_module, "_read_config", side_effect=read)):
            first = asyncio.create_task(self.reload())
            await asyncio.to_thread(entered.wait, 2)
            second = asyncio.create_task(self.reload("/reload"))
            await asyncio.sleep(0.05)
            self.assertEqual(len(reads), 1)
            release.set()
            self.assertEqual([r[0] for r in await asyncio.gather(first, second)], [200, 200])
            self.assertEqual(len(reads), 2)


if __name__ == "__main__":
    unittest.main()
