import asyncio
import importlib.util
import sys
import types
import unittest
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

yaml = types.ModuleType("yaml")
yaml.safe_load = lambda stream: {"providers": [], "logging": {}}
sys.modules["yaml"] = yaml

spec = importlib.util.spec_from_file_location(
    "agentbox_addon_test", ROOT / "proxy" / "addons" / "addon.py"
)
addon_module = importlib.util.module_from_spec(spec)
with patch("builtins.open", mock_open(read_data="")), patch("logging.Logger.info"):
    spec.loader.exec_module(addon_module)


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
        self.addon._cfg = addon_module._Config(allowed_rules=[], providers=[])
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
            async def read(self, size):
                return b""

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


if __name__ == "__main__":
    unittest.main()
