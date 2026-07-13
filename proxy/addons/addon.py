# mitmproxy addon: allowlist enforcement, credential injection, and JSON access logging.
import asyncio, dataclasses, json, logging, os, sys, time
import yaml
from mitmproxy import http

_addon_dir = os.path.dirname(os.path.abspath(__file__))
if _addon_dir not in sys.path:
    sys.path.insert(0, _addon_dir)

from provider import Provider, CompiledRule, compile_rules, rule_matches
from resolvers import RESOLVER_CLASSES

_CONFIG_PATH = "/config/proxy.yaml"
_RELOAD_PORT = 8082

# Content-Types that must never be fully buffered by mitmproxy: plain proto/protobuf
# bodies, and the Buf Connect streaming variants ("application/connect+proto",
# "application/connect+json") used by bidirectional/server-streaming RPCs. Buffering
# those would make mitmproxy wait for the body to end before forwarding anything -
# which never happens for a long-lived stream, hanging the connection indefinitely.
_STREAMABLE_CONTENT_TYPES = (
    "application/proto",
    "application/x-protobuf",
    "application/connect+",
    "application/grpc",
)


def _is_streamable_content_type(content_type: str) -> bool:
    return any(content_type.startswith(ct) for ct in _STREAMABLE_CONTENT_TYPES)


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

with open(_CONFIG_PATH) as f:
    _startup_cfg = yaml.safe_load(f)

_session_name = os.environ.get("AGENTBOX_NAME", "")
_name_flag = f" --name {_session_name}" if _session_name else ""

lcfg = _startup_cfg.get("logging", {})
_log_req_hdr  = lcfg.get("log_request_headers", True)
_log_resp_hdr = lcfg.get("log_response_headers", False)
_log_bodies   = lcfg.get("log_bodies", False)


@dataclasses.dataclass(frozen=True)
class _Config:
    allowed_rules: list[CompiledRule]
    providers: list[Provider]


def _read_config() -> dict:
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _build_allowed_rules(providers: list[Provider], cfg: dict) -> list[CompiledRule]:
    rules: list[CompiledRule] = []
    for p in providers:
        rules.extend(p._rules)
    rules.extend(compile_rules(cfg.get("extra_request_policy", [])))
    return rules


class AgentboxAddon:
    def __init__(self):
        cfg = _read_config()
        self._providers = self._load_providers(cfg)
        self._raw_providers = cfg.get("providers", [])
        allowed_rules = _build_allowed_rules(self._providers, cfg)
        self._cfg = _Config(allowed_rules=allowed_rules, providers=self._providers)
        log.info({"message": "Config loaded",
                  "allowed_rules": len(self._cfg.allowed_rules),
                  "providers": len(self._cfg.providers)})

    @staticmethod
    def _load_providers(cfg: dict) -> list[Provider]:
        providers: list[Provider] = []

        for p in cfg.get("providers", []):
            if not p.get("enabled"):
                continue
            cred_type = p.get("credential_type")
            if cred_type:
                resolver_cls = RESOLVER_CLASSES.get(cred_type)
                if resolver_cls:
                    resolver = resolver_cls(p)
                    providers.append(Provider(p, resolver))
                else:
                    log.error("Unknown credential_type '%s' for provider '%s'",
                              cred_type, p.get("name", "?"))

        return providers

    async def running(self):
        self._reload_server = await asyncio.start_server(self._handle_reload_conn, "127.0.0.1", _RELOAD_PORT)
        log.info({"message": f"Reload endpoint listening on port {_RELOAD_PORT}"})

    async def _handle_reload_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            data = await reader.read(4096)
            cfg = _read_config()
            new_raw_providers = cfg.get("providers", [])

            if new_raw_providers != self._raw_providers:
                loop = asyncio.get_event_loop()
                providers = await loop.run_in_executor(None, self._load_providers, cfg)
                self._providers = providers
                self._raw_providers = new_raw_providers
                log.info({"message": "Full reload: providers rebuilt"})
            else:
                providers = self._providers

            allowed_rules = _build_allowed_rules(providers, cfg)
            new_cfg = _Config(allowed_rules=allowed_rules, providers=providers)
            self._cfg = new_cfg
            log.info({"message": "Config reloaded",
                      "allowed_rules": len(new_cfg.allowed_rules),
                      "providers": len(new_cfg.providers)})
            body = b"OK"
            status = b"200 OK"
        except Exception as exc:
            log.error("Config reload failed (keeping previous config): %s", exc)
            body = str(exc).encode()
            status = b"500 Internal Server Error"
        response = (
            b"HTTP/1.1 " + status + b"\r\n"
            b"Content-Type: text/plain\r\n"
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
            flow.response = http.Response.make(
                403, f"Host '{host}' not allowed.\nTo allow it, run the following command outside of the sandbox: agentbox allow{_name_flag} {host}\n",
                {"Content-Type": "text/plain"},
            )
            flow.metadata["agentbox_blocked"] = True
            log.info({
                "method": method,
                "url": flow.request.pretty_url, "status": 403, "blocked": True,
                "blocked_reason": "no matching policy rule",
                "request_host": host,
                "request_port": port,
                "request_path": path,
                "request_method": method,
            })
            return

        for provider in cfg.providers:
            if provider.matches(flow):
                provider.inject(flow)
                flow.metadata["agentbox_provider"] = provider.name
                break

        if _is_streamable_content_type(flow.request.headers.get("content-type", "")):
            flow.request.stream = True

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        if flow.metadata.get("agentbox_blocked"):
            return
        resp = flow.response
        if resp is not None and _is_streamable_content_type(resp.headers.get("content-type", "")):
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
        if _log_req_hdr:
            entry["req_headers"] = dict(flow.request.headers)
        if _log_resp_hdr and resp:
            entry["resp_headers"] = dict(resp.headers)
        if _log_bodies:
            entry["req_body"]  = flow.request.get_text(strict=False)
            entry["resp_body"] = resp.get_text(strict=False) if resp else None
        log.info(entry)


addons = [AgentboxAddon()]
