"""Real mitmproxy/HTTP streaming regression, optional with proxy dependencies installed.

Runs in a subprocess so the lightweight unit tests' mitmproxy doubles cannot interfere.
"""

import asyncio
import importlib.metadata
import subprocess
import sys
import unittest
from pathlib import Path


class StreamingIntegrationTest(unittest.TestCase):
    def test_active_stream_survives_full_reload_and_later_request_rotates(self):
        try:
            importlib.metadata.version("mitmproxy")
        except importlib.metadata.PackageNotFoundError:
            self.skipTest("Install proxy dependencies to run the real streaming regression")
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker"],
                                capture_output=True, text=True, timeout=30)
        if result.returncode == 77:
            self.skipTest("Environment cannot connect to its own ephemeral loopback listeners")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("integration-old-secret", result.stdout + result.stderr)
        self.assertNotIn("integration-new-secret", result.stdout + result.stderr)
        self.assertIn("[redacted]", result.stdout)


async def streaming_worker():
    import builtins
    import importlib.util
    import io
    import json
    import socket
    import tempfile
    from unittest.mock import patch

    import yaml
    from mitmproxy.options import Options
    from mitmproxy.tools.dump import DumpMaster

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "proxy" / "addons"))
    from secret_contract import fingerprint, injection_secret_name, installation_frame
    import resolvers

    async def probe(reader, writer):
        writer.write(b"pong")
        await writer.drain()
        writer.close()
    async with await asyncio.start_server(probe, "127.0.0.1", 0) as server:
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(
                "127.0.0.1", server.sockets[0].getsockname()[1]), 1)
            try:
                if await asyncio.wait_for(reader.readexactly(4), 1) != b"pong":
                    sys.exit(77)
            finally:
                writer.close()
                await writer.wait_closed()
        except (OSError, TimeoutError, asyncio.IncompleteReadError):
            sys.exit(77)

    def load_module(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    release = asyncio.Event()
    received = []

    async def upstream(reader, writer):
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
            lines = raw.decode().split("\r\n")
            headers = {k.lower(): v.strip() for k, v in
                       (line.split(":", 1) for line in lines[1:] if ":" in line)}
            received.append(headers.get("authorization"))
            if "/stream" in lines[0]:
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                             b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
                             b"d\r\ndata: first\n\n\r\n")
                await writer.drain()
                await release.wait()
                writer.write(b"e\r\ndata: second\n\n\r\n0\r\n\r\n")
            else:
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async with await asyncio.start_server(upstream, "127.0.0.1", 0) as upstream_server:
        upstream_port = upstream_server.sockets[0].getsockname()[1]
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            store = directory / "secrets"
            store.mkdir()
            cfg = {"providers": [{"name": "streaming", "enabled": True, "credential_type": "static",
                                  "api_key_file": "/host/key", "inject_prefix": "Bearer ",
                                  "request_policy": [{"host": r"127\.0\.0\.1", "port": upstream_port}]}]}
            config_path = directory / "proxy.yaml"
            config_path.write_text(yaml.safe_dump(cfg))
            manager = load_module("stream_secret_manager", root / "proxy" / "manage_secrets.py")
            manager.SECRET_DIR = store
            target = injection_secret_name("/host/key")
            manager.install(target, io.BytesIO(installation_frame(b"integration-old-secret")))
            real_open = builtins.open
            def config_open(path, *args, **kwargs):
                return real_open(config_path if path == "/config/proxy.yaml" else path, *args, **kwargs)
            with patch.object(resolvers, "_secret_path", side_effect=lambda source:
                              str(store / injection_secret_name(source))):
                with patch.object(builtins, "open", side_effect=config_open):
                    module = load_module("stream_addon", root / "proxy" / "addons" / "addon.py")
                module._CONFIG_PATH = str(config_path)
                module._RELOAD_PORT = 0
                addon = module.addons[0]
                with socket.socket() as reservation:
                    reservation.bind(("127.0.0.1", 0))
                    proxy_port = reservation.getsockname()[1]
                master = DumpMaster(Options(listen_host="127.0.0.1", listen_port=proxy_port,
                                            confdir=str(directory / "certs")),
                                    with_termlog=False, with_dumper=False)
                master.options.update(connection_strategy="lazy", block_global=False)
                master.addons.add(addon)
                task = asyncio.create_task(master.run())
                try:
                    for _ in range(200):
                        if hasattr(addon, "_reload_server"):
                            break
                        if task.done():
                            await task
                            raise AssertionError("Proxy exited during startup")
                        await asyncio.sleep(0.01)
                    reload_port = addon._reload_server.sockets[0].getsockname()[1]

                    async def request(path):
                        reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
                        writer.write(f"GET http://127.0.0.1:{upstream_port}{path} HTTP/1.1\r\n"
                                     f"Host: 127.0.0.1:{upstream_port}\r\nConnection: close\r\n\r\n".encode())
                        await writer.drain()
                        header = await reader.readuntil(b"\r\n\r\n")
                        return int(header.split()[1]), reader, writer

                    async def reload():
                        reader, writer = await asyncio.open_connection("127.0.0.1", reload_port)
                        writer.write(f"GET /reload/providers HTTP/1.1\r\n"
                                     f"X-Agentbox-Config-Fingerprint: {fingerprint(cfg)}\r\n\r\n".encode())
                        await writer.drain()
                        response = await reader.read()
                        writer.close()
                        await writer.wait_closed()
                        header, body = response.split(b"\r\n\r\n")
                        assert int(header.split()[1]) == 200
                        assert json.loads(body)["fingerprint"] == fingerprint(cfg)

                    status, stream, stream_writer = await request("/stream")
                    assert status == 200, (status, await stream.read())
                    await stream.readuntil(b"data: first\n\n")
                    # Reset/install exercise the real helper core while the stream is in flight.
                    manager.reset()
                    manager.install(target, io.BytesIO(installation_frame(b"integration-new-secret")))
                    status, before, before_writer = await request("/before-reload")
                    assert status == 200
                    await before.read()
                    before_writer.close()
                    await reload()
                    status, later, later_writer = await request("/after-reload")
                    assert status == 200
                    await later.read()
                    later_writer.close()
                    assert received == ["Bearer integration-old-secret", "Bearer integration-old-secret",
                                        "Bearer integration-new-secret"]
                    release.set()
                    assert b"data: second\n\n" in await stream.read()
                    stream_writer.close()
                    await stream_writer.wait_closed()
                    manager.reset()
                    await reload()
                    status, unavailable, unavailable_writer = await request("/unavailable")
                    assert status == 503
                    await unavailable.read()
                    unavailable_writer.close()
                    assert len(received) == 3  # no upstream contact on 503
                finally:
                    release.set()
                    master.shutdown()
                    await task
                    if hasattr(addon, "_reload_server"):
                        addon._reload_server.close()
                        await addon._reload_server.wait_closed()


if __name__ == "__main__":
    if "--worker" in sys.argv:
        asyncio.run(asyncio.wait_for(streaming_worker(), 20))
    else:
        unittest.main()
