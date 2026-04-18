# mitmproxy addon: allowlist enforcement, credential injection, and JSON access logging.
import fnmatch, json, logging, os, time
import yaml
from mitmproxy import http

log = logging.getLogger(__name__)

with open("/config/proxy.yaml") as f:
    cfg = yaml.safe_load(f)

lcfg = cfg.get("logging", {})
os.makedirs("/var/log/proxy", exist_ok=True)
_log = open(lcfg.get("file", "/var/log/proxy/access.log"), "a", buffering=1)
_log_req_hdr  = lcfg.get("log_request_headers", True)
_log_resp_hdr = lcfg.get("log_response_headers", False)
_log_bodies   = lcfg.get("log_bodies", False)


class SandboxAddon:
    def __init__(self):
        self._allowed, self._rules = [], []
        for p in cfg.get("providers", []):
            if not p.get("enabled"):
                continue
            hosts = p.get("allowed_hosts", [])
            self._allowed.extend(hosts)
            key = os.environ.get(p.get("api_key_env", ""), "").strip()
            if p.get("inject_header") and key:
                self._rules.append((hosts, p["inject_header"], p.get("inject_prefix", "") + key))
            elif p.get("inject_header") and not key and p.get("api_key_env"):
                log.warning("Provider '%s' enabled but %s is not set",
                            p.get("name", "?"), p["api_key_env"])
        self._allowed.extend(cfg.get("extra_allowed_hosts", []))

    def request(self, flow: http.HTTPFlow) -> None:
        host = flow.request.pretty_host
        if not any(fnmatch.fnmatch(host, p) for p in self._allowed):
            flow.response = http.Response.make(
                403, f"Host '{host}' not allowed.\nAdd it: sandbox allow {host}\n",
                {"Content-Type": "text/plain"},
            )
            flow.metadata["sandbox_blocked"] = True
            _log.write(json.dumps({
                "ts": _ts(), "method": flow.request.method,
                "url": flow.request.pretty_url, "status": 403, "blocked": True,
            }) + "\n")
            return
        for patterns, header, value in self._rules:
            if any(fnmatch.fnmatch(host, p) for p in patterns):
                flow.request.headers[header] = value

    def response(self, flow: http.HTTPFlow) -> None:
        if flow.metadata.get("sandbox_blocked"):
            return  # already logged in request()
        resp = flow.response
        entry: dict = {
            "ts": _ts(), "method": flow.request.method,
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
        _log.write(json.dumps(entry) + "\n")


def _ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


addons = [SandboxAddon()]
