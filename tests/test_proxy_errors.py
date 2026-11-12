"""Socket-free proxy error regression using stock mitmproxy's HTTP state machine.

Runs in a subprocess to avoid the unit tests' lightweight mitmproxy doubles.
"""

import importlib.metadata
import subprocess
import sys
import unittest
from pathlib import Path


class ProxyErrorIntegrationTest(unittest.TestCase):
    def test_connection_failure_emits_redacted_json_error(self):
        try:
            importlib.metadata.version("mitmproxy")
        except importlib.metadata.PackageNotFoundError:
            self.skipTest("Install proxy dependencies to run the proxy error regression")
        result = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--worker"],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("proxy-error-test-secret", result.stdout + result.stderr)
        self.assertIn("Proxy error logging checks passed", result.stdout)


def error_worker():
    import io
    import json
    from unittest.mock import mock_open, patch

    from mitmproxy import http
    from mitmproxy.addons.proxyserver import Proxyserver
    from mitmproxy.proxy import commands, events
    from mitmproxy.proxy.context import Context
    from mitmproxy.proxy.layers.http import (GetHttpConnection, HTTPMode, HttpLayer,
                                            HttpStream, RequestEndOfMessage, RequestHeaders)
    from mitmproxy.test import taddons, tflow

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "proxy" / "addons"))
    config = r"""providers:
  - name: logging-test
    enabled: true
    credential_type: static
    api_key_env: PROXY_LOGGING_TEST_KEY
    inject_prefix: 'Bearer '
    request_policy:
      - host: 'dns\.example'
"""
    with (patch("builtins.open", mock_open(read_data=config)),
          patch.dict("os.environ", PROXY_LOGGING_TEST_KEY="proxy-error-test-secret")):
        import addon

    for message in ("[Errno -3] Temporary failure in name resolution", "Connection refused"):
        output = io.StringIO()
        with taddons.context(Proxyserver(), addon.addons[0]) as test_context:
            test_context.options.update(connection_strategy="lazy")
            context = Context(tflow.tclient_conn(), test_context.options)
            HttpLayer(context, HTTPMode.regular)
            stream = HttpStream(context, 1)

            def dispatch(event):
                # Drive the real state machine, replacing only the network result.
                generator = stream._handle_event(event)
                reply = None
                while True:
                    try:
                        command = generator.send(reply)
                    except StopIteration:
                        return
                    reply = None
                    if isinstance(command, commands.StartHook):
                        test_context.master.addons.trigger(command)
                    elif isinstance(command, GetHttpConnection):
                        assert command.address == ("dns.example", 443)
                        reply = (None, message)

            with patch.object(addon._handler, "stream", output):
                request = http.Request.make("POST", "https://dns.example/private", b"payload")
                dispatch(events.Start())
                dispatch(RequestHeaders(1, request, end_stream=True))
                dispatch(RequestEndOfMessage(1))

        entries = [json.loads(line) for line in output.getvalue().splitlines()]
        assert len(entries) == 1, entries
        entry = entries[0]
        assert entry["source"] == "proxy" and entry["level"] == "error", entry
        assert entry["error"] == message and entry["status"] is None, entry
        assert entry["method"] == "POST" and entry["url"] == request.pretty_url, entry
        assert isinstance(entry["duration_ms"], int) and entry["duration_ms"] >= 0, entry
        headers = {key.lower(): value for key, value in entry["req_headers"].items()}
        assert headers["authorization"] == "[redacted]", entry
        assert entry["provider"] == "logging-test", entry
        assert "proxy-error-test-secret" not in output.getvalue()
        print(output.getvalue(), end="")
    print("Proxy error logging checks passed")


if __name__ == "__main__":
    if "--worker" in sys.argv:
        error_worker()
    else:
        unittest.main()
