# mitmproxy addon: allowlist enforcement, credential injection, and JSON access logging.
import fnmatch, json, logging, os, sys, time
import yaml
from mitmproxy import http

_addon_dir = os.path.dirname(os.path.abspath(__file__))
if _addon_dir not in sys.path:
    sys.path.insert(0, _addon_dir)

from provider import Provider
from resolvers import RESOLVER_CLASSES


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

with open("/config/proxy.yaml") as f:
    cfg = yaml.safe_load(f)

_session_name = os.environ.get("AGENTBOX_NAME", "")
_name_flag = f" --name {_session_name}" if _session_name else ""

lcfg = cfg.get("logging", {})
_log_req_hdr  = lcfg.get("log_request_headers", True)
_log_resp_hdr = lcfg.get("log_response_headers", False)
_log_bodies   = lcfg.get("log_bodies", False)


class AgentboxAddon:
    def __init__(self):
        self._allowed: list[str] = []
        self._providers: list[Provider] = []

        for p in cfg.get("providers", []):
            if not p.get("enabled"):
                continue
            self._allowed.extend(p.get("allowed_hosts", []))
            cred_type = p.get("credential_type")
            if cred_type:
                resolver_cls = RESOLVER_CLASSES.get(cred_type)
                if resolver_cls:
                    resolver = resolver_cls(p)
                    self._providers.append(Provider(p, resolver))
                else:
                    log.error("Unknown credential_type '%s' for provider '%s'",
                              cred_type, p.get("name", "?"))

        self._allowed.extend(cfg.get("extra_allowed_hosts", []))

    def request(self, flow: http.HTTPFlow) -> None:
        host = flow.request.pretty_host
        if not any(fnmatch.fnmatch(host, p) for p in self._allowed):
            flow.response = http.Response.make(
                403, f"Host '{host}' not allowed.\nAdd it: agentbox allow{_name_flag} {host}\n",
                {"Content-Type": "text/plain"},
            )
            flow.metadata["agentbox_blocked"] = True
            log.info({
                "method": flow.request.method,
                "url": flow.request.pretty_url, "status": 403, "blocked": True,
            })
            return

        for provider in self._providers:
            if provider.matches(flow):
                provider.inject(flow)
                return

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
