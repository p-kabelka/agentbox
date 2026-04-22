import fnmatch
import os

from mitmproxy import http
from resolvers import CredentialResolver


class Provider:
    def __init__(self, config: dict, resolver: CredentialResolver):
        self.name = config.get("name", "unknown")
        self.allowed_hosts: list[str] = config.get("allowed_hosts", [])
        self.path_prefixes: list[str] = [
            os.path.expandvars(p) for p in config.get("path_prefixes", [])
        ]
        self._header = config.get("inject_header", "Authorization")
        self._prefix = config.get("inject_prefix", "")
        self._replace_token = config.get("replace_token")
        self._resolver = resolver

    def matches(self, flow: http.HTTPFlow) -> bool:
        host = flow.request.pretty_host
        if not any(fnmatch.fnmatch(host, p) for p in self.allowed_hosts):
            return False
        if self.path_prefixes and not any(
            flow.request.path.startswith(p) for p in self.path_prefixes
        ):
            return False
        if self._replace_token:
            current = flow.request.headers.get(self._header, "")
            expected = f"{self._prefix}{self._replace_token}"
            if current != expected:
                return False
        return True

    def inject(self, flow: http.HTTPFlow) -> None:
        value = self._resolver.resolve()
        if value:
            flow.request.headers[self._header] = f"{self._prefix}{value}"
