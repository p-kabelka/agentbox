#!/usr/bin/env python3
# Fake GCE metadata server; started when vertex.metadata_server: true in proxy.yaml.
import json
import logging
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Lock

import google.auth
import google.auth.exceptions
import google.auth.transport.requests

logging.basicConfig(level=logging.INFO, format="[metadata] %(message)s")
log = logging.getLogger(__name__)

PORT        = int(os.environ.get("METADATA_PORT", "9090"))
PROJECT_ID  = os.environ.get("VERTEX_PROJECT_ID", "unknown-project")
SCOPES      = ["https://www.googleapis.com/auth/cloud-platform"]

try:
    credentials, detected_project = google.auth.default(scopes=SCOPES)
    if detected_project and PROJECT_ID == "unknown-project":
        PROJECT_ID = detected_project
    log.info("Loaded credentials (project=%s)", PROJECT_ID)
except google.auth.exceptions.DefaultCredentialsError as exc:
    log.error("Failed to load credentials: %s", exc)
    log.error(
        "Mount a service account key at %s or set GOOGLE_APPLICATION_CREDENTIALS",
        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "/run/secrets/credentials.json"),
    )
    raise SystemExit(1)

_lock = Lock()


def _fresh_token() -> dict:
    with _lock:
        credentials.refresh(google.auth.transport.requests.Request())  # type: ignore[union-attr]
        return {
            "access_token": credentials.token,  # type: ignore[union-attr]
            "expires_in":   3599,
            "token_type":   "Bearer",
        }


def _sa_email() -> str:
    creds_file = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    try:
        with open(creds_file) as f:
            return json.load(f).get("client_email", "unknown@unknown.iam.gserviceaccount.com")
    except Exception:
        return "unknown@unknown.iam.gserviceaccount.com"


class MetadataHandler(BaseHTTPRequestHandler):
    def _reply(self, status: int, body: bytes | str, ctype: str = "application/json") -> None:
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("metadata-flavor", "Google")  # required by auth library
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?")[0].rstrip("/")
        log.info("GET %s", path)

        # Root ping — auth library sends this to detect the metadata server
        if path in ("", "/"):
            self._reply(200, b"ok", "text/plain")
        elif "service-accounts/default/token" in path:
            self._reply(200, json.dumps(_fresh_token()).encode())

        elif "service-accounts/default/email" in path:
            self._reply(200, _sa_email(), "text/plain")

        elif "service-accounts/default" in path or "service-accounts/" in path:
            self._reply(200, json.dumps({"default": {}}).encode())

        elif "project/project-id" in path:
            self._reply(200, PROJECT_ID, "text/plain")

        elif "project/numeric-project-id" in path:
            self._reply(200, "0", "text/plain")

        elif "instance/zone" in path:
            self._reply(200, f"projects/0/zones/{os.environ.get('VERTEX_REGION', 'us-east5')}-a", "text/plain")

        else:
            log.warning("Unhandled metadata path: %s", path)
            self._reply(404, b"not found", "text/plain")

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        del format, args  # suppress default access log


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), MetadataHandler)
    log.info("Listening on :%d  (project=%s)", PORT, PROJECT_ID)
    server.serve_forever()
