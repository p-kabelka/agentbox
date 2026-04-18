# mitmproxy addon: credential injection + allowlist enforcement.
import fnmatch
import logging
import os

import yaml
from mitmproxy import http  # type: ignore[import-untyped]

log = logging.getLogger(__name__)


class CredentialInjector:
    def __init__(self):
        with open("/config/proxy.yaml") as f:
            cfg = yaml.safe_load(f)

        self._inject_rules: list[tuple[list[str], str, str]] = []
        self._allowed_patterns: list[str] = []

        for provider in cfg.get("providers", []):
            if not provider.get("enabled"):
                continue

            hosts: list[str] = provider.get("allowed_hosts", [])
            self._allowed_patterns.extend(hosts)

            env_key = provider.get("api_key_env", "")
            api_key = os.environ.get(env_key, "").strip() if env_key else ""
            header = provider.get("inject_header", "")
            prefix = provider.get("inject_prefix", "")

            if header and api_key:
                self._inject_rules.append((hosts, header, f"{prefix}{api_key}"))
            elif header and not api_key and env_key:
                log.warning(
                    "Provider '%s' is enabled but %s is not set",
                    provider.get("name", "?"), env_key,
                )

        self._allowed_patterns.extend(cfg.get("extra_allowed_hosts", []))

        log.info(
            "injector: %d inject rules, %d allowed patterns",
            len(self._inject_rules),
            len(self._allowed_patterns),
        )

    def _is_allowed(self, host: str) -> bool:
        return any(fnmatch.fnmatch(host, p) for p in self._allowed_patterns)

    def _find_rule(self, host: str) -> tuple[str, str] | tuple[None, None]:
        for patterns, header, value in self._inject_rules:
            if any(fnmatch.fnmatch(host, p) for p in patterns):
                return header, value
        return None, None

    def request(self, flow: http.HTTPFlow) -> None:
        host = flow.request.pretty_host

        if not self._is_allowed(host):
            log.warning(
                "BLOCKED %s %s://%s%s",
                flow.request.method,
                flow.request.scheme,
                host,
                flow.request.path,
            )
            flow.response = http.Response.make(
                403,
                (
                    f"Host '{host}' is not in the allowed list.\n"
                    f"Add it with:  sandbox allow {host}\n"
                    f"Or edit:      .sandbox/sessions/<name>/config/proxy.yaml → extra_allowed_hosts\n"
                    f"Then restart: podman compose restart proxy\n"
                ),
                {"Content-Type": "text/plain"},
            )
            return

        header, value = self._find_rule(host)
        if header and value:
            flow.request.headers[header] = value
            log.debug("Injected %s for %s", header, host)


addons = [CredentialInjector()]
