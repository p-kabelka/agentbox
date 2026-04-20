#!/usr/bin/env python3
# Minimal fake GCE metadata server — returns dummy tokens so the agent's Google
# auth library can initialize. Real OAuth tokens are injected by the mitmproxy
# addon before requests reach Google's servers.
import json
import logging
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


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


log = logging.getLogger("metadata")
log.setLevel(logging.INFO)
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(JSONFormatter("metadata"))
log.addHandler(_handler)
log.propagate = False

PORT       = int(os.environ.get("METADATA_PORT", "9090"))
PROJECT_ID = os.environ.get("VERTEX_PROJECT_ID", "")
REGION     = os.environ.get("VERTEX_REGION", "us-east5")


class MetadataHandler(BaseHTTPRequestHandler):
    def _reply(self, status: int, body: bytes | str, ctype: str = "application/json") -> None:
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("metadata-flavor", "Google")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?")[0].rstrip("/")
        if path in ("", "/"):
            self._reply(200, b"ok", "text/plain")
        elif "service-accounts/default/token" in path:
            self._reply(200, json.dumps({
                "access_token": "dummy-replaced-by-proxy",
                "expires_in":   3599,
                "token_type":   "Bearer",
            }).encode())
        elif "service-accounts/default/email" in path:
            self._reply(200, "agent@sandbox.local", "text/plain")
        elif "service-accounts/default" in path or "service-accounts/" in path:
            self._reply(200, json.dumps({"default": {}}).encode())
        elif "project/project-id" in path:
            self._reply(200, PROJECT_ID, "text/plain")
        elif "project/numeric-project-id" in path:
            self._reply(200, "0", "text/plain")
        elif "instance/zone" in path:
            self._reply(200, f"projects/0/zones/{REGION}-a", "text/plain")
        elif path.endswith("/instance"):
            self._reply(200, json.dumps({
                "zone": f"projects/0/zones/{REGION}-a",
            }))
        else:
            log.warning({"method": "GET", "path": path, "status": 404})
            self._reply(404, b"not found", "text/plain")
            return
        log.info({"method": "GET", "path": path, "status": 200})

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        del format, args


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), MetadataHandler)
    log.info("Listening on :%d (project=%s)", PORT, PROJECT_ID)
    server.serve_forever()
