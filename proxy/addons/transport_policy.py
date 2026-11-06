"""Host-wide upstream protocol selection, independent of request policies."""

import dataclasses
import os
import re


@dataclasses.dataclass(frozen=True)
class CompiledHost:
    host_re: re.Pattern
    port: int | re.Pattern

    def matches(self, host: str, port: int) -> bool:
        if not self.host_re.fullmatch(host):
            return False
        if isinstance(self.port, int):
            return self.port == port
        return bool(self.port.fullmatch(str(port)))


def compile_http1_hosts(hosts: list[dict]) -> list[CompiledHost]:
    if not isinstance(hosts, list):
        raise ValueError("force_http1_hosts must be a list")
    result = []
    for host in hosts:
        if (not isinstance(host, dict) or set(host) - {"host", "port"}
                or not isinstance(host.get("host"), str) or not host["host"]):
            raise ValueError("force_http1_hosts entries require a host regex and optional port")
        port = host.get("port", 443)
        if (isinstance(port, bool) or not isinstance(port, (int, str))
                or (isinstance(port, int) and not 1 <= port <= 65535)
                or (isinstance(port, str) and not port)):
            raise ValueError("force_http1_hosts port must be a port number or regex")
        try:
            host_re = re.compile(os.path.expandvars(host["host"]))
            if isinstance(port, str):
                port = re.compile(os.path.expandvars(port))
        except re.error as exc:
            raise ValueError("Invalid force_http1_hosts regex") from exc
        result.append(CompiledHost(host_re, port))
    return result
