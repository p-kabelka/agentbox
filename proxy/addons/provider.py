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
    available: bool
    source: str

    def matches(self, headers) -> bool:
        if not self.replace_token:
            return True
        current = headers.get(self.header, "")
        return current == f"{self.prefix}{self.replace_token}"

    def resolve(self) -> tuple[str, str]:
        if not self.available:
            raise CredentialUnavailable
        try:
            value = self.resolver.resolve()
        except Exception:
            raise CredentialUnavailable from None
        if not value:
            raise CredentialUnavailable
        return self.header, f"{self.prefix}{value}"


class CredentialUnavailable(Exception):
    """A matching policy cannot supply a credential; never includes its contents."""


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
    if not isinstance(policy_configs, list):
        raise ValueError("injection_policy must be a list")
    for policy_config in policy_configs:
        if not isinstance(policy_config, dict):
            raise ValueError("injection policy must be a mapping")
        merged_config = {**base_config, **policy_config}
        for key in _INJECTION_FIELDS:
            if key in {"api_key_file", "api_key_env", "replace_token"} and merged_config.get(key) is None:
                continue
            if key in merged_config and not isinstance(merged_config[key], str):
                raise ValueError("Invalid injection policy field")
        header = merged_config.get("inject_header", "Authorization")
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", header):
            raise ValueError("Invalid injection header")
        if any(c in merged_config.get("inject_prefix", "") for c in "\r\n"):
            raise ValueError("Invalid injection prefix")
        resolver = resolver_cls(merged_config)
        policies.append(InjectionPolicy(
            header=header,
            prefix=merged_config.get("inject_prefix", ""),
            replace_token=merged_config.get("replace_token"),
            resolver=resolver,
            available=resolver.available,
            source=merged_config.get("api_key_file", ""),
        ))
    return policies


def compile_rule(rule_dict: dict) -> CompiledRule | None:
    host_raw = rule_dict.get("host")
    if not host_raw:
        log.warning("Rule missing required 'host' field, skipping")
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
        if not isinstance(config.get("request_policy", []), list):
            raise ValueError("request_policy must be a list")
        resolver_base_config = _resolver_base_config(config)

        policy_configs = config.get("injection_policy")
        if policy_configs is None:
            policy_configs = [config]
            base_config = {}
        else:
            base_config = resolver_base_config
        self._injection_policies = _build_injection_policies(
            policy_configs, base_config, resolver_cls
        )

        self._rules: list[CompiledRule] = []
        self._rule_injection_policies: list[tuple[CompiledRule, list[InjectionPolicy]]] = []
        for rule_config in config.get("request_policy", []):
            rule = compile_rule(rule_config)
            if rule is None:
                continue
            self._rules.append(rule)
            rule_policies = _build_injection_policies(
                rule_config.get("injection_policy", []),
                resolver_base_config,
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

    @property
    def secret_sources(self) -> list[str]:
        policies = self._injection_policies + [p for _, group in self._rule_injection_policies for p in group]
        return [p.source for p in policies if p.source]

    @property
    def unavailable(self) -> list[dict]:
        result = []
        groups = [("provider", self._injection_policies)]
        groups.extend((f"rule[{i}]", policies)
                      for i, (_, policies) in enumerate(self._rule_injection_policies))
        for scope, policies in groups:
            for i, policy in enumerate(policies):
                if not policy.available:
                    result.append({"provider": self.name, "policy": f"{scope}[{i}]"})
        return result

    def resolve_injections(self, flow: http.HTTPFlow, original_headers=None) -> list[tuple[str, str]]:
        headers = original_headers if original_headers is not None else flow.request.headers.copy()
        resolved = []
        for policy in self._injection_policies:
            if policy.matches(headers):
                resolved.append(policy.resolve())

        host = flow.request.pretty_host
        port = flow.request.port
        path = flow.request.path.split("?", 1)[0]
        method = flow.request.method.upper()
        for rule, policies in self._rule_injection_policies:
            if not rule_matches(rule, host, port, path, method):
                continue
            for policy in policies:
                if policy.matches(headers):
                    resolved.append(policy.resolve())
        return resolved

    def inject(self, flow: http.HTTPFlow, original_headers=None) -> bool:
        resolved = self.resolve_injections(flow, original_headers)
        _commit_injections(flow, resolved)
        return bool(resolved)


def _commit_injections(flow: http.HTTPFlow, resolved: list[tuple[str, str]]) -> None:
    for header, value in resolved:
        flow.request.headers[header] = value
    if resolved and hasattr(flow, "metadata"):
        # Flow metadata is serialized by mitmweb; keep it JSON/msgpack-compatible.
        redacted = set(flow.metadata.get("agentbox_injected_headers", []))
        redacted.update(header.lower() for header, _ in resolved)
        flow.metadata["agentbox_injected_headers"] = sorted(redacted)


def inject_matching_providers(providers: list[Provider], flow: http.HTTPFlow) -> list[str]:
    original_headers = flow.request.headers.copy()
    injected: list[str] = []
    resolved = []
    for provider in providers:
        if provider.matches(flow):
            pending = provider.resolve_injections(flow, original_headers)
            if pending:
                resolved.extend(pending)
                injected.append(provider.name)
    _commit_injections(flow, resolved)
    return injected
