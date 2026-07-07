# mitmproxy addon: allowlist enforcement, credential injection, and JSON access logging.
import asyncio, dataclasses, fnmatch, json, logging, os, sys, time
import yaml
from mitmproxy import http

_addon_dir = os.path.dirname(os.path.abspath(__file__))
if _addon_dir not in sys.path:
    sys.path.insert(0, _addon_dir)

from provider import Provider
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
    allowed: list[str]
    providers: list[Provider]


class AgentboxAddon:
    def __init__(self):
        self._providers = self._load_providers()
        self._cfg = _Config(allowed=self._load_allowed_hosts(), providers=self._providers)
        log.info({"message": "Config loaded",
                  "allowed_hosts": len(self._cfg.allowed),
                  "providers": len(self._cfg.providers)})

    @staticmethod
    def _load_providers() -> list[Provider]:
        with open(_CONFIG_PATH) as f:
            cfg = yaml.safe_load(f)

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

    @staticmethod
    def _load_allowed_hosts() -> list[str]:
        with open(_CONFIG_PATH) as f:
            cfg = yaml.safe_load(f)
        allowed: list[str] = []
        for p in cfg.get("providers", []):
            if p.get("enabled"):
                allowed.extend(p.get("allowed_hosts", []))
        allowed.extend(cfg.get("extra_allowed_hosts", []))
        return allowed

    async def running(self):
        await asyncio.start_server(self._handle_reload_conn, "127.0.0.1", _RELOAD_PORT)
        log.info({"message": f"Reload endpoint listening on port {_RELOAD_PORT}"})

    async def _handle_reload_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.read(4096)
            new_cfg = _Config(allowed=self._load_allowed_hosts(), providers=self._providers)
            self._cfg = new_cfg
            log.info({"message": "Config reloaded",
                      "allowed_hosts": len(new_cfg.allowed),
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
        if not any(fnmatch.fnmatch(host, p) for p in cfg.allowed):
            flow.response = http.Response.make(
                403, f"Host '{host}' not allowed.\nTo allow it, run the following command outside of the sandbox: agentbox allow{_name_flag} {host}\n",
                {"Content-Type": "text/plain"},
            )
            flow.metadata["agentbox_blocked"] = True
            log.info({
                "method": flow.request.method,
                "url": flow.request.pretty_url, "status": 403, "blocked": True,
            })
            return

        for provider in cfg.providers:
            if provider.matches(flow):
                provider.inject(flow)
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
        if _log_req_hdr:
            entry["req_headers"] = dict(flow.request.headers)
        if _log_resp_hdr and resp:
            entry["resp_headers"] = dict(resp.headers)
        if _log_bodies:
            entry["req_body"]  = flow.request.get_text(strict=False)
            entry["resp_body"] = resp.get_text(strict=False) if resp else None
        log.info(entry)


addons = [AgentboxAddon()]
