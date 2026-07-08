import dataclasses
import logging
import os
import re

from mitmproxy import http
from resolvers import CredentialResolver

log = logging.getLogger("proxy")


@dataclasses.dataclass(frozen=True)
class CompiledRule:
    host_re: re.Pattern
    port: int | re.Pattern
    path_res: list[re.Pattern]
    methods: frozenset[str]


def compile_rule(rule_dict: dict) -> CompiledRule | None:
    host_raw = rule_dict.get("host")
    if not host_raw:
        log.warning("Rule missing required 'host' field, skipping: %s", rule_dict)
        return None

    host_raw = os.path.expandvars(str(host_raw))
    try:
        host_re = re.compile(host_raw)
    except re.error as exc:
        log.warning("Invalid host regex %r, skipping rule: %s", host_raw, exc)
        return None

    port_raw = rule_dict.get("port", 443)
    if isinstance(port_raw, int):
        port = port_raw
    else:
        port_str = os.path.expandvars(str(port_raw))
        try:
            port = re.compile(port_str)
        except re.error as exc:
            log.warning("Invalid port regex %r, skipping rule: %s", port_str, exc)
            return None

    path_res: list[re.Pattern] = []
    for p in rule_dict.get("paths", [".*"]):
        p = os.path.expandvars(str(p))
        try:
            path_res.append(re.compile(p))
        except re.error as exc:
            log.warning("Invalid path regex %r, skipping pattern: %s", p, exc)
    if not path_res:
        log.warning("All path patterns invalid for rule with host %r, skipping rule", host_raw)
        return None

    methods_raw = rule_dict.get("methods", [])
    methods = frozenset(m.upper() for m in methods_raw)

    return CompiledRule(host_re=host_re, port=port, path_res=path_res, methods=methods)


def compile_rules(rules: list[dict]) -> list[CompiledRule]:
    compiled: list[CompiledRule] = []
    for i, rule_dict in enumerate(rules):
        rule = compile_rule(rule_dict)
        if rule is not None:
            compiled.append(rule)
    return compiled


def rule_matches(rule: CompiledRule, host: str, port: int, path: str, method: str) -> bool:
    if not rule.host_re.fullmatch(host):
        return False
    if isinstance(rule.port, int):
        if port != rule.port:
            return False
    else:
        if not rule.port.fullmatch(str(port)):
            return False
    if not any(p.match(path) for p in rule.path_res):
        return False
    if rule.methods and method not in rule.methods:
        return False
    return True


class Provider:
    def __init__(self, config: dict, resolver: CredentialResolver):
        self.name = config.get("name", "unknown")
        self._header = config.get("inject_header", "Authorization")
        self._prefix = config.get("inject_prefix", "")
        self._replace_token = config.get("replace_token")
        self._resolver = resolver
        self._rules = compile_rules(config.get("request_policy", []))

    def matches(self, flow: http.HTTPFlow) -> bool:
        host = flow.request.pretty_host
        port = flow.request.port
        path = flow.request.path.split("?", 1)[0]
        method = flow.request.method.upper()

        rule_matched = False
        for rule in self._rules:
            if rule_matches(rule, host, port, path, method):
                rule_matched = True
                break

        if not rule_matched:
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
