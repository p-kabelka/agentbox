# mitmproxy addon: OPA policy enforcement, credential injection, and JSON access logging.
import asyncio, dataclasses, json, logging, os, sys, time, urllib.parse, urllib.request
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

_REDACTED_HEADERS = frozenset({"authorization", "x-api-key", "proxy-authorization", "cookie"})

_DEFAULT_OPA_URL = "http://opa:8181"
_DEFAULT_OPA_TIMEOUT = 5
_DEFAULT_MAX_BODY_SIZE = 67_108_864  # 64 MB


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
    providers: list[Provider]


@dataclasses.dataclass(frozen=True)
class _OpaConfig:
    enabled: bool
    url: str
    policy_path: str
    full_policy_path: str
    timeout: int
    max_body_size: int
    fail_open: bool


def _read_config() -> dict:
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _load_opa_config(cfg: dict) -> _OpaConfig:
    opa = cfg.get("opa", {})
    url = opa.get("url", _DEFAULT_OPA_URL)
    return _OpaConfig(
        enabled=opa.get("enabled", True),
        url=url,
        policy_path=opa.get("policy_path", "/v1/data/agentbox/allow"),
        full_policy_path=opa.get("full_policy_path", "/v1/data/agentbox"),
        timeout=opa.get("timeout", _DEFAULT_OPA_TIMEOUT),
        max_body_size=opa.get("max_body_size", _DEFAULT_MAX_BODY_SIZE),
        fail_open=opa.get("fail_open", False),
    )


