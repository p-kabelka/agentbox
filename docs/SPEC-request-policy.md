# L7 Request Policy — Feature Specification

## 1. Summary

Replace the proxy's current flat hostname-based allowlist and fnmatch-based provider matching with a unified L7 request policy system. The new system enables filtering and credential injection scoped by **hostname**, **port**, **path**, and **HTTP method**, using **Python regex** pattern matching. It resolves the current limitations where:

- Hostnames are matched without port, so the same hostname on different ports cannot be distinguished.
- Provider `path_prefixes` cannot be bound to a specific host+port combination, causing ambiguous matches when multiple hostnames share path patterns.
- There is no HTTP method filtering — a matched endpoint allows all methods.
- `fnmatch` glob patterns lack the expressiveness needed for complex path matching.

---

## 2. DSL / Library Evaluation

Before designing a custom solution, the following existing systems were evaluated:

| Candidate | Fit | Verdict |
|-----------|-----|---------|
| **OPA / Rego** | General-purpose policy engine. Requires a separate runtime or embedding via WASM/REST. Powerful but extreme overkill for 5-10 providers with 3-10 rules each. Adds ~50MB dependency and a learning curve for contributors. | Reject |
| **Casbin** | Authorization DSL with Python bindings. Designed for RBAC/ABAC; its model (subject, object, action) maps poorly to HTTP request fields (host, port, path, method). Configuration in `.conf` + `.csv` files is an awkward fit alongside the existing YAML presets. | Reject |
| **CEL (Common Expression Language)** | Google's expression language. Python bindings (`cel-python`) exist but are not widely adopted, poorly maintained, and add a non-trivial dependency. The expressions would be embedded in YAML strings, creating a language-within-a-language. | Reject |
| **mitmproxy flowfilter** | mitmproxy's built-in filter DSL (e.g., `~d example.com & ~u /api/.*`). Designed for interactive use, not programmatic policy. No support for port matching in the filter syntax. Not composable with credential injection logic. | Reject |
| **Python `re` module** | Standard library. Zero additional dependencies. The existing matching code is 13 lines of `fnmatch`; upgrading to `re.compile()` patterns is a minimal, auditable change. Regex is universally understood by the target audience (developers configuring security proxies). | **Accept** |

**Decision:** Use Python's built-in `re` module. The scope of the matching logic (host+port+path+method against a list of compiled regex patterns) does not justify an external policy engine. The current implementation is ~13 lines; the new one will be ~30-40 lines. Adding an external dependency would increase the attack surface of a security-critical component for no practical benefit.

---

## 3. Current Architecture (What Changes)

### 3.1 Files Modified

| File | Current Role | Change |
|------|-------------|--------|
| `proxy/addons/addon.py` | Allowlist enforcement + provider dispatch | Replace flat `allowed: list[str]` with compiled request policy; update `requestheaders()` to evaluate policies |
| `proxy/addons/provider.py` | Provider matching (`fnmatch` on host, path) + credential injection | Remove `fnmatch` usage; replace `matches()` with compiled regex rules matching host+port+path+method; replace `allowed_hosts`/`path_prefixes` with `request_policy` |
| `presets/*/proxy.yaml` | Provider config with `allowed_hosts` (hostname globs) and `path_prefixes` (path globs) | Replace with `request_policy` rules and `extra_request_policy`; remove `allowed_hosts`, `path_prefixes`, and `extra_allowed_hosts` |
| `bin/agentbox` | `allow`/`deny` commands modify `extra_allowed_hosts` | Update to read/write `extra_request_policy` instead |
| `docs/SPEC.md` | Documents current allowlist behavior | Update to reflect new request policy |
| `docs/ARCHITECTURE.md` | Documents current design | Update Layer 3 (Credential) description |

### 3.2 Files NOT Modified

| File | Reason |
|------|--------|
| `proxy/addons/resolvers.py` | Credential resolution is orthogonal to request matching. No changes. |
| `proxy/metadata_server.py` | Fake metadata server is unaffected by policy changes. |
| `proxy/start.sh` | Proxy entrypoint is unaffected. |
| `compose-base.yaml` | Network topology is unaffected. |
| `agent/*` | Agent container is unaffected — it has no knowledge of proxy policy. |

