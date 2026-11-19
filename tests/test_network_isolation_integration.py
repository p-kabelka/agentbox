"""Opt-in, real agent network isolation and passt integration tests.

Build first: AGENTBOX_IMAGE_TAG=git-sidecar-test agentbox build base network git proxy
Run: AGENTBOX_INTEGRATION=1 AGENTBOX_IMAGE_TAG=git-sidecar-test python3 -m unittest discover -s tests -p test_network_isolation_integration.py -v
On systems without KVM only, explicitly set AGENTBOX_TEST_RUNTIME=crun.
Set AGENTBOX_TEST_USE_PASST=1 to exercise krun's passt backend and IPv4 workaround.
"""

import hashlib
import http.client
import ipaddress
import json
import os
import socket
import subprocess
import sys
import unittest

import yaml

from integration_support import AgentboxIntegrationTest, ROOT


class NetworkIsolationIntegrationTest(AgentboxIntegrationTest):
    def test_session_local_tcp_works_without_reaching_host_loopback(self):
        self.initialize()
        with socket.socket() as host:
            host.bind(("127.0.0.1", 0))
            host.listen()
            host_port = host.getsockname()[1]
            script = f"""
import http.client, http.server, socket, threading
port = {host_port}
try:
    client = socket.create_connection(('127.0.0.1', port), timeout=2)
except OSError:
    pass
else:
    client.close()
    raise AssertionError('reached a host-only listener')

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'local')

# Use an explicit port as OpenCode's managed service does. Some TSI versions
# do not report the bound ephemeral port to the guest for bind(port=0).
with http.server.HTTPServer(('127.0.0.1', port), Handler) as server:
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    client = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    try:
        client.request('GET', '/')
        response = client.getresponse()
        assert response.status == 200 and response.read() == b'local'
    finally:
        client.close()
        server.shutdown()
        worker.join()
"""
            self.cli("start", "--session", self.session_id, "--", "python3", "-c", script)

    def start_waiting_agent(self):
        script = """
import http.client, os, socket, sys
from pathlib import Path
work = Path('/root/network-lifetime-work')
token = os.urandom(16).hex()
work.write_text(token)
client = http.client.HTTPConnection('proxy', 8080, timeout=5)
def check():
    assert work.read_text() == token, 'Working state lost'
    client.request('GET', 'http://not-allowed.invalid/')
    response = client.getresponse()
    assert response.status == 403, response.status
    response.read()
    assert client.sock is not None, 'Proxy closed the keepalive connection'
    for port in (9418, 8083):
        with socket.create_connection(('git', port), timeout=5):
            pass
    for port in (8081, 8082):
        try:
            with socket.create_connection(('proxy', port), timeout=1):
                raise AssertionError(('Management port reachable', port))
        except OSError:
            pass
check()
original_socket = client.sock
print('NETWORK_LIFETIME_READY', flush=True)
for command in sys.stdin:
    if command.strip() == 'exit':
        break
    reconnect = command.strip() == 'reconnect'
    if reconnect:
        client.close()
    check()
    if reconnect:
        original_socket = client.sock
    else:
        assert client.sock is original_socket, 'TCP connection replaced'
    print('NETWORK_LIFETIME_OK', flush=True)
"""
        process = subprocess.Popen([sys.executable, str(ROOT / "bin/agentbox"), "start",
                                    "--session", self.session_id, "--", "python3", "-c", script],
                                   env=self.env, cwd=self.project, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        def cleanup():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            process.stdin.close()
            process.stdout.close()

        self.addCleanup(cleanup)
        self.read_agent_output(process, b"NETWORK_LIFETIME_READY", 90)
        return process

    def check_waiting_agent(self, process, *, reconnect=False):
        self.assertIsNone(process.poll(), "Agent attachment exited")
        process.stdin.write(b"reconnect\n" if reconnect else b"check\n")
        process.stdin.flush()
        self.read_agent_output(process, b"NETWORK_LIFETIME_OK")

    def finish_waiting_agent(self, process):
        process.stdin.write(b"exit\n")
        process.stdin.flush()
        output, _ = process.communicate(timeout=30)
        self.assertEqual(process.returncode, 0, output.decode(errors="replace"))

    def test_network_policy_refresh_preserves_persistent_agent_and_connections(self):
        self.initialize("--persist")
        process = self.start_waiting_agent()
        cfg = self.config()
        agent = cfg["services"]["agent"]["container_name"]
        identity = ["podman", "inspect", "--format", "{{.Id}} {{.State.Pid}} {{.State.StartedAt}}", agent]
        original = self.run_command(identity).stdout
        for _ in range(2):
            self.cli("proxy", "reload", "--session", self.session_id)
            self.check_waiting_agent(process)
            containers = json.loads(self.cli("containers", "--session", self.session_id, "--json").stdout)
            self.assertEqual({c["name"] for c in containers},
                             {s["container_name"] for name, s in cfg["services"].items() if name != "guard"})
        self.assertEqual(self.run_command(identity).stdout, original)
        self.finish_waiting_agent(process)

    def test_sidecar_restarts_keep_addresses_for_the_running_agent(self):
        self.initialize()
        process = self.start_waiting_agent()
        holders = ("network", "proxy-network", "git-network")
        before = {name: self.inspect_service(name) for name in holders}
        metadata = self.session / "network-config/network.json"
        original_policy = json.loads(metadata.read_text())
        info = json.loads(self.cli("list", "--json").stdout)[0]
        port = int(info["url"].rsplit(":", 1)[1])

        def check_web_ui():
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                connection.request("GET", "/")
                connection.getresponse().read()
            finally:
                connection.close()

        check_web_ui()
        self.cli("proxy", "restart", "--session", self.session_id)
        check_web_ui()
        self.check_waiting_agent(process, reconnect=True)
        git = self.config()["services"]["git"]["container_name"]
        self.run_command(["podman", "restart", git])
        self.run_command(["podman", "exec", git, "python3", "-c", """
import socket, time
for attempt in range(50):
    try:
        for port in (9418, 8083):
            with socket.create_connection(('127.0.0.1', port), timeout=1):
                pass
        break
    except OSError:
        time.sleep(0.1)
else:
    raise AssertionError('Git listeners did not come back')
"""])
        self.check_waiting_agent(process)

        self.assertEqual(json.loads(metadata.read_text()), original_policy)
        for name in holders:
            after = self.inspect_service(name)
            original = before[name]
            self.assertEqual(after["Id"], original["Id"])
            self.assertEqual(after["State"]["StartedAt"], original["State"]["StartedAt"])
            self.assertEqual(after["NetworkSettings"]["Networks"], original["NetworkSettings"]["Networks"])
        self.finish_waiting_agent(process)

    def test_network_installer_failure_preserves_ephemeral_agent_and_policy(self):
        self.initialize()
        process = self.start_waiting_agent()
        original = {c["name"]: (c["id"], c["pid"]) for c in
                    json.loads(self.cli("containers", "--session", self.session_id, "--json").stdout)}
        self.assertEqual(len(original), 6)  # One agent, two sidecars, and three namespace holders.
        cfg = self.config()
        cfg["services"]["guard"]["entrypoint"] = ["/usr/bin/false"]
        (self.session / "compose.yaml").write_text(yaml.safe_dump(cfg))
        failed = self.cli("proxy", "reload", "--session", self.session_id, check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("cannot enforce agent network policy", failed.stderr)
        self.check_waiting_agent(process)
        del cfg["services"]["guard"]["entrypoint"]
        (self.session / "compose.yaml").write_text(yaml.safe_dump(cfg))
        self.cli("proxy", "reload", "--session", self.session_id)
        self.check_waiting_agent(process)
        current = {c["name"]: (c["id"], c["pid"]) for c in
                   json.loads(self.cli("containers", "--session", self.session_id, "--json").stdout)}
        self.assertEqual(current, original)
        self.finish_waiting_agent(process)

    def assert_second_start_rejected(self, *, persistent):
        self.initialize(*(["--persist"] if persistent else []))
        process = self.start_waiting_agent()
        before = json.loads(self.cli("containers", "--session", self.session_id, "--json").stdout)
        metadata = (self.session / "network-config/network.json").stat()
        rejected = self.cli("start", "--session", self.session_id, check=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("session already running", rejected.stderr)
        after = json.loads(self.cli("containers", "--session", self.session_id, "--json").stdout)
        self.assertEqual(after, before)
        self.assertEqual((self.session / "network-config/network.json").stat().st_ino, metadata.st_ino)
        self.check_waiting_agent(process)
        self.finish_waiting_agent(process)
        if not persistent:
            # Releasing ownership permits the next agent and a fresh passt instance.
            self.cli("start", "--session", self.session_id, "--", "true")

    def test_ephemeral_session_rejects_second_start_without_side_effects(self):
        self.assert_second_start_rejected(persistent=False)

    def test_persistent_session_rejects_second_start_without_side_effects(self):
        self.assert_second_start_rejected(persistent=True)

    def test_ephemeral_session_refuses_unattached_agent_until_stopped(self):
        self.initialize()
        process = self.start_waiting_agent()
        process.kill()  # Release the CLI lock without stopping its agent container.
        process.wait(timeout=10)
        rejected = self.cli("start", "--session", self.session_id, check=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("unattached agent", rejected.stderr)
        self.cli("stop", "--session", self.session_id)
        self.assertEqual(json.loads(self.cli("containers", "--session", self.session_id, "--json").stdout), [])
        self.cli("start", "--session", self.session_id, "--", "true")

    def test_network_holder_restart_filters_its_current_namespace_without_restarting_agent(self):
        self.initialize()
        process = self.start_waiting_agent()
        holder = self.config()["services"]["network"]["container_name"]
        self.run_command(["podman", "kill", holder])
        self.run_command(["podman", "start", holder])
        self.cli("proxy", "reload", "--session", self.session_id)
        # Probe the replacement namespace directly, without launching a second
        # agent. A reused installer in the old namespace would leave this open.
        endpoints = json.loads((self.session / "network-config/network.json").read_text())["endpoints"]
        self.run_command(["podman", "exec", holder, "python3", "-c", f"ENDPOINTS={endpoints!r}\n" + """
import socket
for service, port in (('proxy', 8080), ('git', 9418), ('git', 8083)):
    with socket.create_connection((ENDPOINTS[service], port), timeout=5):
        pass
for port in (8081, 8082, 9090):
    try:
        with socket.create_connection((ENDPOINTS['proxy'], port), timeout=1):
            raise AssertionError(('Management port reachable', port))
    except OSError:
        pass
"""])
        self.assertIsNone(process.poll(), "Holder failure terminated the existing agent")
        self.finish_waiting_agent(process)

    def test_passt_dns_and_workaround_routes_survive_vm_restart(self):
        if not self.use_passt:
            self.skipTest("Set AGENTBOX_TEST_USE_PASST=1 to exercise passt guest networking")
        if os.environ.get("AGENTBOX_TEST_RUNTIME", "krun") == "crun":
            self.skipTest("This regression requires a krun VM, not the explicit crun fallback")
        self.initialize("--persist")
        script = """
import json, socket
from pathlib import Path
assert int(json.loads(Path('/.krun_config.json').read_text())['annotations']['krun.use_passt']) == 1
for service in ('proxy', 'git'):
    assert socket.getaddrinfo(service, 8080, socket.AF_INET, socket.SOCK_STREAM)
routes = [line.split() for line in Path('/proc/net/route').read_text().splitlines()[1:]]
assert any(route[0] == 'eth0' and route[1] != '00000000' and route[2] == '00000000' for route in routes), routes
"""
        self.cli("start", "--session", self.session_id, "--", "python3", "-c", script)
        name = self.config()["services"]["agent"]["container_name"]
        before = self.run_command(["podman", "inspect", "--format", "{{.Id}}", name]).stdout.strip()
        # Reuse the exact retained container, but boot a new guest kernel.
        self.cli("start", "--session", self.session_id)
        self.assertEqual(self.run_command(["podman", "inspect", "--format", "{{.Id}}", name]).stdout.strip(), before)
        self.assert_isolation()

    def test_passt_repairs_existing_ipv4_before_resolving_sidecars(self):
        if not self.use_passt:
            self.skipTest("Set AGENTBOX_TEST_USE_PASST=1 to exercise passt guest networking")
        if os.environ.get("AGENTBOX_TEST_RUNTIME", "krun") == "crun":
            self.skipTest("This regression requires a krun VM, not the explicit crun fallback")
        self.initialize()
        self.assert_isolation()  # Ephemeral start tears down the sidecar containers on exit.
        self.cli("start", "--session", self.session_id, "--", "python3", "-c", """
import json, socket, subprocess, urllib.request
from pathlib import Path

def ip(*args):
    return subprocess.check_output(['ip', *args], text=True)

resolv = Path('/etc/resolv.conf').read_bytes()
original = json.loads(ip('-j', '-4', 'address', 'show', 'dev', 'eth0'))[0]['addr_info']
for address in (original[0]['local'] + '/32', '169.254.2.2/16'):
    ip('-4', 'address', 'flush', 'dev', 'eth0')
    ip('address', 'add', address, 'dev', 'eth0')
    subprocess.run(['agentbox-network'], check=True)
    configured = json.loads(ip('-j', '-4', 'address', 'show', 'dev', 'eth0'))[0]['addr_info']
    assert all(any(a['local'] == b['local'] and a['prefixlen'] == b['prefixlen']
                   for a in configured) for b in original), configured
    for service in ('proxy', 'git'):
        assert socket.getaddrinfo(service, 8080, socket.AF_INET, socket.SOCK_STREAM)
    with socket.create_connection(('proxy', 8080), timeout=5):
        pass
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with client.open('http://git:8083/manifest.json', timeout=5) as response:
        assert 'workspace' in json.load(response)
assert Path('/etc/resolv.conf').read_bytes() == resolv
""")

    def test_passt_blocking_full_duplex_and_large_git_transfer(self):
        if not self.use_passt or os.environ.get("AGENTBOX_TEST_RUNTIME", "krun") == "crun":
            self.skipTest("Requires a real krun/passt VM")
        self.initialize()
        self.cli("start", "--session", self.session_id, "--", "python3", "-c", """
import hashlib, json, os, socket, subprocess, threading
from pathlib import Path

config = json.loads(Path('/.krun_config.json').read_text())
assert int(config['annotations']['krun.use_passt']) == 1
assert Path('/sys/class/net/eth0').exists()
sock = socket.create_connection(('git', 9418), timeout=5)
sock.settimeout(None)  # Git uses blocking sockets for its concurrent read/write.
receiving, received, sent = threading.Event(), threading.Event(), threading.Event()
reply = bytearray()

def reader():
    receiving.set()
    while len(reply) < 4:
        chunk = sock.recv(4 - len(reply))
        if not chunk:
            return
        reply.extend(chunk)
    received.set()

def writer():
    request = b'git-upload-pack /workspace.git\\0host=git\\0'
    sock.sendall(f'{len(request) + 4:04x}'.encode() + request)
    sent.set()

threading.Thread(target=reader, daemon=True).start()
assert receiving.wait(1)
assert not received.wait(0.25)  # Enter recv() before the writer attempts send().
threading.Thread(target=writer, daemon=True).start()
if not received.wait(5) or not sent.wait(5):
    print('Blocking read starved concurrent write on the Git socket', flush=True)
    os._exit(1)  # Do not wait for threads stuck inside the guest kernel.
assert int(reply, 16) > 4, reply
sock.close()

def git(*args):
    subprocess.run(['git', *args], check=True, timeout=30)

git('-C', '/workspace', 'switch', '-c', 'bulk-roundtrip')
payload = os.urandom(4 * 1024 * 1024)
Path('/workspace/bulk').write_bytes(payload)
git('-C', '/workspace', 'add', 'bulk')
git('-C', '/workspace', 'commit', '-m', 'large native Git transfer')
git('-C', '/workspace', 'push', '-u', 'origin', 'HEAD')
git('clone', '--branch', 'bulk-roundtrip', 'git://git/workspace.git', '/tmp/roundtrip')
assert hashlib.sha256(Path('/tmp/roundtrip/bulk').read_bytes()).digest() == hashlib.sha256(payload).digest()
""")

    def test_agent_cannot_reach_management_peers_or_host_even_with_guest_routes(self):
        self.initialize("--persist")
        git = self.inspect_service("git-network")
        network = next(iter(git["NetworkSettings"]["Networks"]))
        info = json.loads(self.run_command(["podman", "network", "inspect", network]).stdout)[0]
        gateway = info["subnets"][0]["gateway"]
        # A real host service bound on every IPv4 interface, not an unused port.
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("0.0.0.0", 0))
        listener.listen()
        host_port = listener.getsockname()[1]
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(udp.close)
        udp.bind(("0.0.0.0", 0))
        udp.settimeout(0.3)
        host_addresses = {gateway}
        for result in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            if not ipaddress.ip_address(result[4][0]).is_loopback:
                host_addresses.add(result[4][0])
        peer = "agentbox-unrelated-" + hashlib.sha256(self.session_id.encode()).hexdigest()[:16]
        self.addCleanup(lambda: self.run_command(["podman", "rm", "-f", peer], check=False))
        self.run_command(["podman", "run", "-d", "--name", peer, "--network", network,
                          "--entrypoint", "python3", self.config()["services"]["git"]["image"],
                          "-m", "http.server", "8080"])
        peer_info = json.loads(self.run_command(["podman", "inspect", peer]).stdout)[0]
        peer_ip = peer_info["NetworkSettings"]["Networks"][network]["IPAddress"]
        script = """
import ipaddress, json, socket, subprocess
from pathlib import Path

def connects(host, port):
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False

for host, port in (('proxy', 8080), ('git', 9418), ('git', 8083)):
    assert connects(host, port), (host, port, 'required service unavailable')
proxy = socket.gethostbyname('proxy')
blocked = [(proxy, 8081), (proxy, 8082), (proxy, 9090),
           (GATEWAY, HOST_PORT), (PEER, 8080), ('::ffff:' + GATEWAY, HOST_PORT)]
blocked.extend((address, HOST_PORT) for address in HOST_ADDRESSES)
for host, port in blocked:
    assert not connects(host, port), (host, port, 'network policy bypass')
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
    for address in HOST_ADDRESSES:
        try:
            udp.sendto(b'AGENTBOX_GUARD_UDP_TEST', (address, UDP_PORT))
        except OSError:
            pass

import http.client
client = http.client.HTTPConnection('proxy', 8080, timeout=5)
client.request('GET', 'http://proxy:8081/')
response = client.getresponse()
assert response.status == 403 and b'not allowed' in response.read()
client.close()

# Root in the guest can add routes; that must not change the outer policy.
if Path('/.krun_config.json').exists() and Path('/sys/class/net/eth0').exists():
    subprocess.run(['ip', 'route', 'replace', 'default', 'via', GATEWAY, 'dev', 'eth0'],
                   check=True)
    for host, port in blocked + [('203.0.113.1', 443)]:
        assert not connects(host, port), (host, port, 'guest route bypass')
if not Path('/.krun_config.json').exists():
    # The explicit ordinary-container test fallback cannot alter the shared namespace either.
    result = subprocess.run(['ip', 'route', 'replace', 'default', 'via', GATEWAY, 'dev', 'eth0'],
                            capture_output=True)
    assert result.returncode != 0, 'Agent acquired outer NET_ADMIN'
    try:
        socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
        raise AssertionError('Agent acquired outer NET_RAW')
    except PermissionError:
        pass
print('SIDECAR_ONLY_EGRESS_VERIFIED', flush=True)
"""
        script = (f"GATEWAY={gateway!r}; HOST_PORT={host_port}; PEER={peer_ip!r}; "
                  f"HOST_ADDRESSES={sorted(host_addresses)!r}; UDP_PORT={udp.getsockname()[1]}\n") + script
        self.cli("start", "--session", self.session_id, "--", "python3", "-c", script)
        with self.assertRaises(socket.timeout):
            udp.recvfrom(128)
        # A new VM boot reuses the same guarded namespace/container contract.
        self.cli("start", "--session", self.session_id)
        with self.assertRaises(socket.timeout):
            udp.recvfrom(128)

    def test_vertex_metadata_is_the_only_optional_proxy_port(self):
        preset = self.home / "custom" / "presets" / "integration" / "proxy.yaml"
        preset.write_text(yaml.safe_dump({"providers": [{
            "name": "vertex", "enabled": True, "metadata_server": True,
            "credential_type": "static", "api_key_env": "TEST_VERTEX_KEY", "request_policy": [],
        }], "environment": {"TEST_VERTEX_KEY": "test-only"}}))
        self.initialize()
        self.cli("start", "--session", self.session_id, "--", "python3", "-c", """
import json, socket, urllib.request
client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with client.open('http://proxy:9090/computeMetadata/v1/instance/service-accounts/default/token', timeout=5) as response:
    assert json.load(response)['access_token'] == 'dummy-replaced-by-proxy'
for port in (8081, 8082):
    try:
        with socket.create_connection(('proxy', port), timeout=1):
            raise AssertionError(('Management port reachable', port))
    except OSError:
        pass
""")

    def test_http_proxy_cannot_forward_to_host_or_its_own_management(self):
        preset = self.home / "custom" / "presets" / "integration" / "proxy.yaml"
        preset.write_text(yaml.safe_dump({"providers": [],
                                          "extra_request_policy": [{"host": ".*", "port": ".*"}]}))
        self.initialize()
        git = self.inspect_service("git-network")
        network = next(iter(git["NetworkSettings"]["Networks"]))
        info = json.loads(self.run_command(["podman", "network", "inspect", network]).stdout)[0]
        gateway = info["subnets"][0]["gateway"]
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("0.0.0.0", 0))
        listener.listen()
        listener.settimeout(0.3)
        targets = [f"http://{gateway}:{listener.getsockname()[1]}/",
                   "http://proxy:8081/", "http://127.0.0.1:8082/health",
                   "http://[::1]:8082/health"]
        script = f"TARGETS={targets!r}\n" + """
import http.client
for target in TARGETS:
    client = http.client.HTTPConnection('proxy', 8080, timeout=5)
    client.request('GET', target)
    response = client.getresponse()
    body = response.read()
    assert response.status == 502 and b'Agentbox blocked' in body, (target, response.status, body)
    client.close()
"""
        self.cli("start", "--session", self.session_id, "--", "python3", "-c", script)
        with self.assertRaises(socket.timeout):
            listener.accept()

    def test_http_proxy_refuses_connect_dns_without_an_l7_rule(self):
        self.initialize("--persist")
        name = "agentbox-dns-" + hashlib.sha256(self.session_id.encode()).hexdigest()[:16]
        network = name + "-net"
        self.run_command(["podman", "network", "create", "-o", "isolate=false", network])
        self.addCleanup(lambda: self.run_command(["podman", "network", "rm", network], check=False))
        self.addCleanup(lambda: self.run_command(["podman", "rm", "-f", name], check=False))
        self.run_command(["podman", "run", "-d", "--name", name, "--network", network,
                          "--entrypoint", "python3", self.config()["services"]["proxy"]["image"], "-c", """
import socketserver, threading
from pathlib import Path
class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        with Path('/tmp/dns-connections').open('a') as log:
            log.write(str(self.server.server_address[1]) + '\\n')
        size = self.rfile.read(2)
        if len(size) == 2:
            query = bytearray(self.rfile.read(int.from_bytes(size, 'big')))
            query[2] |= 0x80
            self.wfile.write(size + query)
servers = [socketserver.ThreadingTCPServer(('0.0.0.0', port), Handler) for port in (53, 5353)]
threading.Thread(target=servers[0].serve_forever, daemon=True).start()
servers[1].serve_forever()
"""])
        fixture = json.loads(self.run_command(["podman", "inspect", name]).stdout)[0]
        address = fixture["NetworkSettings"]["Networks"][network]["IPAddress"]
        # Only the address is trusted, not any L7 request. This isolates DNS
        # protocol enforcement from the independent private-address restriction.
        config = self.session / "proxy-config" / "proxy.yaml"
        config.write_text(yaml.safe_dump({"providers": [], "extra_request_policy": [],
                                         "private_upstreams": [address]}))
        script = f"TARGET={address!r}\n" + """
import socket, struct
def read_exact(client, size):
    result = b''
    while len(result) < size:
        part = client.recv(size - len(result))
        assert part, 'Connection closed before the DNS response'
        result += part
    return result
query = struct.pack('!6H', 0x1234, 0x100, 1, 0, 0, 0)
query += b'\\x05hello\\x07example\\x00' + struct.pack('!2H', 1, 1)
for port in (53, 5353):
    with socket.create_connection(('proxy', 8080), timeout=5) as client:
        client.sendall(f'CONNECT {TARGET}:{port} HTTP/1.1\\r\\nHost: {TARGET}:{port}\\r\\n\\r\\n'.encode())
        header = b''
        while not header.endswith(b'\\r\\n\\r\\n'):
            header += read_exact(client, 1)
        assert b' 200 ' in header, header
        for _ in range(2):
            client.sendall(struct.pack('!H', len(query)) + query)
            size = int.from_bytes(read_exact(client, 2), 'big')
            response = read_exact(client, size)
            assert response[:2] == query[:2], response
            assert response[3] & 15 == 5, ('DNS was not refused', port, response)
"""
        self.cli("start", "--session", self.session_id, "--", "python3", "-c", script)
        # No tunneled query may even open an upstream socket. Then prove that
        # both fixture listeners are reachable from the proxy, avoiding a false
        # pass from bridge isolation or a missing DNS server.
        self.run_command(["podman", "exec", name, "python3", "-c",
                          "from pathlib import Path; assert not Path('/tmp/dns-connections').exists()"])
        services = self.config()["services"]
        proxy = services["proxy"]["container_name"]
        self.run_command(["podman", "start", services["proxy-network"]["container_name"], proxy])
        self.run_command(["podman", "exec", proxy, "python3", "-c",
                          f"import socket\nfor port in (53, 5353):\n"
                          f"    with socket.create_connection(({address!r}, port), timeout=5): pass\n"])

    def test_allowed_http_https_reuse_and_private_upstream_restart_policy(self):
        preset = self.home / "custom" / "presets" / "integration" / "proxy.yaml"
        preset.write_text(yaml.safe_dump({"providers": [], "extra_request_policy": [
            {"host": r"upstream\.example", "port": port} for port in (8080, 8443)]}))
        self.initialize("--persist")
        name = "agentbox-upstream-" + hashlib.sha256(self.session_id.encode()).hexdigest()[:16]
        network = name + "-net"
        self.run_command(["podman", "network", "create", "-o", "isolate=false", network])
        self.addCleanup(lambda: self.run_command(["podman", "network", "rm", network], check=False))
        certs = self.root / "tls"
        certs.mkdir()
        certificate, key = certs / "upstream.crt", certs / "upstream.key"
        self.run_command(["podman", "run", "--rm", "--network", "none", "-v", f"{certs}:/tls:z",
                          "--entrypoint", "python3", self.config()["services"]["proxy"]["image"], "-c", """
from datetime import datetime, timedelta, timezone
from pathlib import Path
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
key = rsa.generate_private_key(public_exponent=65537,key_size=2048)
name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'upstream.example')])
now = datetime.now(timezone.utc)
cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(days=1))
        .not_valid_after(now+timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True,path_length=None),critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName('upstream.example')]),critical=False)
        .sign(key,hashes.SHA256()))
Path('/tls/upstream.crt').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
Path('/tls/upstream.key').write_bytes(key.private_bytes(serialization.Encoding.PEM,
    serialization.PrivateFormat.TraditionalOpenSSL,serialization.NoEncryption()))
"""])
        self.addCleanup(lambda: self.run_command(["podman", "rm", "-f", name], check=False))
        self.run_command(["podman", "run", "-d", "--name", name, "--network", network,
                          "-v", f"{certs}:/tls:ro,z", "--entrypoint", "python3",
                          self.config()["services"]["proxy"]["image"], "-c", """
import ssl, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import StreamEnded
class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def handle(self):
        if getattr(self.connection, 'selected_alpn_protocol', lambda: None)() != 'h2':
            return super().handle()
        connection = H2Connection(config=H2Configuration(client_side=False))
        connection.initiate_connection()
        self.connection.sendall(connection.data_to_send())
        while data := self.connection.recv(65536):
            for event in connection.receive_data(data):
                if isinstance(event, StreamEnded):
                    body = b'EXTERNAL_FIXTURE_OK H2 ' + str(self.client_address[1]).encode()
                    connection.send_headers(event.stream_id, [(':status', '200'), ('content-length', str(len(body)))])
                    connection.send_data(event.stream_id, body, end_stream=True)
            self.connection.sendall(connection.data_to_send())
    def do_GET(self):
        body = b'EXTERNAL_FIXTURE_OK ' + str(self.client_address[1]).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
plain = ThreadingHTTPServer(('0.0.0.0',8080),Handler)
secure = ThreadingHTTPServer(('0.0.0.0',8443),Handler)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain('/tls/upstream.crt','/tls/upstream.key')
context.set_alpn_protocols(['h2', 'http/1.1'])
secure.socket = context.wrap_socket(secure.socket,server_side=True)
threading.Thread(target=plain.serve_forever,daemon=True).start()
secure.serve_forever()
"""])
        fixture = json.loads(self.run_command(["podman", "inspect", name]).stdout)[0]
        address = fixture["NetworkSettings"]["Networks"][network]["IPAddress"]
        proxy_config = self.session / "proxy-config" / "proxy.yaml"
        policy = yaml.safe_load(proxy_config.read_text())
        policy["private_upstreams"] = [address]  # Explicitly trust only this remote fixture IP.
        proxy_config.write_text(yaml.safe_dump(policy))
        cfg = self.config()
        cfg["services"]["proxy-network"]["extra_hosts"] = [f"upstream.example:{address}"]
        cfg["services"]["proxy"]["volumes"].append(f"{certificate}:/custom-certs/upstream.crt:ro,z")
        (self.session / "compose.yaml").write_text(yaml.safe_dump(cfg))
        self.cli("start", "--session", self.session_id, "--", "python3", "-c", """
import http.client, ssl
for secure in (False, True):
    if secure:
        client = http.client.HTTPSConnection('proxy', 8080, context=ssl.create_default_context(), timeout=10)
        client.set_tunnel('upstream.example', 8443)
        target = '/'
    else:
        client = http.client.HTTPConnection('proxy', 8080, timeout=10)
        target = 'http://upstream.example:8080/'
    ports = []
    for _ in range(8):
        client.request('GET', target)
        response = client.getresponse()
        body = response.read()
        assert response.status == 200 and body.startswith(b'EXTERNAL_FIXTURE_OK '), (response.status, body)
        ports.append(body.split()[-1])
    assert len(set(ports)) == 1, ('upstream connection not reused', ports)
    client.close()
""")
        # Exercise the production proxy with an HTTP/2 client as well. The
        # proxy image supplies h2; the minimal agent image deliberately does not.
        proxy = cfg["services"]["proxy"]["container_name"]
        self.run_command(["podman", "start", cfg["services"]["proxy-network"]["container_name"], proxy])
        self.cli("proxy", "reload", "--session", self.session_id)
        self.run_command(["podman", "exec", proxy, "python3", "-c", """
import socket, ssl
from h2.connection import H2Connection
from h2.events import DataReceived, ResponseReceived, StreamEnded
raw = socket.create_connection(('127.0.0.1', 8080), timeout=10)
raw.sendall(b'CONNECT upstream.example:8443 HTTP/1.1\\r\\nHost: upstream.example:8443\\r\\n\\r\\n')
header = b''
while not header.endswith(b'\\r\\n\\r\\n'):
    header += raw.recv(1)
assert b' 200 ' in header, header
context = ssl.create_default_context(cafile='/root/.mitmproxy/mitmproxy-ca-cert.pem')
context.set_alpn_protocols(['h2'])
with context.wrap_socket(raw, server_hostname='upstream.example') as client:
    assert client.selected_alpn_protocol() == 'h2'
    connection = H2Connection()
    connection.initiate_connection()
    bodies = {stream: b'' for stream in range(1, 17, 2)}
    for stream in bodies:
        connection.send_headers(stream, [(':method', 'GET'), (':scheme', 'https'),
            (':authority', 'upstream.example:8443'), (':path', '/')], end_stream=True)
    client.sendall(connection.data_to_send())
    completed = set()
    while len(completed) < len(bodies):
        data = client.recv(65536)
        assert data, 'HTTP/2 connection closed prematurely'
        for event in connection.receive_data(data):
            if isinstance(event, ResponseReceived):
                assert (b':status', b'200') in event.headers, event.headers
            elif isinstance(event, DataReceived):
                bodies[event.stream_id] += event.data
                connection.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
            elif isinstance(event, StreamEnded):
                completed.add(event.stream_id)
        client.sendall(connection.data_to_send())
    assert all(body.startswith(b'EXTERNAL_FIXTURE_OK H2 ') for body in bodies.values()), bodies
    assert len({body.split()[-1] for body in bodies.values()}) == 1, bodies
"""])
        # Keep a real upstream connection pooled while trying to revoke its IP.
        # Reload must reject the whole edit; restart must close the old pool.
        process = subprocess.Popen(["podman", "exec", "-i", proxy, "python3", "-c", """
import http.client, sys
client = http.client.HTTPConnection('127.0.0.1', 8080, timeout=10)
def check():
    client.request('GET', 'http://upstream.example:8080/')
    response = client.getresponse()
    body = response.read()
    assert response.status == 200, (response.status, body)
    return body
original = check()
print('PRIVATE_UPSTREAM_READY', flush=True)
for _ in sys.stdin:
    assert check() == original, 'Pooled upstream connection replaced'
    print('PRIVATE_UPSTREAM_REUSED', flush=True)
"""], env=self.env, cwd=self.project, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        def cleanup():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            process.stdin.close()
            process.stdout.close()

        self.addCleanup(cleanup)
        self.read_agent_output(process, b"PRIVATE_UPSTREAM_READY")
        policy["private_upstreams"] = []
        proxy_config.write_text(yaml.safe_dump(policy))
        # Exercise both the full synchronization and fast-reload endpoints.
        rejected = self.cli("proxy", "reload", "--session", self.session_id, check=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("proxy restart", rejected.stderr)
        reply = self.run_command(["podman", "exec", proxy, "curl", "-sS", "--max-time", "10",
                                  "-w", "\n%{http_code}", "http://127.0.0.1:8082/reload"])
        body, _, status = reply.stdout.rpartition("\n")
        self.assertEqual(status, "409")
        self.assertIs(json.loads(body)["restart_required"], True)
        process.stdin.write(b"check\n")
        process.stdin.flush()
        self.read_agent_output(process, b"PRIVATE_UPSTREAM_REUSED")
        self.cli("proxy", "restart", "--session", self.session_id)
        self.assertNotEqual(process.wait(timeout=15), 0, "Proxy restart retained the old client")
        self.run_command(["podman", "exec", proxy, "python3", "-c", """
import http.client
client = http.client.HTTPConnection('127.0.0.1', 8080, timeout=10)
client.request('GET', 'http://upstream.example:8080/')
response = client.getresponse()
body = response.read()
assert response.status == 502 and b'Agentbox blocked' in body, (response.status, body)
"""])


class AgentOutputTest(unittest.TestCase):
    def test_readiness_requires_a_marker_line_not_an_error_quoting_the_command(self):
        script = "print(\"Error executing: print('NETWORK_LIFETIME_READY')\", flush=True)"
        with subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE) as process:
            with self.assertRaisesRegex(AssertionError, "Error executing"):
                AgentboxIntegrationTest().read_agent_output(process, b"NETWORK_LIFETIME_READY")

    def test_readiness_accepts_a_complete_marker_line(self):
        script = "print('banner\\nNETWORK_LIFETIME_READY', flush=True)"
        with subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE) as process:
            AgentboxIntegrationTest().read_agent_output(process, b"NETWORK_LIFETIME_READY")


if __name__ == "__main__":
    unittest.main()
