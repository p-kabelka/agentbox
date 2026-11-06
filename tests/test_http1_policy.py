"""Real, socket-free ALPN negotiation using stock mitmproxy and public hooks.

Run in a subprocess so unit tests' lightweight mitmproxy doubles cannot interfere.
"""

import importlib.metadata
import subprocess
import sys
import unittest
from pathlib import Path


class HTTP1PolicyIntegrationTest(unittest.TestCase):
    def test_upstream_alpn_policy_with_stock_mitmproxy(self):
        try:
            importlib.metadata.version("mitmproxy")
        except importlib.metadata.PackageNotFoundError:
            self.skipTest("Install proxy dependencies to run the TLS negotiation regression")
        result = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--worker"],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ALPN policy checks passed", result.stdout)


def tls_worker():
    import datetime
    import tempfile
    from types import SimpleNamespace
    from unittest.mock import mock_open, patch

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from OpenSSL import SSL, crypto

    from mitmproxy import connection, ctx, tls
    from mitmproxy.addons.tlsconfig import TlsConfig
    from mitmproxy.options import Options
    from mitmproxy.proxy.context import Context
    from mitmproxy.proxy.server_hooks import ServerConnectionHookData

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "proxy" / "addons"))
    with patch("builtins.open", mock_open(read_data="providers: []\n")):
        import addon

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "api.example")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                   .public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - datetime.timedelta(minutes=1))
                   .not_valid_after(now + datetime.timedelta(days=1))
                   .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                   .add_extension(x509.SubjectAlternativeName([x509.DNSName("api.example")]), critical=False)
                   .sign(key, hashes.SHA256()))

    def negotiate(upstream):
        backend_context = SSL.Context(SSL.TLS_SERVER_METHOD)
        backend_context.use_certificate(crypto.X509.from_cryptography(certificate))
        backend_context.use_privatekey(crypto.PKey.from_cryptography_key(key))
        backend_context.set_alpn_select_callback(
            lambda conn, offers: b"h2" if b"h2" in offers else b"http/1.1")
        backend = SSL.Connection(backend_context)
        backend.set_accept_state()
        connections = (upstream, backend)
        completed = set()
        for _ in range(100):
            for index, conn in enumerate(connections):
                if index not in completed:
                    try:
                        conn.do_handshake()
                        completed.add(index)
                    except (SSL.WantReadError, SSL.WantWriteError):
                        pass
            for source, destination in ((upstream, backend), (backend, upstream)):
                while True:
                    try:
                        destination.bio_write(source.bio_read(65536))
                    except SSL.WantReadError:
                        break
            if len(completed) == 2:
                assert upstream.get_alpn_proto_negotiated() == backend.get_alpn_proto_negotiated()
                return upstream.get_alpn_proto_negotiated()
        raise AssertionError("In-memory TLS handshake did not complete")

    forced = {"host": r"api\.example", "port": 8443}
    provider = {"name": "api", "enabled": True, "credential_type": "static",
                "injection_policy": [], "request_policy": [forced]}
    cases = [
        ({"force_http1_hosts": [forced]}, 8443, b"http/1.1"),
        ({"force_http1_hosts": [forced], "extra_request_policy": [forced]}, 8443, b"http/1.1"),
        ({"force_http1_hosts": [forced], "providers": [provider]}, 8443, b"http/1.1"),
        ({"force_http1_hosts": [{**forced, "port": "844[34]"}]}, 8444, b"http/1.1"),
        ({"force_http1_hosts": [{"host": "api.example"}]}, 443, b"http/1.1"),
        ({"extra_request_policy": [{"host": "api.example", "port": 8443}]}, 8443, b"h2"),
        ({"force_http1_hosts": [forced]}, 8444, b"h2"),
        ({"force_http1_hosts": [{**forced, "host": "other.example"}]}, 8443, b"h2"),
        ({"force_http1_hosts": []}, 8443, b"h2"),
    ]
    with tempfile.TemporaryDirectory() as directory:
        ca = Path(directory) / "ca.pem"
        ca.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        for config, port, expected in cases:
            options = Options(http2=True, ssl_insecure=False, ssl_verify_upstream_trusted_ca=str(ca))
            client = connection.Client(peername=("127.0.0.1", 12345), sockname=("127.0.0.1", 8080),
                                       alpn=b"h2", alpn_offers=(b"h2", b"http/1.1"))
            context = Context(client, options)
            context.server = connection.Server(address=("api.example", port), sni="api.example", tls=True)
            policy = addon.AgentboxAddon.__new__(addon.AgentboxAddon)
            policy._apply_candidate(policy._build_candidate(config))
            policy.server_connect(ServerConnectionHookData(context.server, client))
            start = tls.TlsData(context.server, context)
            tls_config = TlsConfig()
            tls_config.load(SimpleNamespace(add_option=options.add_option))
            with patch.object(ctx, "options", options, create=True):
                tls_config.tls_start_server(start)
            assert negotiate(start.ssl_conn) == expected, (config, port, expected)
            assert client.alpn == b"h2"
            assert options.http2 is True and options.ssl_insecure is False
    print(f"ALPN policy checks passed: {len(cases)} verified TLS handshakes")


if __name__ == "__main__":
    if "--worker" in sys.argv:
        tls_worker()
    else:
        unittest.main()