---

## 4. New Configuration Schema

### 4.1 Provider-Level Request Policy

Each provider gains a `request_policy` field that replaces the current `allowed_hosts` + `path_prefixes` pair. The `request_policy` is a list of rules, each specifying the L7 attributes to match.

```yaml
providers:
  - name: anthropic
    enabled: true
    credential_type: static
    api_key_env: ANTHROPIC_API_KEY
    inject_header: x-api-key
    inject_prefix: ""
    request_policy:
      - host: "api\\.anthropic\\.com"
        paths:
          - "/v1/messages(/.*)?$"
          - "/v1/complete$"
          - "/v1/models(/.*)?$"
        methods: [POST, GET]
```

### 4.2 Rule Fields

Each rule in the `request_policy` list has the following fields:

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `host` | `string` | Yes | — | Python regex pattern matched against the request hostname. Anchored with implicit `^` and `$` (full match). |
| `port` | `int` or `string` | No | `443` | Port number or regex pattern. When an integer, exact match. When a string, treated as a regex pattern (anchored). Default is `443` since all proxy traffic is HTTPS. |
| `paths` | `list[string]` | No | `[".*"]` (match all) | List of Python regex patterns matched against the request path (without query string). Each pattern is anchored with implicit `^`. Not anchored with `$` by default — the pattern matches if it matches a prefix of the path unless the user explicitly adds `$`. |
| `methods` | `list[string]` | No | `[]` (match all) | List of HTTP methods (uppercase). Empty list means all methods are allowed. Exact string match, not regex. |

### 4.3 Pattern Matching Semantics

All regex patterns use Python `re` syntax. Patterns support environment variable expansion via `os.path.expandvars()` before compilation (preserving existing behavior for Vertex-style `${VERTEX_PROJECT_ID}` patterns).

**Host patterns** are matched against `flow.request.pretty_host` (the hostname as seen on the wire, after SNI/Host-header resolution by mitmproxy). The match is **full-match** (implicit `^...$` anchoring).

**Port** is matched against `flow.request.port` (integer). When `port` is an integer in YAML, it is compared with `==`. When `port` is a string, it is treated as a regex applied to the string representation of the port number (full match). This allows patterns like `"8[0-9]{3}"` to match a range of ports.

**Path patterns** are matched against `flow.request.path` (the path component only, without query string). Before matching, the query string is stripped from `flow.request.path` (split on `?`, take the first part). Patterns are anchored at the start with implicit `^` but NOT at the end. To require an exact path match, the user must add `$` explicitly. This makes the common case (`/v1/messages` matching `/v1/messages` and `/v1/messages/batch`) work naturally.

**Method matching** is an exact case-insensitive comparison against the list. If the `methods` list is empty or omitted, all methods match. Methods are normalized to uppercase before comparison.

### 4.4 `extra_request_policy`

The top-level `extra_allowed_hosts` field is replaced by `extra_request_policy`. It uses the same rule schema as provider-level `request_policy`, but rules under `extra_request_policy` only control **egress allowlisting** — they never trigger credential injection.

```yaml
extra_request_policy:
  - host: "pypi\\.org"
  - host: ".*\\.fedoraproject\\.org"
```

For ergonomic CLI usage, `agentbox allow` and `agentbox deny` continue to accept a single hostname argument and translate it into a rule with only a `host` field (escaping dots for regex safety, i.e., `pypi.org` becomes `host: "pypi\\.org"`).

The old `allowed_hosts`, `path_prefixes`, and `extra_allowed_hosts` fields are removed. All presets and user configurations must use the new `request_policy` and `extra_request_policy` format.

### 4.5 Full Preset Examples

**Anthropic Direct API:**

```yaml
- name: anthropic
  enabled: true
  credential_type: static
  api_key_env: ANTHROPIC_API_KEY
  inject_header: x-api-key
  inject_prefix: ""
  request_policy:
    - host: "api\\.anthropic\\.com"
      paths:
        - "/v1/messages(/.*)?$"
        - "/v1/complete$"
        - "/v1/models(/.*)?$"
      methods: [POST, GET]
```

**Vertex AI (with env var expansion):**

