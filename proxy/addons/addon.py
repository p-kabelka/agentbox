# mitmproxy addon: allowlist enforcement, credential injection, and JSON access logging.
import fnmatch, json, logging, os, sys, threading, time
import yaml
from mitmproxy import http


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

# Must match the access_token returned by metadata_server.py
_MOCK_TOKEN = "dummy-replaced-by-proxy"


class AgentboxAddon:
    def __init__(self):
        self._allowed        = []
        self._rules          = []   # (patterns, header, value) — static key injection
        self._vertex_hosts   = []   # patterns for Google APIs (allowlist)
        self._vertex_project = ""
        self._vertex_region  = ""
        self._vertex_creds   = None # (credentials, request) — refreshed on demand
        self._vertex_lock    = threading.Lock()

        for p in cfg.get("providers", []):
            if not p.get("enabled"):
                continue
            hosts = p.get("allowed_hosts", [])
            self._allowed.extend(hosts)
            if p.get("name") == "vertex":
                self._vertex_hosts   = hosts
                self._vertex_project = os.environ.get("VERTEX_PROJECT_ID", "")
                self._vertex_region  = os.environ.get("VERTEX_REGION", "us-east5")
                self._load_vertex_creds()
            else:
                key = os.environ.get(p.get("api_key_env", ""), "").strip()
                if p.get("inject_header") and key:
                    self._rules.append((hosts, p["inject_header"], p.get("inject_prefix", "") + key))
                elif p.get("inject_header") and not key and p.get("api_key_env"):
                    log.warning("Provider '%s' enabled but %s is not set",
                                p.get("name", "?"), p["api_key_env"])
        self._allowed.extend(cfg.get("extra_allowed_hosts", []))

    def _load_vertex_creds(self) -> None:
        try:
            import google.auth
            import google.auth.transport.requests
            # If GOOGLE_APPLICATION_CREDENTIALS points to a missing file, clear it
            # so google.auth.default() falls through to the well-known ADC path.
            cenv = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
            if cenv and not os.path.isfile(cenv):
                del os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
            creds, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            self._vertex_creds = (creds, google.auth.transport.requests.Request())
            log.info("Loaded Google credentials for Vertex injection (type=%s)",
                     type(creds).__name__)
        except Exception as exc:
            log.error("Failed to load Google credentials for Vertex: %s", exc)

    def _vertex_token(self) -> str | None:
        if not self._vertex_creds:
            return None
        creds, req = self._vertex_creds
        with self._vertex_lock:
            if not creds.valid:
                creds.refresh(req)
            return creds.token

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

        # Vertex: replace mock token with a real one on inference requests only
        if self._vertex_hosts and any(fnmatch.fnmatch(host, p) for p in self._vertex_hosts):
            expected_host = f"{self._vertex_region}-aiplatform.googleapis.com"
            prefix = (f"/v1/projects/{self._vertex_project}"
                      f"/locations/{self._vertex_region}"
                      f"/publishers/anthropic/models/")
            auth = flow.request.headers.get("Authorization", "")
            if (host == expected_host
                    and flow.request.path.startswith(prefix)
                    and auth == f"Bearer {_MOCK_TOKEN}"):
                if token := self._vertex_token():
                    flow.request.headers["Authorization"] = f"Bearer {token}"
            return

        # Other providers: inject static API key
        for patterns, header, value in self._rules:
            if any(fnmatch.fnmatch(host, p) for p in patterns):
                flow.request.headers[header] = value

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
