# mitmproxy addon: allowlist enforcement, credential injection, and JSON access logging.
import asyncio, dataclasses, json, logging, os, re, sys, time
import yaml
from mitmproxy import http

_addon_dir = os.path.dirname(os.path.abspath(__file__))
if _addon_dir not in sys.path:
    sys.path.insert(0, _addon_dir)

from provider import (Provider, CompiledRule, CredentialUnavailable, compile_rules,
                      inject_matching_providers, rule_matches)
from resolvers import RESOLVER_CLASSES
from secret_contract import fingerprint, injection_secret_name

_CONFIG_PATH = "/config/proxy.yaml"
_RELOAD_PORT = 8082

# Content-Types that must never be fully buffered by mitmproxy: plain proto/protobuf
# bodies, Buf Connect streaming variants ("application/connect+proto",
# "application/connect+json") used by bidirectional/server-streaming RPCs, and
# Server-Sent Events ("text/event-stream") used by HTTP/1.1 fallbacks such as
# Cursor CLI's useHttp1ForAgent. Buffering those would make mitmproxy wait for
# the body to end before forwarding anything — which never happens for a
# long-lived stream, hanging the connection indefinitely. The client then sees
# the stream close without a terminal event (e.g. turnEnded).
_STREAMABLE_CONTENT_TYPES = (
    "application/proto",
    "application/x-protobuf",
    "application/connect+",
    "application/grpc",
    "text/event-stream",
)


def _is_streamable_content_type(content_type: str) -> bool:
    ct = content_type.lower()
    return any(ct.startswith(prefix) for prefix in _STREAMABLE_CONTENT_TYPES)


def _wants_sse(flow: http.HTTPFlow) -> bool:
    return "text/event-stream" in flow.request.headers.get("accept", "").lower()


class JSONFormatter(logging.Formatter):
    def __init__(self, source: str):
        super().__init__()
        self.source = source

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "source": self.source,
        }
        if isinstance(record.msg, dict):
            entry.update(record.msg)
        else:
            entry["level"] = record.levelname.lower()
            entry["message"] = record.getMessage()
        return json.dumps(entry)


log = logging.getLogger("proxy")
log.setLevel(logging.INFO)
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(JSONFormatter("proxy"))
log.addHandler(_handler)
log.propagate = False

_session_name = os.environ.get("AGENTBOX_NAME", "")
_name_flag = f" --name {_session_name}" if _session_name else ""

_log_req_hdr, _log_resp_hdr, _log_bodies = True, False, False


@dataclasses.dataclass(frozen=True)
class _Config:
    allowed_rules: list[CompiledRule]
    providers: list[Provider]
    fingerprint: str = ""
    provider_fingerprint: str = ""
    logging_flags: tuple[bool, bool, bool] = (True, False, False)

    @property
    def unavailable(self) -> list[dict]:
        return [policy for provider in self.providers for policy in provider.unavailable]


def _read_config() -> dict:
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _build_allowed_rules(providers: list[Provider], cfg: dict) -> list[CompiledRule]:
    rules: list[CompiledRule] = []
    for p in providers:
        rules.extend(p._rules)
    rules.extend(compile_rules(cfg.get("extra_request_policy", [])))
    return rules


def _blocked_request_entry(flow: http.HTTPFlow) -> dict:
    request = flow.request
    host = request.pretty_host
    path = request.path.split("?", 1)[0]
    method = request.method.upper()

    entry = {
        "method": method,
        "url": request.pretty_url,
        "status": 403,
        "blocked": True,
        "blocked_reason": "no matching policy rule",
        "request_host": host,
        "request_port": request.port,
        "request_path": path,
        "request_method": method,
    }
    if _log_req_hdr:
        entry["req_headers"] = dict(request.headers)
    if _log_bodies:
        entry["req_body"] = request.get_text(strict=False)
    return entry


def _deny_request(flow: http.HTTPFlow) -> None:
    host = flow.request.pretty_host
    flow.response = http.Response.make(
        403, f"Host '{host}' not allowed.\nTo allow it, run the following command outside of the sandbox: agentbox allow{_name_flag} {host}\n",
        {"Content-Type": "text/plain"},
    )
    log.info(_blocked_request_entry(flow))