```yaml
- name: vertex
  enabled: true
  credential_type: oauth
  metadata_server: true
  inject_header: Authorization
  inject_prefix: "Bearer "
  replace_token: "dummy-replaced-by-proxy"
  request_policy:
    - host: "(.*-)?aiplatform\\.googleapis\\.com"
      paths:
        - "/v1/projects/${VERTEX_PROJECT_ID}/locations/${VERTEX_REGION}/publishers/anthropic/models/.*"
```

**Cursor (multiple hosts, different path policies per host):**

```yaml
- name: cursor
  enabled: true
  credential_type: static
  api_key_file: ~/.keys/cursor-token
  inject_header: Authorization
  inject_prefix: "Bearer "
  replace_token: "eyJhbG..."
  request_policy:
    - host: "api2\\.cursor\\.sh"
      paths:
        - "/aiserver\\.v1\\..*"
        - "/agent\\.v1\\..*"
    - host: "agentn\\.us\\.api5\\.cursor\\.sh"
      paths:
        - "/aiserver\\.v1\\..*"
        - "/agent\\.v1\\..*"
    - host: "repo42\\.cursor\\.sh"
      paths:
        - "/v1/traces$"
```

**Provider with host+port binding (new capability):**

```yaml
- name: internal-llm
  enabled: true
  credential_type: static
  api_key_env: INTERNAL_LLM_KEY
  inject_header: Authorization
  inject_prefix: "Bearer "
  request_policy:
    - host: "llm\\.internal\\.corp\\.com"
      port: 8443
      paths:
        - "/v1/chat/completions$"
      methods: [POST]
    - host: "llm\\.internal\\.corp\\.com"
      port: 9443
      paths:
        - "/v1/embeddings$"
      methods: [POST]
```

---

## 5. Implementation Design

### 5.1 Compiled Rule Representation

At provider construction time, each rule in `request_policy` is compiled into a `CompiledRule` object:

```python
@dataclasses.dataclass(frozen=True)
class CompiledRule:
    host_re: re.Pattern        # compiled from rule["host"], anchored full-match
    port: int | re.Pattern     # int for exact match, compiled pattern for regex
    path_res: list[re.Pattern] # compiled from rule["paths"], anchored at start
    methods: frozenset[str]    # uppercase method names; empty = match all
```

Compilation happens once at startup (and on hot-reload). Runtime matching is purely against pre-compiled `re.Pattern` objects — no string compilation in the request hot path.

### 5.2 Provider Matching Logic

The `Provider.matches()` method is updated:

The `replace_token` check remains as a separate guard after rule matching, preserving the Vertex/Cursor dummy token mechanism. The method returns `True` only if both a rule matched AND the replace_token check passes (if configured):

```python
def matches(self, flow: http.HTTPFlow) -> bool:
    host = flow.request.pretty_host
    port = flow.request.port
    path = flow.request.path.split("?", 1)[0]
    method = flow.request.method.upper()

    rule_matched = False
    for rule in self._rules:
        if not rule.host_re.fullmatch(host):
            continue
        if isinstance(rule.port, int):
            if port != rule.port:
                continue
        else:
            if not rule.port.fullmatch(str(port)):
                continue
        if not any(p.match(path) for p in rule.path_res):
            continue
        if rule.methods and method not in rule.methods:
            continue
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
```

### 5.3 Allowlist Enforcement

The `AgentboxAddon.requestheaders()` method currently checks a flat `list[str]` of allowed hosts using `fnmatch`. This is replaced with a unified policy check:

1. Build a combined list of `CompiledRule` objects from all enabled providers' `request_policy` rules plus all `extra_request_policy` rules.
2. On each request, check if **any** combined rule matches the request's host+port+path+method.
3. If no rule matches, return HTTP 403.
4. If a rule matches (request is allowed), proceed to provider matching for credential injection.

This means the allowlist and provider matching now use the **same rule format and matching engine**, eliminating the current inconsistency where the allowlist uses hostname-only fnmatch and providers use host+path fnmatch.

