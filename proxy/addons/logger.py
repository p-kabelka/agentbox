# mitmproxy addon: JSON access log → /var/log/proxy/access.log
import json
import os
import time

import yaml
from mitmproxy import http  # type: ignore[import-untyped]

with open("/config/proxy.yaml") as _f:
    _lcfg = yaml.safe_load(_f).get("logging", {})

_log_path     = _lcfg.get("file", "/var/log/proxy/access.log")
_log_req_hdr  = _lcfg.get("log_request_headers", True)
_log_resp_hdr = _lcfg.get("log_response_headers", False)
_log_bodies   = _lcfg.get("log_bodies", False)

os.makedirs(os.path.dirname(_log_path), exist_ok=True)
_log_file = open(_log_path, "a", buffering=1)  # line-buffered


class AccessLogger:
    def response(self, flow: http.HTTPFlow) -> None:
        resp = flow.response
        entry: dict = {
            "ts":          time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "method":      flow.request.method,
            "url":         flow.request.pretty_url,
            "status":      resp.status_code if resp else None,
            "duration_ms": (
                round(
                    (resp.timestamp_end - flow.request.timestamp_start) * 1000
                )
                if resp and resp.timestamp_end
                else None
            ),
        }
        if _log_req_hdr:
            entry["req_headers"] = dict(flow.request.headers)
        if _log_resp_hdr and resp:
            entry["resp_headers"] = dict(resp.headers)
        if _log_bodies:
            entry["req_body"]  = flow.request.get_text(strict=False)
            entry["resp_body"] = resp.get_text(strict=False) if resp else None

        _log_file.write(json.dumps(entry) + "\n")

    def request(self, flow: http.HTTPFlow) -> None:
        # Blocked requests never reach response(); log them here
        if flow.response and flow.response.status_code == 403:
            entry = {
                "ts":     time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "method": flow.request.method,
                "url":    flow.request.pretty_url,
                "status": 403,
                "blocked": True,
            }
            _log_file.write(json.dumps(entry) + "\n")


addons = [AccessLogger()]
