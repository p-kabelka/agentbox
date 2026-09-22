import dataclasses
import logging
import os
import re

from mitmproxy import http
from resolvers import CredentialResolver

log = logging.getLogger("proxy")

_INJECTION_FIELDS = {
    "api_key_env",
    "api_key_file",
    "inject_header",
    "inject_prefix",
    "replace_token",
}


@dataclasses.dataclass(frozen=True)
class CompiledRule:
    host_re: re.Pattern
    port: int | re.Pattern
    path_res: list[re.Pattern]
    methods: frozenset[str]


@dataclasses.dataclass(frozen=True)
class InjectionPolicy:
    header: str
    prefix: str
    replace_token: str | None
    resolver: CredentialResolver

    def matches(self, headers) -> bool:
        if not self.replace_token:
            return True
        current = headers.get(self.header, "")
        return current == f"{self.prefix}{self.replace_token}"

    def inject(self, flow: http.HTTPFlow) -> bool:
        value = self.resolver.resolve()
        if not value:
            return False
        flow.request.headers[self.header] = f"{self.prefix}{value}"
        return True


def _resolver_base_config(config: dict) -> dict:
    return {
        key: value for key, value in config.items()
        if key not in _INJECTION_FIELDS
        and key not in {"injection_policy", "request_policy"}
    }


def _build_injection_policies(
    policy_configs: list[dict],
    base_config: dict,
    resolver_cls: type[CredentialResolver],
) -> list[InjectionPolicy]:
    policies: list[InjectionPolicy] = []
    for policy_config in policy_configs:
        merged_config = {**base_config, **policy_config}
        policies.append(InjectionPolicy(
            header=merged_config.get("inject_header", "Authorization"),
            prefix=merged_config.get("inject_prefix", ""),
            replace_token=merged_config.get("replace_token"),
            resolver=resolver_cls(merged_config),
        ))
    return policies


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
    def __init__(self, config: dict, resolver_cls: type[CredentialResolver]):
        self.name = config.get("name", "unknown")
        resolver_base_config = _resolver_base_config(config)

        policy_configs = config.get("injection_policy")
        if policy_configs is None:
            policy_configs = [config]
            base_config = {}
        else:
            base_config = resolver_base_config.copy()
            base_config["_require_namespaced_secret"] = True
        self._injection_policies = _build_injection_policies(
            policy_configs, base_config, resolver_cls
        )

        self._rules: list[CompiledRule] = []
        self._rule_injection_policies: list[tuple[CompiledRule, list[InjectionPolicy]]] = []
        rule_base_config = {**resolver_base_config, "_require_namespaced_secret": True}
        for rule_config in config.get("request_policy", []):
            rule = compile_rule(rule_config)
            if rule is None:
                continue
            self._rules.append(rule)
            rule_policies = _build_injection_policies(
                rule_config.get("injection_policy", []),
                rule_base_config,
                resolver_cls,
            )
            self._rule_injection_policies.append((rule, rule_policies))

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

        return True

    def inject(self, flow: http.HTTPFlow, original_headers=None) -> bool:
        headers = original_headers if original_headers is not None else flow.request.headers.copy()
        injected = False
        for policy in self._injection_policies:
            if policy.matches(headers) and policy.inject(flow):
                injected = True

        host = flow.request.pretty_host
        port = flow.request.port
        path = flow.request.path.split("?", 1)[0]
        method = flow.request.method.upper()
        for rule, policies in self._rule_injection_policies:
            if not rule_matches(rule, host, port, path, method):
                continue
            for policy in policies:
                if policy.matches(headers) and policy.inject(flow):
                    injected = True
        return injected


def inject_matching_providers(providers: list[Provider], flow: http.HTTPFlow) -> list[str]:
    original_headers = flow.request.headers.copy()
    injected: list[str] = []
    for provider in providers:
        if provider.matches(flow) and provider.inject(flow, original_headers):
            injected.append(provider.name)
    return injected