```python
@dataclasses.dataclass(frozen=True)
class _Config:
    allowed_rules: list[CompiledRule]   # combined allowlist (was: allowed: list[str])
    providers: list[Provider]

# In requestheaders():
def requestheaders(self, flow: http.HTTPFlow) -> None:
    cfg = self._cfg
    host = flow.request.pretty_host
    port = flow.request.port
    path = flow.request.path.split("?", 1)[0]
    method = flow.request.method.upper()

    if not any(rule_matches(rule, host, port, path, method) for rule in cfg.allowed_rules):
        flow.response = http.Response.make(403, ...)
        flow.metadata["agentbox_blocked"] = True
        return

    for provider in cfg.providers:
        if provider.matches(flow):
            provider.inject(flow)
            break
```

The `rule_matches()` function is a shared utility used by both the allowlist check and `Provider.matches()` to avoid duplicating the matching logic.

### 5.4 Hot-Reload

The current implementation only reloads the allowlist on hot-reload, not the providers. This was an intentional design choice: `OAuthResolver.__init__()` calls `google.auth.default()`, which scans the credential chain (environment variables, credential files, potentially network calls) and can block for seconds. Since the reload handler is a coroutine on the same asyncio event loop that processes all addon hooks, a blocking resolver init stalls the entire event loop — no new requests are processed and in-flight requests waiting for upstream responses are delayed.

Swapping `self._cfg` does not risk dropping in-flight requests. mitmproxy runs all addon hooks on a single event loop thread, and `requestheaders()` is synchronous, so it executes atomically — no interleaving with the async reload handler is possible. Once `requestheaders()` returns (having injected credentials into `flow.request.headers`), the flow's state is carried in the `HTTPFlow` object and is unaffected by subsequent `_cfg` swaps. The `response()` hook does not reference `_cfg`.

The hot-reload mechanism is updated to reload **both** the allowlist rules and the providers, but with a separation to preserve fast reloads for the common case:

**Fast path (allowlist-only reload):** When only `extra_request_policy` has changed (the `agentbox allow`/`deny` case), rebuild the combined allowlist using the existing providers' compiled rules plus the new `extra_request_policy` rules. No resolver re-initialization. This is the path triggered by `agentbox allow`/`deny`.

**Full reload:** When provider configuration has changed, re-build providers including resolver initialization. This path is triggered by `agentbox proxy-reload` (explicit full reload). To avoid stalling the event loop, `OAuthResolver.__init__()` should be run in an executor (`loop.run_in_executor()`) so the credential chain scan does not block request processing.

On reload (both paths):
1. Re-read `proxy.yaml`.
2. Detect whether provider configuration has changed (compare provider sections).
3. If providers changed (full reload): re-build providers with new resolvers, compile new `request_policy` rules. Use `run_in_executor` for blocking resolver init.
4. If only `extra_request_policy` changed (fast path): reuse existing providers, rebuild combined allowlist only.
5. Atomically swap `self._cfg`.

### 5.5 `agentbox allow` / `agentbox deny` CLI

The CLI commands are updated:

**`agentbox allow <host>`** — Adds a rule to `extra_request_policy`:
```python
def _update_allowlist(host: str, add: bool, args) -> None:
    # ...
    rules: list = cfg.setdefault("extra_request_policy", [])
    escaped_host = re.escape(host)  # escape dots, etc. for regex safety
    if add:
        if not any(r.get("host") == escaped_host for r in rules):
            rules.append({"host": escaped_host})
            save_yaml(config_path, cfg)
    else:
        rules = [r for r in rules if r.get("host") != escaped_host]
        cfg["extra_request_policy"] = rules
        save_yaml(config_path, cfg)
    # trigger reload (unchanged)
    compose(session_dir, "exec", "-T", "proxy", "curl", "-sf", "http://localhost:8082/reload", check=True)
```

The `host` argument is automatically escaped using `re.escape()` so that `agentbox allow pypi.org` produces `host: "pypi\\.org"` rather than a regex that matches `pypiXorg`.

**`agentbox allow <host>:<port>`** — If the argument contains a colon followed by digits, the port is extracted and added as a separate field:
```python
# "registry.internal.com:8443" -> {"host": "registry\\.internal\\.com", "port": 8443}
```

### 5.6 Provider `path_prefixes` Binding to Specific Hosts