class AgentboxAddon:
    def __init__(self):
        cfg = _read_config()
        self._providers = self._load_providers(cfg)
        self._raw_providers = cfg.get("providers", [])
        self._cfg = _Config(providers=self._providers)
        self._opa = _load_opa_config(cfg)
        self._opa_allow_url = self._opa.url + self._opa.policy_path
        self._opa_full_url = self._opa.url + self._opa.full_policy_path
        log.info({"message": "Config loaded",
                  "providers": len(self._cfg.providers),
                  "opa_enabled": self._opa.enabled,
                  "opa_url": self._opa.url})

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
        self._check_opa_health()

    def _check_opa_health(self) -> None:
        if not self._opa.enabled:
            return
        try:
            req = urllib.request.Request(self._opa.url + "/health", method="GET")
            with urllib.request.urlopen(req, timeout=2):
                log.info({"message": "OPA health check passed"})
        except Exception as exc:
            log.warning("OPA not reachable at startup (will retry per-request): %s", exc)

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
                new_cfg = _Config(providers=providers)
                self._cfg = new_cfg
                log.info({"message": "Config reloaded: providers rebuilt",
                          "providers": len(new_cfg.providers)})
            else:
                log.info({"message": "Config reloaded: no provider changes"})

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

        # Provider matching first — LLM inference is always allowed
        for provider in cfg.providers:
            if provider.matches(flow):
                provider.inject(flow)
                flow.metadata["agentbox_provider"] = provider.name
                break

        ct = flow.request.headers.get("content-type", "")
        if _is_streamable_content_type(ct):
            flow.request.stream = True

        # If a provider matched, no OPA query needed
        if flow.metadata.get("agentbox_provider"):
            return

        # If OPA disabled, deny non-provider requests
        if not self._opa.enabled:
            self._block_request(flow)
            return

        # For bodyless requests or streaming content, evaluate OPA now (metadata-only)
        if flow.request.method in ("GET", "HEAD", "DELETE", "OPTIONS") or flow.request.stream:
            opa_input = self._build_opa_input(flow, body=None)
            allowed, opa_meta = self._query_opa(opa_input)
            if not allowed:
                if opa_meta.get("opa_decision") == "deny":
                    opa_meta["opa_denial_reasons"] = self._query_opa_reasons(opa_input)
                self._block_request(flow, opa_meta)
                return
            flow.metadata["opa_meta"] = opa_meta

    def request(self, flow: http.HTTPFlow) -> None:
        if flow.metadata.get("agentbox_blocked"):
            return
        if flow.metadata.get("agentbox_provider"):
            return
        if flow.metadata.get("opa_meta"):
            return  # Already evaluated in requestheaders (bodyless/streaming)

        if not self._opa.enabled:
            self._block_request(flow)
            return

        body = self._parse_body(flow)
        opa_input = self._build_opa_input(flow, body=body)
        allowed, opa_meta = self._query_opa(opa_input)
        if not allowed:
            if opa_meta.get("opa_decision") == "deny":
                opa_meta["opa_denial_reasons"] = self._query_opa_reasons(opa_input)
            self._block_request(flow, opa_meta)
            return
        flow.metadata["opa_meta"] = opa_meta

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
        opa_meta = flow.metadata.get("opa_meta")
        if opa_meta:
            entry.update(opa_meta)
        if _log_req_hdr:
            entry["req_headers"] = dict(flow.request.headers)
        if _log_resp_hdr and resp:
            entry["resp_headers"] = dict(resp.headers)
        if _log_bodies:
            entry["req_body"]  = flow.request.get_text(strict=False)
            entry["resp_body"] = resp.get_text(strict=False) if resp else None
        log.info(entry)

    # ── OPA query methods ────────────────────────────────────────────────────

    def _query_opa(self, opa_input: dict) -> tuple[bool, dict]:
        start = time.monotonic()
        try:
            data = json.dumps(opa_input).encode()
            req = urllib.request.Request(
                self._opa_allow_url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self._opa.timeout) as resp:
                result = json.loads(resp.read())
                elapsed = (time.monotonic() - start) * 1000
                allowed = result.get("result", False)
                meta = {
                    "opa_decision": "allow" if allowed else "deny",
                    "opa_duration_ms": round(elapsed, 1),
                }
                return allowed, meta
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            log.error("OPA query failed (denying request): %s", exc)
            meta = {
                "opa_decision": "error",
                "opa_error": str(exc),
                "opa_duration_ms": round(elapsed, 1),
            }
            if self._opa.fail_open:
                return True, meta
            return False, meta

    def _query_opa_reasons(self, opa_input: dict) -> list[str]:
        try:
            data = json.dumps(opa_input).encode()
            req = urllib.request.Request(
                self._opa_full_url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self._opa.timeout) as resp:
                result = json.loads(resp.read())
                reasons = result.get("result", {}).get("denial_reasons", [])
                return sorted(reasons)
        except Exception:
            return []

    # ── Body parsing ─────────────────────────────────────────────────────────

    def _parse_body(self, flow: http.HTTPFlow) -> dict | None:
        ct = flow.request.headers.get("content-type", "")
        raw = flow.request.get_content(strict=False)

        if raw is None or len(raw) == 0:
            return None
        if len(raw) > self._opa.max_body_size:
            return None

        ct_lower = ct.lower().split(";")[0].strip()

        try:
            if ct_lower == "application/json":
                return json.loads(raw)
            elif ct_lower == "application/x-www-form-urlencoded":
                return dict(urllib.parse.parse_qs(raw.decode("utf-8", errors="replace")))
            elif ct_lower in ("text/xml", "application/xml"):
                import xmltodict
                return xmltodict.parse(raw)
            elif ct_lower in ("application/x-yaml", "text/yaml", "text/x-yaml"):
                return yaml.safe_load(raw)
            elif ct_lower == "text/plain":
                return {"text": raw.decode("utf-8", errors="replace")}
            elif ct_lower.startswith("multipart/form-data"):
                return self._parse_multipart(flow)
            else:
                return None
        except Exception as exc:
            log.debug("Body parse failed for %s: %s", ct_lower, exc)
            return None

    def _parse_multipart(self, flow: http.HTTPFlow) -> dict | None:
        parts = {}
        multipart = flow.request.multipart_form
        if not multipart:
            return None
        for key, value in multipart.items():
            k = key.decode("utf-8", errors="replace") if isinstance(key, bytes) else key
            try:
                parts[k] = value.decode("utf-8", errors="strict")
            except (UnicodeDecodeError, AttributeError):
                parts[k] = {"binary": True, "size": len(value) if value else 0}
        return parts

    # ── OPA input document ───────────────────────────────────────────────────

    def _build_opa_input(self, flow: http.HTTPFlow, body: dict | None = None) -> dict:
        raw_path = flow.request.path
        path = raw_path.split("?", 1)[0]
        query_string = raw_path.split("?", 1)[1] if "?" in raw_path else ""

        headers = {}
        for k, v in flow.request.headers.items():
            lk = k.lower()
            if lk in _REDACTED_HEADERS:
                headers[lk] = "[REDACTED]"
            else:
                headers[lk] = v

        ct = flow.request.headers.get("content-type", "")
        raw = flow.request.get_content(strict=False)
        content_length = flow.request.headers.get("content-length")
        body_size = int(content_length) if content_length and content_length.isdigit() else None

        body_available = body is not None
        body_parse_error = None
        if raw and len(raw) > 0 and body is None:
            if len(raw) > self._opa.max_body_size:
                body_parse_error = "body exceeds size limit"

        request_doc: dict = {
            "host": flow.request.pretty_host,
            "port": flow.request.port,
            "path": path,
            "query_params": dict(urllib.parse.parse_qs(query_string)) if query_string else {},
            "raw_path": raw_path,
            "method": flow.request.method.upper(),
            "headers": headers,
            "content_type": ct,
            "body_size": body_size,
            "body_available": body_available,
            "body_parse_error": body_parse_error,
        }

        if body_available:
            request_doc["body"] = body

        return {"input": {"request": request_doc}}

    # ── Request blocking ─────────────────────────────────────────────────────

    def _block_request(self, flow: http.HTTPFlow, opa_meta: dict | None = None) -> None:
        host = flow.request.pretty_host

        if opa_meta and "opa_denial_reasons" in opa_meta and opa_meta["opa_denial_reasons"]:
            reasons = opa_meta["opa_denial_reasons"]
            reason_text = "\n".join(f"  - {r}" for r in reasons)
            body = f"Request to '{host}' denied by policy.\nReasons:\n{reason_text}\n"
        else:
            body = f"Host '{host}' not allowed.\n"
        body += f"To allow it, run the following command outside of the sandbox: agentbox allow{_name_flag} {host}\n"

        flow.response = http.Response.make(403, body, {"Content-Type": "text/plain"})
        flow.metadata["agentbox_blocked"] = True

        log_entry: dict = {
            "method": flow.request.method.upper(),
            "url": flow.request.pretty_url,
            "status": 403,
            "blocked": True,
            "request_host": host,
            "request_port": flow.request.port,
            "request_path": flow.request.path.split("?", 1)[0],
            "request_method": flow.request.method.upper(),
        }
        if opa_meta:
            log_entry.update(opa_meta)
        else:
            log_entry["blocked_reason"] = "no matching policy rule"
        log.info(log_entry)


addons = [AgentboxAddon()]