def _log_failed_blocked_request(flow: http.HTTPFlow) -> None:
    entry = _blocked_request_entry(flow)
    entry["status"] = None
    entry["blocked_reason"] = "no matching policy rule; request body failed"
    log.info(entry)


class AgentboxAddon:
    def __init__(self):
        self._reload_lock = asyncio.Lock()
        self._apply_candidate(self._build_candidate(_read_config()))
        log.info({"message": "Config loaded",
                  "allowed_rules": len(self._cfg.allowed_rules),
                  "providers": len(self._cfg.providers)})

    @staticmethod
    def _load_providers(cfg: dict) -> list[Provider]:
        providers: list[Provider] = []
        raw = cfg.get("providers", [])
        if not isinstance(raw, list):
            raise ValueError("providers must be a list")
        targets = {}
        for p in raw:
            if not isinstance(p, dict):
                raise ValueError("provider must be a mapping")
            if not p.get("enabled"):
                continue
            cred_type = p.get("credential_type")
            resolver_cls = RESOLVER_CLASSES.get(cred_type)
            if resolver_cls is None:
                raise ValueError("Unknown provider credential_type")
            if not isinstance(p.get("name", "unknown"), str):
                raise ValueError("provider name must be a string")
            provider = Provider(p, resolver_cls)
            for source in provider.secret_sources:
                target = injection_secret_name(source)
                if target in targets and targets[target] != source:
                    raise ValueError("Provider secret target collision")
                targets[target] = source
            providers.append(provider)
        return providers

    @classmethod
    def _build_candidate(cls, cfg: dict, providers=None) -> _Config:
        if not isinstance(cfg, dict):
            raise ValueError("Configuration must be a mapping")
        full = fingerprint(cfg)
        provider_fp = fingerprint(cfg.get("providers", []))
        lcfg = cfg.get("logging", {})
        flags = tuple(lcfg.get(key, default) for key, default in (
            ("log_request_headers", True), ("log_response_headers", False), ("log_bodies", False)))
        if not all(isinstance(flag, bool) for flag in flags):
            raise ValueError("Logging settings must be booleans")
        if providers is None:
            providers = cls._load_providers(cfg)
        return _Config(_build_allowed_rules(providers, cfg), providers, full, provider_fp, flags)

    def _apply_candidate(self, candidate: _Config) -> None:
        global _log_req_hdr, _log_resp_hdr, _log_bodies
        # No awaits: hooks see either the complete old or complete new candidate.
        self._cfg = candidate
        _log_req_hdr, _log_resp_hdr, _log_bodies = candidate.logging_flags

    async def running(self):
        self._reload_server = await asyncio.start_server(self._handle_reload_conn, "127.0.0.1", _RELOAD_PORT)
        log.info({"message": f"Reload endpoint listening on port {_RELOAD_PORT}"})

    async def _handle_reload_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            data = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            lines = data.decode("ascii").split("\r\n")
            method, path, _ = lines[0].split()
            headers = {key.lower(): value for key, value in
                       (line.split(":", 1) for line in lines[1:] if ":" in line)}
            if method not in {"GET", "POST"} or path not in {"/reload", "/reload/providers", "/health"}:
                body, status = {"error": "Unknown endpoint"}, b"404 Not Found"
            elif path == "/health":
                body, status = {"ready": True}, b"200 OK"
            else:
                async with self._reload_lock:
                    cfg = _read_config()
                    full = fingerprint(cfg)
                    provider_fp = fingerprint(cfg.get("providers", []))
                    expected = headers.get("x-agentbox-config-fingerprint", "").strip()
                    if path == "/reload/providers" and not re.fullmatch(r"[0-9a-f]{64}", expected):
                        body, status = {"error": "Expected configuration fingerprint required"}, b"400 Bad Request"
                    elif ((path == "/reload/providers" and expected != full)
                          or (path == "/reload" and provider_fp != self._cfg.provider_fingerprint)):
                        body = {"error": "Concurrent configuration change; run agentbox proxy-reload"}
                        status = b"409 Conflict"
                    else:
                        providers = self._cfg.providers if path == "/reload" else None
                        candidate = await asyncio.get_running_loop().run_in_executor(
                            None, self._build_candidate, cfg, providers)
                        self._apply_candidate(candidate)
                        log.info({"message": "Config reloaded", "providers": len(candidate.providers),
                                  "allowed_rules": len(candidate.allowed_rules),
                                  "unavailable": candidate.unavailable})
                        body = {"fingerprint": candidate.fingerprint,
                                "provider_fingerprint": candidate.provider_fingerprint,
                                "unavailable": candidate.unavailable}
                        status = b"200 OK"
        except Exception:
            log.error("Config reload failed (keeping previous config)")
            body = {"error": "Invalid configuration; previous runtime retained"}
            status = b"500 Internal Server Error"
        body = json.dumps(body).encode()
        response = (
            b"HTTP/1.1 " + status + b"\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n\r\n" + body
        )
        writer.write(response)
        await writer.drain()
        writer.close()

    def requestheaders(self, flow: http.HTTPFlow) -> None:
        cfg = self._cfg
        host = flow.request.pretty_host
        port = flow.request.port
        path = flow.request.path.split("?", 1)[0]
        method = flow.request.method.upper()

        if not any(rule_matches(rule, host, port, path, method) for rule in cfg.allowed_rules):
            flow.metadata["agentbox_blocked"] = True
            if _log_bodies:
                flow.request.stream = False
                flow.metadata["agentbox_block_pending"] = True
            else:
                _deny_request(flow)
            return

        try:
            injected_providers = inject_matching_providers(cfg.providers, flow)
        except CredentialUnavailable:
            flow.metadata["agentbox_blocked"] = True
            flow.response = http.Response.make(
                503, "Provider credential unavailable; run agentbox proxy-reload outside the sandbox.\n",
                {"Content-Type": "text/plain"})
            log.warning({"message": "Provider credential unavailable", "status": 503})
            return
        if injected_providers:
            flow.metadata["agentbox_provider"] = injected_providers[-1]
            flow.metadata["agentbox_providers"] = injected_providers

        if _is_streamable_content_type(flow.request.headers.get("content-type", "")):
            flow.request.stream = True

    def request(self, flow: http.HTTPFlow) -> None:
        """Finish deferred denials after mitmproxy has read the complete request body."""
        if flow.metadata.pop("agentbox_block_pending", False):
            _deny_request(flow)

    def error(self, flow: http.HTTPFlow) -> None:
        """Log a deferred denial when an incomplete request body prevents request()."""
        if flow.metadata.pop("agentbox_block_pending", False):
            _log_failed_blocked_request(flow)

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        if flow.metadata.get("agentbox_blocked"):
            return
        resp = flow.response
        if resp is None:
            return
        # HTTP/1.1 SSE: request is often application/connect+json while the
        # response is text/event-stream (or Accept advertised SSE). Stream if
        # either side looks like a long-lived RPC/SSE body.
        if (
            _is_streamable_content_type(resp.headers.get("content-type", ""))
            or _is_streamable_content_type(flow.request.headers.get("content-type", ""))
            or _wants_sse(flow)
        ):
            flow.response.stream = True

    def response(self, flow: http.HTTPFlow) -> None:
        if flow.metadata.get("agentbox_blocked"):
            return
        resp = flow.response
        entry: dict = {
            "method": flow.request.method,
            "url": flow.request.pretty_url,
            "status": resp.status_code if resp else None,
            "duration_ms": (
                round((resp.timestamp_end - flow.request.timestamp_start) * 1000)
                if resp and resp.timestamp_end else None
            ),
        }
        provider = flow.metadata.get("agentbox_provider")
        if provider:
            entry["provider"] = provider
        providers = flow.metadata.get("agentbox_providers", [])
        if len(providers) > 1:
            entry["providers"] = providers
        if _log_req_hdr:
            redacted = flow.metadata.get("agentbox_injected_headers", set())
            entry["req_headers"] = {key: "[redacted]" if key.lower() in redacted else value
                                    for key, value in flow.request.headers.items()}
        if _log_resp_hdr and resp:
            entry["resp_headers"] = dict(resp.headers)
        if _log_bodies:
            entry["req_body"]  = flow.request.get_text(strict=False)
            entry["resp_body"] = resp.get_text(strict=False) if resp else None
        log.info(entry)


addons = [AgentboxAddon()]