A key improvement: each rule in `request_policy` binds its `paths` to a specific `host` (and optionally `port`). This eliminates the current problem where `path_prefixes` apply across all of a provider's `allowed_hosts`.

**Current problem example:**
```yaml
# Current config — path_prefixes apply to ALL allowed_hosts
allowed_hosts:
  - "api2.cursor.sh"
  - "repo42.cursor.sh"
path_prefixes:
  - "/aiserver.v1.*"
  - "/v1/traces"
# Problem: /v1/traces is meant only for repo42.cursor.sh but currently
# also matches on api2.cursor.sh. And /aiserver.v1.* is meant for
# api2.cursor.sh but currently also matches on repo42.cursor.sh.
```

**New config — each rule binds paths to a specific host:**
```yaml
request_policy:
  - host: "api2\\.cursor\\.sh"
    paths:
      - "/aiserver\\.v1\\..*"
      - "/agent\\.v1\\..*"
  - host: "repo42\\.cursor\\.sh"
    paths:
      - "/v1/traces$"
```

**Multi-host path sharing is still supported** — use a regex alternation or multiple rules:
```yaml
# Option A: regex alternation in host pattern
request_policy:
  - host: "(api2|agentn\\.us\\.api5)\\.cursor\\.sh"
    paths:
      - "/aiserver\\.v1\\..*"

# Option B: multiple rules with the same paths
request_policy:
  - host: "api2\\.cursor\\.sh"
    paths:
      - "/aiserver\\.v1\\..*"
  - host: "agentn\\.us\\.api5\\.cursor\\.sh"
    paths:
      - "/aiserver\\.v1\\..*"
```

---

## 6. Validation

### 6.1 Startup Validation

At proxy startup (and on hot-reload), the following validation is performed on each rule:

1. **`host` is required and must be a valid regex.** If `re.compile()` raises `re.error`, log an error with the provider name and rule index, and skip the rule.
2. **`port` must be a valid integer or a valid regex string.** Invalid values log an error and skip the rule.
3. **Each entry in `paths` must be a valid regex.** Invalid patterns log an error and skip the individual path pattern (not the entire rule, unless all paths are invalid).
4. **Each entry in `methods` must be a recognized HTTP method** (`GET`, `POST`, `PUT`, `DELETE`, `PATCH`, `HEAD`, `OPTIONS`, `CONNECT`, `TRACE`). Unrecognized methods log a warning but are still accepted (to support custom methods).
5. **Environment variable expansion** is applied to all string fields before regex compilation (`os.path.expandvars()`).

Invalid rules are skipped with a log warning, not a fatal error — this prevents a single typo from breaking the entire proxy. The provider is still loaded with its remaining valid rules.

### 6.2 Configuration Validation in `agentbox init`

The CLI should validate `request_policy` rules at `agentbox init` time (before the proxy container starts) to catch obvious errors early. Validation includes:

- All regex patterns are syntactically valid.
- Required fields (`host`) are present.
- `port` values are valid.
- `methods` values are uppercase strings.

Validation errors are printed as warnings, not fatal errors, to match the current behavior where `proxy.yaml` is user-owned and the CLI does not prevent startup on configuration issues.

---

## 7. Logging

### 7.1 Blocked Request Logging

Blocked requests (HTTP 403) now include the reason for blocking in the log entry:

```json
{
  "ts": "2026-06-25T12:00:00Z",
  "source": "proxy",
  "method": "POST",
  "url": "https://evil.com:443/exfil",
  "status": 403,
  "blocked": true,
  "blocked_reason": "no matching policy rule",
  "request_host": "evil.com",
  "request_port": 443,
  "request_path": "/exfil",
  "request_method": "POST"
}
```

### 7.2 Provider Match Logging

When a provider matches and injects credentials, the log entry includes which provider matched:

```json
{
  "ts": "2026-06-25T12:00:01Z",
  "source": "proxy",
  "method": "POST",
  "url": "https://api.anthropic.com/v1/messages",
  "status": 200,
  "provider": "anthropic",
  "duration_ms": 1234
}
```

---

## 8. Security Considerations

### 8.1 Regex Denial of Service (ReDoS)

User-supplied regex patterns could theoretically cause catastrophic backtracking. Mitigations:

