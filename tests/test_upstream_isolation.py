import asyncio
import importlib.util
import json
import socket
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("upstream_guard", ROOT / "proxy/addons/upstream_isolation.py")
upstream = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upstream)


class UpstreamIsolationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.policy = Path(temporary.name) / "policy.json"
        self.policy.write_text(json.dumps({"blocked": ["10.89.0.0/24", "9.9.9.9"]}))
        p = patch.object(upstream, "POLICY", self.policy)
        p.start()
        self.addCleanup(p.stop)
        self.guard = upstream.UpstreamIsolation()
        self.guard.enabled = True

    async def connect(self, addresses):
        server = types.SimpleNamespace(address=("allowed.example", 8081), sni=None, error=None)
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 8081)) for address in addresses]
        with patch.object(asyncio.get_running_loop(), "getaddrinfo", new=AsyncMock(return_value=answers)):
            await self.guard.server_connect(types.SimpleNamespace(server=server))
        return server

    async def test_resolved_host_control_plane_and_loopback_addresses_are_denied(self):
        for address in ("10.89.0.1", "10.89.0.3", "10.89.0.99", "9.9.9.9", "127.0.0.1",
                        "::1", "::ffff:127.0.0.1", "::ffff:10.89.0.1", "169.254.1.2", "fe80::1"):
            with self.subTest(address=address):
                server = await self.connect([address])
                self.assertIn("blocked", server.error)
                self.assertEqual(server.address, ("allowed.example", 8081))
        self.assertIsNotNone((await self.connect(["8.8.8.8", "10.89.0.1"])).error)

    async def test_safe_address_is_pinned_and_original_tls_name_is_preserved(self):
        for address in ("8.8.8.8", "2606:4700:4700::1111"):
            with self.subTest(address=address):
                server = await self.connect([address])
                self.assertIsNone(server.error)
                self.assertEqual(server.address, (address, 8081))
                self.assertEqual(server.sni, "allowed.example")
        server = await self.connect(["2606:4700:4700::1111", "8.8.8.8"])
        self.assertEqual(server.address, ("8.8.8.8", 8081))

    async def test_private_upstreams_require_explicit_ips_and_never_override_host_blocks(self):
        self.assertIsNotNone((await self.connect(["10.50.1.42"])).error)
        self.policy.write_text(json.dumps({"blocked": ["10.89.0.0/24"],
                                          "private_upstreams": ["10.50.1.42", "10.89.0.3", "127.0.0.1"]}))
        self.assertIsNone((await self.connect(["10.50.1.42"])).error)
        self.assertIsNotNone((await self.connect(["10.89.0.3"])).error)
        self.assertIsNotNone((await self.connect(["127.0.0.1"])).error)

    async def test_missing_invalid_policy_and_dns_failure_are_fail_closed(self):
        self.policy.unlink()
        self.assertIsNotNone((await self.connect(["8.8.8.8"])).error)
        for invalid in ("not json", "{}", "[]", "[true]", '{"blocked":["not an address"]}'):
            self.policy.write_text(invalid)
            self.assertIsNotNone((await self.connect(["8.8.8.8"])).error)
        self.policy.write_text('{"blocked":["10.89.0.0/24"]}')
        self.assertIsNotNone((await self.connect([])).error)
        server = types.SimpleNamespace(address=("allowed.example", 443), sni=None, error=None)
        with patch.object(asyncio.get_running_loop(), "getaddrinfo",
                          new=AsyncMock(side_effect=socket.gaierror("failed"))):
            await self.guard.server_connect(types.SimpleNamespace(server=server))
        self.assertIsNotNone(server.error)