1. Patterns are compiled at startup, not per-request. A slow compilation is acceptable.
2. For runtime matching, patterns are applied to short strings (hostnames ~50 chars, paths ~200 chars, ports ~5 chars). ReDoS on short inputs is practically difficult to trigger.
3. Document in the preset template that patterns should be simple and avoid nested quantifiers. No runtime timeout is implemented — the risk is low given the input lengths.

### 8.2 Anchoring

Host patterns are always applied with `re.fullmatch()` (equivalent to `^...$` anchoring). This prevents a pattern like `api\.anthropic\.com` from matching `evil-api.anthropic.com.attacker.com`.

Path patterns are applied with `re.match()` (anchored at start with implicit `^`). This means `/v1/messages` matches `/v1/messages` and `/v1/messages/batch` but not `/evil/v1/messages`. Users who want exact path matching must add `$`.

Port patterns (when string) are applied with `re.fullmatch()`.

### 8.3 Default-Deny

The system remains default-deny. A request that does not match any rule in the combined allowlist (provider rules + extra_request_policy) is blocked with HTTP 403. There is no implicit allow.

---

## 9. Implementation Plan

Ordered list of changes to implement this feature:

### Step 1: Add `CompiledRule` and `rule_matches()` to `provider.py`

- Define the `CompiledRule` dataclass.
- Implement `compile_rule(rule_dict: dict) -> CompiledRule` that takes a raw YAML rule dict, expands env vars, compiles regex patterns, and returns a `CompiledRule`.
- Implement `rule_matches(rule: CompiledRule, host: str, port: int, path: str, method: str) -> bool` as a standalone function.
- Add `compile_rules(rules: list[dict]) -> list[CompiledRule]` for batch compilation with error handling.

### Step 2: Update `Provider.__init__()` and `Provider.matches()`

- In `__init__()`: read `request_policy` from config and compile rules. Remove `allowed_hosts` and `path_prefixes` handling.
- Replace the `matches()` method body to use compiled rules and `rule_matches()`.
- Preserve the `replace_token` check logic.

### Step 3: Update `_Config` and `AgentboxAddon`

- Change `_Config.allowed` from `list[str]` to `list[CompiledRule]` (rename to `allowed_rules`).
- Replace `_load_allowed_hosts()` with `_load_allowed_rules()`: build the combined allowlist from all enabled providers' compiled rules plus `extra_request_policy` rules.
- Update `requestheaders()` to use `rule_matches()` for the allowlist check.
- Update `_handle_reload_conn()` to support two reload paths: fast path (allowlist-only, reuses existing providers) for `agentbox allow`/`deny`, and full reload (re-builds providers with `run_in_executor` for blocking resolver init) for `agentbox proxy-reload`.
- Implement provider config change detection (compare raw provider YAML sections) to determine which reload path to use.

### Step 4: Update `agentbox` CLI

- Update `_update_allowlist()` to read/write `extra_request_policy` instead of `extra_allowed_hosts`.
- Escape the host argument with `re.escape()`.
- Support `host:port` syntax for the allow/deny commands.

### Step 5: Migrate built-in presets

- Convert `presets/default/proxy.yaml` to use `request_policy`.
- Convert `presets/cursor/proxy.yaml` to use `request_policy`.
- Convert `presets/claude-vertex/proxy.yaml` to use `request_policy`.
- Convert `presets/opencode-vertex/proxy.yaml` to use `request_policy`.
- Remove all `extra_allowed_hosts` lists and replace with `extra_request_policy` rules.

### Step 6: Update documentation

- Update `docs/SPEC.md` sections 4.3 (Traffic Mediation) and 6.3 (Provider Configuration).
- Update `docs/ARCHITECTURE.md` section 4.2 (Proxy Container) and 6.6 (Configuration over code).
- Add comments in preset YAML files documenting the new rule format.

### Step 7: Testing

- Test regex matching for host, port, path, method.
- Test `agentbox allow`/`deny` with new format.
- Test hot-reload with provider changes.
- Test edge cases: missing fields, invalid regex, env var expansion, ReDoS-resistant inputs.
- Test that anchoring prevents partial host matches (security-critical).
