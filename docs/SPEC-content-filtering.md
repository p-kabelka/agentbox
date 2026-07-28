# OPA/Rego Content Filtering — Feature Specification

## 1. Summary

Replace the proxy's `request_policy` (Python regex rules compiled in the addon) with an external **OPA (Open Policy Agent) server** that evaluates **Rego policies** against every outbound request. The proxy addon retains sole responsibility for **credential injection** (token rotation, dummy token replacement). All request **filtering** — allow, deny, and content-level inspection — moves to OPA.

This separation of concerns produces two independently deployable modules:

| Module | Responsibility | Changes |
|--------|---------------|---------|
| **Proxy addon** (mitmproxy) | Credential injection, request/response streaming, access logging | Loses `request_policy` matching; gains OPA query step |
| **OPA server** | Request filtering: allow/deny based on host, port, path, method, headers, and parsed body content | New component |

The current `request_policy` system matches requests against compiled Python regex rules for host, port, path, and method. It cannot inspect request bodies, cannot express cross-field constraints (e.g., "POST to this path only if `body.metadata.name` starts with `agent-`"), and couples policy logic to the proxy's Python process. OPA/Rego removes all three limitations.

For content types that can be parsed into structured data (JSON, form-encoded, XML, YAML), the addon converts the body to a JSON object and includes it in the OPA input document. Rego policies can then filter on any field at any depth. For content types that cannot be meaningfully parsed (binary formats, protobuf, gRPC streams), the addon passes only **request metadata** (host, port, path, method, headers, content type, body size) so that OPA can still apply coarse-grained filtering. For content types that have known structure but require specialized parsing (e.g., multipart form data), the addon extracts what it can (part names, text parts) and passes the rest as opaque metadata.

---

## 2. Motivation

### 2.1 Why Replace `request_policy`

The current `request_policy` system (specified in [SPEC-request-policy.md](SPEC-request-policy.md)) was explicitly designed as a lightweight L7 filter using Python regex. It was the right choice for its scope — host/port/path/method matching for a handful of providers. But the requirements have grown:

1. **Body-level filtering.** "Allow POST to the Jira API only if `issue_key == PROJ-123`" or "allow K8s resource creation only if `metadata.name` starts with `agent-`." The current system cannot express these constraints — it never reads the request body.

2. **Dynamic policy updates without proxy restart.** The current hot-reload mechanism re-reads `proxy.yaml` and recompiles regex patterns. OPA's remote server mode supports **hot-reload natively** — policies update on the OPA server and take effect on the next query without touching the proxy.

3. **Policy as a separate concern.** Embedding filtering rules in `proxy.yaml` couples policy to the proxy deployment. An external policy engine allows policies to be managed, versioned, tested, and audited independently of the proxy configuration.

4. **Expressiveness ceiling.** Regex rules cannot express logical conjunctions across different request fields (e.g., "this path AND this body field AND this method"). Rego is a purpose-built policy language with set operations, string manipulation, object traversal, and partial evaluation.

### 2.2 Why OPA/Rego

The research report (Section 4) evaluated three policy formats: YAML, OPA/Rego, and Cedar. OPA/Rego is selected for the PoC based on:

| Criterion | OPA/Rego |
|-----------|----------|
| Body inspection | Yes — any JSON path |
| Hot-reload | Yes — remote server mode, sub-millisecond evaluation |
| Expressiveness | Full — `startswith`, `regex.match`, set membership, object traversal, custom functions |
| Ecosystem | Mature — CNCF graduated, production-proven (Envoy, Kubernetes, Terraform) |
| Integration | REST API — language-agnostic, trivial to call from Python |
| Learning curve | Medium — Rego is unfamiliar but well-documented |

Cedar was considered for its formal verification guarantees but intentionally omits regex support (a safety trade-off), making it unsuitable for the path-matching patterns already established in the presets. Cedar is a candidate for a future production hardening phase.

### 2.3 What Stays the Same

- **Credential injection** remains in the proxy addon. `Provider.matches()`, `Provider.inject()`, `CredentialResolver`, `StaticKeyResolver`, `OAuthResolver` — all untouched.
- **Network-level isolation** (agent container has no direct external access) — unchanged.
- **Streaming detection** for protobuf/gRPC content types — unchanged.
- **Access logging** — unchanged (extended with OPA decision metadata).
- **`agentbox allow`/`deny` CLI** — updated to write OPA-compatible policy instead of `extra_request_policy` rules.
- **Hot-reload mechanism** — the existing TCP reload endpoint on port 8082 continues to work for provider/credential changes. OPA policy changes do not require proxy reload at all.

---

## 3. Architecture

### 3.1 Request Flow

```
[Agent Container]
       |
       | outbound HTTPS request
       v
[mitmproxy addon]
  1. TLS termination (existing)
  2. Extract request metadata (host, port, path, method, headers)
  3. Read and parse request body (if parseable content type)
  4. Build OPA input document
  5. POST to OPA: /v1/data/agentbox/allow
  6. If OPA returns allow=false: respond HTTP 403 to agent, stop
  7. If OPA returns allow=true: proceed to credential injection
  8. Provider matching + credential injection (existing, unchanged)
  9. Forward request upstream
       |
       v
[External API]
```

```
[OPA Server]
  - Runs as a sidecar container on the same internal network
  - Loads policies from /policies/ volume (mounted from host)
  - Watches policy files for changes (--watch flag)
  - Evaluates /v1/data/agentbox/allow on each query
  - Returns {"result": true/false} with optional denial reason
```

### 3.2 Component Topology

```
                                    [Policy Files]
                                    /policies/*.rego
                                         |
                                         | (volume mount, watched)
                                         v
[Agent Container] --> [mitmproxy + addon] --> [OPA Server :8181]
   (sandboxed)         port 8080               (sidecar container)
                       - TLS termination
                       - body parsing
                       - OPA query
                       - credential injection        [External APIs]
                       - access logging          <--- (upstream)
```

The OPA server runs on `agent-net` (`internal: true` — no internet route), the same network as the agent and proxy. The proxy can reach OPA directly because it is on `agent-net`. The agent is also on `agent-net` but cannot query OPA directly — all agent HTTP traffic is routed through mitmproxy via `HTTPS_PROXY`/`HTTP_PROXY` environment variables, and OPA's hostname is not in any allowlist, so mitmproxy blocks the request. No TLS is needed for the proxy-to-OPA hop since `agent-net` has no external exposure.

### 3.3 Separation of Concerns

| Concern | Before (current) | After (this spec) |
|---------|------------------|-------------------|
| **Allow/deny decision** | `request_policy` regex rules in `proxy.yaml`, evaluated by `rule_matches()` in Python | Rego policies on OPA server, evaluated via REST API |
| **Body inspection** | Not possible | Addon parses body, includes in OPA input; Rego inspects any field |
| **Credential injection** | `Provider.matches()` + `Provider.inject()` | Unchanged — `Provider.matches()` + `Provider.inject()` |
| **Policy hot-reload** | Re-read `proxy.yaml`, recompile regex, swap `_Config` | OPA watches policy files, reloads automatically; proxy is unaware |
| **Allowlist management** | `extra_request_policy` in `proxy.yaml` + `agentbox allow/deny` CLI | `agentbox allow/deny` writes Rego rules to a managed policy file; OPA picks up changes via file watch |

---

## 4. OPA Input Document

The proxy addon constructs a JSON input document for each request and sends it to OPA. The structure of this document varies based on whether the request body can be parsed.

### 4.1 Base Input (All Requests)

Every request, regardless of content type, includes this metadata:

```json
{
  "input": {
    "request": {
      "host": "api.example.com",
      "port": 443,
      "path": "/v1/resources",
      "query_params": {
        "action": ["create"],
        "namespace": ["dev-sandbox"]
      },
      "raw_path": "/v1/resources?action=create&namespace=dev-sandbox",
      "method": "POST",
      "headers": {
        "content-type": "application/json",
        "user-agent": "python-requests/2.31.0"
      },
      "content_type": "application/json",
      "body_size": 1234,
      "body_available": true,
      "body_parse_error": null
    }
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `host` | string | `flow.request.pretty_host` — hostname after SNI/Host resolution |
| `port` | int | `flow.request.port` |
| `path` | string | Path component only, query string stripped (`split("?", 1)[0]`). Used for path-based matching without query noise. |
| `query_params` | object | Parsed query string via `urllib.parse.parse_qs(query)`. Keys map to lists of values (standard for query strings — `?a=1&a=2` → `{"a": ["1", "2"]}`). Empty object `{}` when no query string is present. |
| `raw_path` | string | Full path as received, including query string (`flow.request.path`). Available for Rego `regex.match` when a policy needs to match across path and query together. |
| `method` | string | Uppercase HTTP method |
| `headers` | object | All request headers as key-value pairs (lowercased keys). The `authorization` header is **redacted** (replaced with `"[REDACTED]"`) to prevent policies from depending on credentials. |
| `content_type` | string | Value of the `Content-Type` header, or empty string if absent |
| `body_size` | int\|null | `Content-Length` header value if present, null otherwise |
| `body_available` | bool | Whether a parsed body is included in the `body` field |
| `body_parse_error` | string\|null | If body parsing was attempted but failed, the error message; null otherwise |

### 4.2 Parsed Body (Supported Content Types)

When the request body can be parsed into structured data, the addon includes a `body` field in `input.request`:

```json
{
  "input": {
    "request": {
      "host": "api.atlassian.net",
      "port": 443,
      "path": "/rest/api/3/issue/PROJ-123",
      "query_params": {},
      "raw_path": "/rest/api/3/issue/PROJ-123",
      "method": "PUT",
      "headers": { "...": "..." },
      "content_type": "application/json",
      "body_size": 256,
      "body_available": true,
      "body_parse_error": null,
      "body": {
        "fields": {
          "summary": "Updated title",
          "description": "New description"
        }
      }
    }
  }
}
```

Supported content types and their conversion:

| Content-Type | Conversion | Notes |
|-------------|------------|-------|
| `application/json` | `json.loads(body)` | Direct JSON parse. Includes `application/json; charset=utf-8` and similar variants. |
| `application/x-www-form-urlencoded` | `urllib.parse.parse_qs(body)` → dict | Values are lists (standard for form encoding). |
| `text/xml`, `application/xml` | `xmltodict.parse(body)` → dict | Uses `xmltodict` for XML-to-dict conversion. Included in dependencies. |
| `application/x-yaml`, `text/yaml`, `text/x-yaml` | `yaml.safe_load(body)` → dict/list | YAML parsed via `yaml.safe_load` (already a dependency). |
| `multipart/form-data` | Extract text parts → dict, binary parts → metadata only | Text field values included; file parts include only `filename`, `content_type`, `size`. |
| `text/plain` | `{"text": body_string}` | Raw text wrapped in an object so Rego can apply `regex.match` or `contains`. |

### 4.3 Unparseable Body (Unsupported Content Types)

For binary formats, protobuf, gRPC, or any content type not in the supported list, the addon does **not** include a `body` field. The `body_available` flag is `false`:

```json
{
  "input": {
    "request": {
      "host": "k8s-api.internal",
      "port": 6443,
      "path": "/api/v1/namespaces/default/pods",
      "query_params": {},
      "raw_path": "/api/v1/namespaces/default/pods",
      "method": "POST",
      "headers": { "content-type": "application/x-protobuf" },
      "content_type": "application/x-protobuf",
      "body_size": 4096,
      "body_available": false,
      "body_parse_error": null
    }
  }
}
```

Rego policies can still filter these requests using metadata:

```rego
# Allow protobuf requests only to specific hosts and paths
allow if {
    input.request.content_type == "application/x-protobuf"
    input.request.host == "k8s-api.internal"
    startswith(input.request.path, "/api/v1/namespaces/dev-sandbox/")
    input.request.method == "GET"
}
```

### 4.4 Body Size Limit

To prevent the addon from buffering arbitrarily large request bodies, a configurable size limit is enforced:

- **Default: 1 MiB** (`1048576` bytes).
- If `Content-Length` exceeds the limit, the body is not read. `body_available` is `false`, `body_parse_error` is `"body exceeds size limit"`.
- If `Content-Length` is absent (chunked encoding), the body is read up to the limit. If the limit is reached before the body ends, reading stops, `body_available` is `false`, `body_parse_error` is `"body exceeds size limit"`.
- Configurable via `opa.max_body_size` in `proxy.yaml`.

### 4.5 Authorization Header Redaction

The `authorization` header (and other credential-bearing headers like `x-api-key`, `proxy-authorization`) is redacted in the input document sent to OPA. This prevents:

1. Policy authors from writing rules that depend on credential values (which would break when tokens rotate).
2. Credential values from appearing in OPA decision logs.

Redacted headers are replaced with `"[REDACTED]"`. The list of redacted header names is:

- `authorization`
- `x-api-key`
- `proxy-authorization`
- `cookie`

### 4.6 Streaming Requests

For content types identified as streamable (`_STREAMABLE_CONTENT_TYPES` — protobuf, gRPC, Connect), the addon **does not buffer or parse the body**. These requests are evaluated by OPA using metadata only. `flow.request.stream` is set to `True` as before, ensuring the body is forwarded without buffering.

This is not a limitation for the PoC — gRPC and streaming protobuf services are typically filtered by host, port, path, and method, not by body content.

---

## 5. OPA Server Configuration

### 5.1 Deployment

The OPA server runs as a sidecar container in the same Podman pod or internal network as the proxy. It is added to `compose-base.yaml`:

```yaml
opa:
  image: docker.io/openpolicyagent/opa:1-static
  command:
    - "run"
    - "--server"
    - "--addr=0.0.0.0:8181"
    - "--watch"
    - "/policies"
  volumes:
    - type: bind
      source: ${POLICY_DIR:-./policies}
      target: /policies
      read_only: true
  networks:
    - agent-net
  restart: unless-stopped
```

The OPA container is attached only to `agent-net` (`internal: true` — no internet route). It does not need internet access — policies are loaded from a local bind-mounted volume. The proxy is on both `agent-net` and `proxy-net`, so it can reach OPA over `agent-net`. The agent container is also on `agent-net` but all its traffic is routed through mitmproxy via `HTTPS_PROXY` — it cannot query OPA directly because OPA listens on plain HTTP and the agent's proxy configuration forces all HTTP traffic through mitmproxy, which would block the request (OPA's hostname is not in any allowlist).

Key flags:

- `--server`: Run as a daemon with REST API.
- `--addr=0.0.0.0:8181`: Listen on all interfaces (`agent-net` only — no external exposure).
- `--watch`: Watch the `/policies` directory for file changes and automatically reload policies.

### 5.2 Policy Directory Structure

Policies are organized in a directory mounted into the OPA container:

```
policies/
  main.rego           # Entry point: package agentbox, defines top-level allow rule
  providers.rego      # Provider-specific rules (Jira, K8s, GitHub, etc.)
  managed.rego        # Auto-generated by agentbox allow/deny CLI
  data.json           # Optional: external data (allowed ticket IDs, namespace prefixes, etc.)
```

The `--watch` flag means any file change in this directory triggers an automatic policy reload. No proxy restart or explicit reload signal is needed.

### 5.3 Policy Package Convention

All policies use the `package agentbox` package. The proxy queries `/v1/data/agentbox/allow`. The top-level `allow` rule aggregates provider-specific rules:

```rego
package agentbox

import rego.v1

default allow := false
```

OPA evaluates all `allow` rules across all files in the `agentbox` package. If any rule evaluates to `true`, the request is allowed. This follows OPA's standard incremental definition pattern — rules are additive across files.

### 5.4 OPA Query and Response

**Request from addon to OPA:**

```
POST http://opa:8181/v1/data/agentbox/allow
Content-Type: application/json

{
  "input": {
    "request": { ... }
  }
}
```

**Response from OPA (allowed):**

```json
{
  "result": true
}
```

**Response from OPA (denied):**

```json
{
  "result": false
}
```

When `result` is absent or the OPA query fails (network error, policy compilation error), the addon treats the request as **denied** (fail-closed). This is critical for security — a misconfigured OPA server must not silently allow all traffic.

### 5.5 Denial Reasons

For better debugging and agent feedback, policies can optionally populate a `denial_reasons` set:

```rego
package agentbox

import rego.v1

default allow := false

denial_reasons contains reason if {
    not _any_rule_allows
    reason := "no policy rule matched this request"
}

denial_reasons contains reason if {
    input.request.host == "api.atlassian.net"
    input.request.body.fields.issue_key != "PROJ-123"
    reason := sprintf("Jira access restricted to PROJ-123, got %s", [input.request.body.fields.issue_key])
}
```

The addon queries `/v1/data/agentbox` (the full package) when a request is denied and uses the `denial_reasons` set in the 403 response body and access log. This is a secondary query made only on denial — the hot path (allowed requests) makes a single query to `/v1/data/agentbox/allow`.

### 5.6 OPA Health Check

The addon checks OPA availability at startup and logs a warning if OPA is unreachable. The health check uses OPA's built-in endpoint:

```
GET http://opa:8181/health
```

If OPA is not reachable at startup, the proxy **refuses to start** rather than falling back to allow-all. This is a hard dependency — without OPA, no filtering is possible.

If OPA becomes unreachable after startup (container crash, network issue), all requests are **denied** (fail-closed) until OPA recovers. The addon logs an error on each failed OPA query.

---

## 6. Proxy Addon Changes

### 6.1 Files Modified

| File | Change |
|------|--------|
| `proxy/addons/addon.py` | Remove `request_policy` allowlist logic; add OPA query step in `requestheaders()`; add body parsing; add OPA health check |
| `proxy/addons/provider.py` | `Provider.matches()` simplified — no longer checks `request_policy` rules for allowlisting (OPA handles that); retains `request_policy` rules only for credential injection matching |
| `proxy/requirements.in` | Add `requests` (already present), `xmltodict` |
| `proxy/start.sh` | Wait for OPA health check before starting mitmproxy |
| `compose-base.yaml` | Add OPA sidecar service |
| `presets/*/proxy.yaml` | Add `opa` configuration section; provider `request_policy` retained for credential injection only |
| `bin/agentbox` | `allow`/`deny` commands write Rego rules to managed policy file |

### 6.2 Files NOT Modified

| File | Reason |
|------|--------|
| `proxy/addons/resolvers.py` | Credential resolution is orthogonal to filtering. |
| `proxy/metadata_server.py` | Fake metadata server is unaffected. |
| `agent/*` | Agent container has no knowledge of OPA. |

### 6.3 New `requestheaders()` / `request()` Flow

The current `requestheaders()` hook performs allowlist checking before the body is read. With OPA body inspection, the flow splits across two hooks:

**`requestheaders()` — metadata-only fast path:**

For requests with streamable content types (protobuf, gRPC) or no body (GET, HEAD, DELETE), the full OPA evaluation can happen in `requestheaders()` using metadata only. This avoids buffering.

**`request()` — body inspection path:**

For requests with a parseable body (POST, PUT, PATCH with JSON/form/XML/YAML content), the addon uses the `request()` hook (called after the full body is received) to:

1. Parse the body based on content type.
2. Build the complete OPA input document (metadata + parsed body).
3. Query OPA.
4. Allow or deny.

This two-hook approach ensures:
- Streaming requests are never buffered (evaluated on metadata in `requestheaders()`).
- Body-bearing requests are fully parsed before OPA evaluation (evaluated in `request()`).
- Credential injection still happens after OPA allows the request.

```python
def requestheaders(self, flow: http.HTTPFlow) -> None:
    cfg = self._cfg

    ct = flow.request.headers.get("content-type", "")
    if _is_streamable_content_type(ct):
        flow.request.stream = True

    # For bodyless requests or streaming content, evaluate OPA now
    if flow.request.method in ("GET", "HEAD", "DELETE", "OPTIONS") or flow.request.stream:
        opa_input = self._build_opa_input(flow, body=None)
        if not self._opa_allow(opa_input):
            self._block_request(flow, opa_input)
            return
        self._inject_credentials(flow)

    # For body-bearing requests, defer to request() hook

def request(self, flow: http.HTTPFlow) -> None:
    if flow.metadata.get("agentbox_blocked"):
        return
    if flow.metadata.get("agentbox_provider"):
        return  # Already handled in requestheaders (bodyless/streaming)

    body = self._parse_body(flow)
    opa_input = self._build_opa_input(flow, body=body)
    if not self._opa_allow(opa_input):
        self._block_request(flow, opa_input)
        return
    self._inject_credentials(flow)
```

### 6.4 OPA Client

The addon communicates with OPA using Python's `urllib.request` (standard library) rather than adding a dependency on `requests` or `httpx`. The OPA query is synchronous and blocking, which is acceptable because:

1. OPA runs locally (sidecar on the same network), so latency is sub-millisecond.
2. mitmproxy's `request()` hook is already synchronous and blocking.
3. The addon already performs synchronous operations in this hook (credential resolution via `Provider.inject()`).

```python
import json
import urllib.request

_OPA_URL = "http://opa:8181/v1/data/agentbox/allow"
_OPA_FULL_URL = "http://opa:8181/v1/data/agentbox"
_OPA_TIMEOUT = 5  # seconds

def _opa_allow(self, opa_input: dict) -> bool:
    try:
        data = json.dumps(opa_input).encode()
        req = urllib.request.Request(
            _OPA_URL,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_OPA_TIMEOUT) as resp:
            result = json.loads(resp.read())
            return result.get("result", False)
    except Exception as exc:
        log.error("OPA query failed (denying request): %s", exc)
        return False  # fail-closed
```

### 6.5 Body Parsing

Body parsing is extracted into a dedicated method. It uses a content-type dispatch table:

```python
_REDACTED_HEADERS = frozenset({"authorization", "x-api-key", "proxy-authorization", "cookie"})

_MAX_BODY_SIZE = 1_048_576  # 1 MiB default

def _parse_body(self, flow: http.HTTPFlow) -> dict | None:
    ct = flow.request.headers.get("content-type", "")
    raw = flow.request.get_content(strict=False)

    if raw is None or len(raw) == 0:
        return None
    if len(raw) > self._max_body_size:
        return None  # body_parse_error set in _build_opa_input

    ct_lower = ct.lower().split(";")[0].strip()

    if ct_lower == "application/json":
        return json.loads(raw)
    elif ct_lower == "application/x-www-form-urlencoded":
        return dict(urllib.parse.parse_qs(raw.decode("utf-8", errors="replace")))
    elif ct_lower in ("text/xml", "application/xml"):
        import xmltodict
        return xmltodict.parse(raw)
    elif ct_lower in ("application/x-yaml", "text/yaml", "text/x-yaml"):
        return yaml.safe_load(raw)
    elif ct_lower == "text/plain":
        return {"text": raw.decode("utf-8", errors="replace")}
    elif ct_lower.startswith("multipart/form-data"):
        return self._parse_multipart(flow)
    else:
        return None  # unsupported content type
```

### 6.6 Provider Matching After OPA

After OPA allows a request, the addon still needs to determine **which provider** should inject credentials. The current `Provider.matches()` method uses `request_policy` rules for this. This matching logic is **retained** — `request_policy` rules on providers continue to determine credential injection scope. OPA replaces only the **allowlist enforcement** step.

```
Before:
  request_policy rules → allowlist check (allow/deny)
  request_policy rules → provider match (which credentials to inject)
  Both use the same CompiledRule/rule_matches() engine.

After:
  OPA → allowlist check (allow/deny)
  request_policy rules → provider match (which credentials to inject)
  OPA handles filtering; request_policy handles credential routing only.
```

This means `request_policy` in `proxy.yaml` is no longer a security boundary — it only controls credential injection routing. A request can pass OPA but match no provider (allowed, no credentials injected — e.g., `extra_request_policy` equivalent). A request that fails OPA is blocked regardless of whether a provider would match.

### 6.7 Configuration Schema Addition

A new `opa` section is added to `proxy.yaml`:

```yaml
opa:
  enabled: true
  url: "http://opa:8181"
  policy_path: "/v1/data/agentbox/allow"
  full_policy_path: "/v1/data/agentbox"
  timeout: 5
  max_body_size: 1048576  # 1 MiB
  fail_open: false  # if true, allow requests when OPA is unreachable (NOT recommended)
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `true` | When `false`, no OPA queries are made — **all requests matching a provider's `request_policy` are allowed** (falls back to current behavior). For development/debugging only. |
| `url` | string | `"http://opa:8181"` | OPA server base URL. |
| `policy_path` | string | `"/v1/data/agentbox/allow"` | OPA REST API path for the allow decision. |
| `full_policy_path` | string | `"/v1/data/agentbox"` | OPA REST API path for full evaluation (used to retrieve denial_reasons on deny). |
| `timeout` | int | `5` | Timeout in seconds for OPA queries. |
| `max_body_size` | int | `1048576` | Maximum request body size to parse and send to OPA (bytes). |
| `fail_open` | bool | `false` | If `true`, allow requests when OPA is unreachable. **Not recommended** — defeats the purpose of policy enforcement. Provided for debugging only. |

---

## 7. Policy Examples

### 7.1 Basic Allowlist (Equivalent to Current `request_policy`)

This policy replicates the current regex-based allowlist behavior in Rego:

```rego
# policies/main.rego
package agentbox

import rego.v1

default allow := false

# Anthropic API
allow if {
    input.request.host == "api.anthropic.com"
    input.request.port == 443
    regex.match(`^/v1/(messages(/.*)?|complete|models(/.*)?)$`, input.request.path)
    input.request.method in {"POST", "GET"}
}

# OpenAI API
allow if {
    input.request.host == "api.openai.com"
    input.request.port == 443
    regex.match(`^/v1/(chat/completions|completions|embeddings|responses(/.*)?)$`, input.request.path)
    input.request.method in {"POST", "GET"}
}

# Vertex AI
allow if {
    regex.match(`^(.*-)?aiplatform\.googleapis\.com$`, input.request.host)
    startswith(input.request.path, "/v1/projects/")
    contains(input.request.path, "/publishers/anthropic/models/")
}
```

### 7.2 Body-Level Filtering — Jira

Allow the agent to interact with Jira but only for specific tickets:

```rego
# policies/providers.rego
package agentbox

import rego.v1

# Jira: read any issue (GET is low-risk)
allow if {
    _is_jira_host
    input.request.method == "GET"
    regex.match(`^/rest/api/3/issue/`, input.request.path)
}

# Jira: update only allowed tickets (PUT with body inspection)
allow if {
    _is_jira_host
    input.request.method == "PUT"
    regex.match(`^/rest/api/3/issue/[A-Z]+-[0-9]+$`, input.request.path)
    _jira_ticket_allowed
}

# Jira: add comment only to allowed tickets
allow if {
    _is_jira_host
    input.request.method == "POST"
    regex.match(`^/rest/api/3/issue/[A-Z]+-[0-9]+/comment$`, input.request.path)
    _jira_ticket_allowed
}

# Jira: search with JQL (POST body inspection)
allow if {
    _is_jira_host
    input.request.method == "POST"
    input.request.path == "/rest/api/3/search"
    input.request.body_available
}

# Helper: identify Jira host
_is_jira_host if {
    endswith(input.request.host, ".atlassian.net")
    input.request.port == 443
}

# Helper: check if the ticket in the URL is in the allowed set
_jira_ticket_allowed if {
    parts := split(input.request.path, "/")
    ticket_id := parts[5]  # /rest/api/3/issue/<ticket_id>
    ticket_id in data.allowed_tickets
}
```

With external data (`policies/data.json`):

```json
{
    "allowed_tickets": ["PROJ-123", "PROJ-456"]
}
```

### 7.3 Body-Level Filtering — Kubernetes

Allow the agent to manage resources in a specific namespace with a name prefix:

```rego
package agentbox

import rego.v1

# K8s: list/read in dev-sandbox namespace (GET, no body needed)
allow if {
    _is_k8s_host
    input.request.method == "GET"
    regex.match(`^/api/v1/namespaces/dev-sandbox/`, input.request.path)
}

# K8s: create resources with agent- prefix (POST with body inspection)
allow if {
    _is_k8s_host
    input.request.method == "POST"
    regex.match(`^/api/v1/namespaces/dev-sandbox/`, input.request.path)
    input.request.body_available
    startswith(input.request.body.metadata.name, "agent-")
}

# K8s: update resources with agent- prefix (PUT/PATCH with body inspection)
allow if {
    _is_k8s_host
    input.request.method in {"PUT", "PATCH"}
    regex.match(`^/api/v1/namespaces/dev-sandbox/`, input.request.path)
    input.request.body_available
    startswith(input.request.body.metadata.name, "agent-")
}

# K8s: delete resources with agent- prefix (DELETE, name in URL path)
allow if {
    _is_k8s_host
    input.request.method == "DELETE"
    regex.match(`^/api/v1/namespaces/dev-sandbox/[a-z]+/agent-`, input.request.path)
}

_is_k8s_host if {
    input.request.host == "k8s-api.internal"
    input.request.port == 6443
}
```

### 7.4 Text Body Filtering — Regex on Plain Text

For `text/plain` bodies, the addon wraps the text in `{"text": "..."}`. Rego can apply string operations:

```rego
# Allow webhook notifications only if they don't contain sensitive keywords
allow if {
    input.request.host == "hooks.slack.com"
    input.request.method == "POST"
    input.request.body_available
    not contains(input.request.body.text, "API_KEY")
    not contains(input.request.body.text, "SECRET")
    not regex.match(`[A-Za-z0-9+/]{40,}`, input.request.body.text)  # base64 blobs
}
```

### 7.5 MCP JSON-RPC Filtering

MCP uses JSON-RPC 2.0 over HTTP (SSE or Streamable HTTP transports). Since JSON-RPC is `application/json`, the existing JSON body parser handles it — no special detection or parsing is needed in the addon. The full JSON-RPC structure lands in `input.request.body`, and Rego policies filter on `method`, `params.name`, `params.arguments`, etc. directly:

```rego
package agentbox

import rego.v1

# MCP: allow tool listing and capability negotiation
allow if {
    _is_mcp_host
    input.request.body.method in {"initialize", "tools/list", "notifications/initialized"}
}

# MCP: allow specific tools only
allow if {
    _is_mcp_host
    input.request.body.method == "tools/call"
    input.request.body.params.name in data.allowed_mcp_tools
}

# MCP: allow jira_update only for specific tickets (argument inspection)
allow if {
    _is_mcp_host
    input.request.body.method == "tools/call"
    input.request.body.params.name == "jira_update"
    input.request.body.params.arguments.issue_key in data.allowed_tickets
}

# MCP: allow k8s_create only with name prefix (argument inspection)
allow if {
    _is_mcp_host
    input.request.body.method == "tools/call"
    input.request.body.params.name == "k8s_create"
    input.request.body.params.arguments.namespace == "dev-sandbox"
    startswith(input.request.body.params.arguments.name, "agent-")
}

_is_mcp_host if {
    input.request.host == "mcp-gateway.internal"
}
```

With external data (`policies/data.json`):

```json
{
    "allowed_mcp_tools": ["jira_get_issue", "jira_update", "k8s_list", "k8s_create"],
    "allowed_tickets": ["PROJ-123", "PROJ-456"]
}
```

This approach gives identical filtering granularity to a dedicated MCP gateway — tool name matching, argument-level inspection, cross-field constraints — without any MCP-specific code in the addon. The addon sees JSON; Rego sees the JSON-RPC structure.

For MCP servers using stdio transport (not HTTP), the traffic does not pass through the proxy. Filtering stdio-based MCP requires a dedicated MCP gateway (e.g., Agentgateway) as a separate component. This is out of scope for this spec.

### 7.6 Unsupported Content Type — Metadata-Only Filtering

For binary formats where the body cannot be parsed, policies filter on request metadata:

```rego
# Allow gRPC calls only to specific services and methods (encoded in path)
allow if {
    input.request.content_type == "application/grpc"
    input.request.host == "grpc.internal.service"
    input.request.method == "POST"
    regex.match(`^/myservice\.v1\.(Read|List)`, input.request.path)
}

# Block gRPC write methods
deny_reasons contains "gRPC write methods not allowed" if {
    input.request.content_type == "application/grpc"
    regex.match(`^/myservice\.v1\.(Create|Update|Delete)`, input.request.path)
}
```

### 7.7 Managed Allowlist — `agentbox allow/deny`

The `agentbox allow` / `agentbox deny` CLI writes rules to `policies/managed.rego`. This file is auto-generated and watched by OPA:

```rego
# policies/managed.rego
# Auto-generated by agentbox allow/deny. Do not edit manually.
package agentbox

import rego.v1

allow if { input.request.host == "pypi.org" }
allow if { input.request.host == "registry.npmjs.org" }
allow if { regex.match(`^.*\.fedoraproject\.org$`, input.request.host) }
```

---

## 8. CLI Changes

### 8.1 `agentbox allow <host>`

Currently writes to `extra_request_policy` in `proxy.yaml` and triggers a proxy reload. Updated to write a Rego rule to `managed.rego` in the policy directory.

**New behavior:**

1. Escape the hostname for use in Rego string comparison (`==`) for exact hostnames, or `regex.match` for patterns containing wildcards.
2. Append an `allow` rule to `managed.rego`.
3. OPA picks up the file change automatically via `--watch` — **no proxy reload needed**.

```python
def _update_allowlist(host: str, add: bool, args) -> None:
    managed_path = os.path.join(session_dir, "policies", "managed.rego")
    # Read existing managed rules, add/remove the host, rewrite the file
    # OPA --watch picks up the change automatically
```

The proxy reload endpoint (`curl http://localhost:8082/reload`) is no longer called for allow/deny operations. It is still used for provider/credential configuration changes that affect `proxy.yaml`.

### 8.2 `agentbox deny <host>`

Removes the corresponding `allow` rule from `managed.rego`. If the host was allowed by a non-managed policy file (e.g., `providers.rego`), `deny` cannot override it — the user must edit the policy file directly. The CLI prints a warning in this case.

### 8.3 `agentbox init`

Updated to:

1. Copy preset policy files from `presets/<name>/policies/` to the session's policy directory.
2. Generate `managed.rego` with the header and no rules.
3. Include the OPA container in the generated `compose.yaml`.

### 8.4 `agentbox policy-reload`

New command. Forces OPA to reload policies by sending a `PUT` to OPA's policy API. Normally unnecessary (OPA `--watch` handles this), but useful for debugging.

---

## 9. Preset Migration

### 9.1 Policy Files per Preset

Each preset gains a `policies/` subdirectory with Rego files that replicate the current `request_policy` allowlist rules:

```
presets/
  default/
    proxy.yaml
    agent.yaml
    policies/
      main.rego       # default-deny base + provider allow rules
      managed.rego    # empty, auto-generated placeholder
  claude-vertex/
    proxy.yaml
    agent.yaml
    policies/
      main.rego       # Vertex-specific allow rules
      managed.rego
  cursor/
    proxy.yaml
    agent.yaml
    policies/
      main.rego       # Cursor-specific allow rules
      managed.rego
```

### 9.2 `proxy.yaml` Changes

Provider `request_policy` fields are **retained** but their role changes. They no longer serve as a security boundary (OPA handles that). They only control which provider's credentials are injected for a matching request:

```yaml
# Before: request_policy is both allowlist AND credential routing
# After: request_policy is credential routing only; OPA is the allowlist

providers:
  - name: anthropic
    enabled: true
    credential_type: static
    api_key_env: ANTHROPIC_API_KEY
    inject_header: x-api-key
    inject_prefix: ""
    # These rules now only control when this provider's credentials are injected.
    # The actual allow/deny decision is made by OPA.
    request_policy:
      - host: "api\\.anthropic\\.com"
        paths:
          - "/v1/messages(/.*)?$"
          - "/v1/complete$"
          - "/v1/models(/.*)?$"
        methods: [POST, GET]

opa:
  enabled: true
  url: "http://opa:8181"

# extra_request_policy is removed — managed.rego replaces it
```

### 9.3 Migration of `extra_request_policy`

The `extra_request_policy` entries in current presets (e.g., the long list of Fedora mirrors in `claude-vertex/proxy.yaml`) are converted to `allow` rules in the preset's `main.rego`:

```rego
# Fedora mirrors
allow if { input.request.host == "fedora.mirror-services.net" }
allow if { input.request.host == "mirrors.fedoraproject.org" }
allow if { input.request.host == "mirror.slu.cz" }
# ... etc
```

The `extra_request_policy` field is removed from `proxy.yaml`.

---

## 10. Logging

### 10.1 Access Log Extension

The structured JSON access log is extended with OPA decision metadata:

**Allowed request:**
```json
{
  "ts": "2026-07-28T12:00:00Z",
  "source": "proxy",
  "method": "POST",
  "url": "https://api.anthropic.com/v1/messages",
  "status": 200,
  "provider": "anthropic",
  "opa_decision": "allow",
  "opa_duration_ms": 0.3,
  "duration_ms": 1234
}
```

**Denied request:**
```json
{
  "ts": "2026-07-28T12:00:01Z",
  "source": "proxy",
  "method": "PUT",
  "url": "https://myproject.atlassian.net/rest/api/3/issue/OTHER-999",
  "status": 403,
  "blocked": true,
  "opa_decision": "deny",
  "opa_duration_ms": 0.5,
  "opa_denial_reasons": ["Jira access restricted to PROJ-123, got OTHER-999"],
  "request_host": "myproject.atlassian.net",
  "request_port": 443,
  "request_path": "/rest/api/3/issue/OTHER-999",
  "request_method": "PUT"
}
```

**OPA unreachable:**
```json
{
  "ts": "2026-07-28T12:00:02Z",
  "source": "proxy",
  "method": "GET",
  "url": "https://api.example.com/data",
  "status": 403,
  "blocked": true,
  "opa_decision": "error",
  "opa_error": "ConnectionRefusedError: [Errno 111] Connection refused",
  "request_host": "api.example.com"
}
```

### 10.2 OPA Decision Logs

OPA has built-in decision logging that can be enabled independently. This is outside the scope of this spec but is compatible — OPA can be configured to log all decisions to a file or remote endpoint for audit purposes.

---

## 11. Security Considerations

### 11.1 Fail-Closed

The default and recommended behavior is fail-closed. If OPA is unreachable, the request is denied. The `fail_open` configuration option exists for debugging only and should never be used in production.

### 11.2 OPA Server Isolation

The OPA server is on `agent-net` (`internal: true`), the same network as the agent. However, the agent cannot reach OPA directly because all agent HTTP traffic is forced through mitmproxy via `HTTPS_PROXY`/`HTTP_PROXY` environment variables, and OPA's hostname is not in any allowlist. The agent cannot:

- Query OPA to discover what policies are in effect (blocked by mitmproxy).
- Modify OPA policies (policy files are bind-mounted read-only from the host).
- Bypass OPA by connecting directly to external services (`agent-net` has no internet route; `proxy-net` is only attached to the proxy container).

### 11.3 Input Document Trust

The OPA input document is constructed by the proxy addon from the actual HTTP request. The agent cannot forge the input document — it only controls the HTTP request itself. The addon's body parsing is done on the raw bytes received from the agent, not on any agent-provided metadata.

### 11.4 Body Parsing Safety

Parsing untrusted request bodies introduces risk:

- **JSON:** `json.loads()` is safe against arbitrary input (no code execution, bounded memory for bounded input).
- **XML:** `xmltodict.parse()` is vulnerable to XML entity expansion (billion laughs attack). Mitigation: use `defusedxml` or limit entity expansion. Since the body size is already capped at 1 MiB by default, the blast radius is bounded.
- **YAML:** `yaml.safe_load()` is safe (no arbitrary Python object instantiation).
- **Body size limit:** The 1 MiB default cap prevents memory exhaustion from large bodies.

### 11.5 Credential Redaction

Authorization headers are redacted in the OPA input document. This prevents:
- Policies that depend on credential values (fragile).
- Credential leakage through OPA decision logs.
- The OPA server (a third-party component) from seeing live credentials.

Note: at the point when the OPA input is constructed (before credential injection), the authorization header contains either a dummy token or no token. Redaction is a defense-in-depth measure for cases where the order of operations changes or new credential injection patterns are added.

### 11.6 Policy Complexity

Rego policies are Turing-incomplete by design (no general loops, recursion is bounded by data structure depth). However, complex policies with deep object traversal or large data sets can still have high evaluation latency. For the PoC, this is not a concern — policies are expected to be simple. For production, OPA's profiling and benchmarking tools should be used to ensure sub-millisecond evaluation.

---

## 12. Implementation Plan

### Step 1: Add OPA sidecar to compose

- Add OPA service to `compose-base.yaml`.
- Create `presets/default/policies/main.rego` with the default-deny base policy.
- Create `presets/default/policies/managed.rego` as an empty placeholder.
- Update `agentbox init` to copy policy files to the session directory and include the policy volume mount in the generated compose.

### Step 2: Implement body parsing in the addon

- Add `_parse_body()` method to `AgentboxAddon`.
- Add `_build_opa_input()` method that constructs the input document from a flow and optional parsed body.
- Add header redaction logic.
- Add `xmltodict` to `proxy/requirements.in` and regenerate lockfile.

### Step 3: Implement OPA client in the addon

- Add `_opa_allow()` method using `urllib.request`.
- Add `_opa_denial_reasons()` method for the secondary query on denial.
- Add OPA health check in `running()` hook.
- Add `opa` configuration section parsing.

### Step 4: Rewire `requestheaders()` and add `request()` hook

- Split the allow/deny logic: `requestheaders()` handles bodyless and streaming requests via OPA metadata-only query; `request()` handles body-bearing requests via OPA full query.
- Remove the `rule_matches()` allowlist loop from `requestheaders()`.
- Retain `Provider.matches()` + `Provider.inject()` for credential injection after OPA allows.
- Update the 403 response to include OPA denial reasons.
- Update access logging with OPA decision metadata.

### Step 5: Convert preset policies

- Write `main.rego` for each preset (`default`, `claude-vertex`, `cursor`, `opencode-vertex`) that replicates the current `request_policy` allowlist rules in Rego.
- Migrate `extra_request_policy` entries from `proxy.yaml` to `main.rego` `allow` rules.
- Remove `extra_request_policy` from all `proxy.yaml` files.
- Add `opa` section to all `proxy.yaml` files.

### Step 6: Update `agentbox` CLI

- Update `agentbox allow` / `agentbox deny` to write/remove rules in `managed.rego`.
- Remove the `extra_request_policy` read/write logic.
- Remove the proxy reload call for allow/deny (OPA `--watch` handles it).
- Add `agentbox policy-reload` command for manual OPA reload.
- Update `agentbox init` to set up the policy directory.

### Step 7: Update proxy startup

- Update `proxy/start.sh` to wait for OPA health check before starting mitmproxy.
- Add retry logic with backoff for the OPA health check (OPA container may start slower than the proxy).

### Step 8: Update documentation

- Update `docs/SPEC.md` sections on traffic mediation and request filtering.
- Update `docs/ARCHITECTURE.md` with the OPA sidecar and body inspection flow.
- Update `docs/SPEC-request-policy.md` with a deprecation note pointing to this spec.
- Add comments in preset policy files documenting the rule format.

### Step 9: Testing

- Test OPA query for allowed and denied requests.
- Test body parsing for each supported content type (JSON, form, XML, YAML, text, multipart).
- Test unsupported content types (protobuf, binary) pass metadata only.
- Test body size limit enforcement.
- Test fail-closed behavior when OPA is unreachable.
- Test `agentbox allow` / `deny` writes correct Rego and OPA picks up changes.
- Test hot-reload: modify policy file, verify next request uses new policy.
- Test credential injection still works correctly after OPA allow.
- Test streaming requests are not buffered.
- Test header redaction in OPA input.
- Test denial reasons are included in 403 response and access log.
- Test preset migration: verify each preset's Rego policy allows the same requests as the current `request_policy`.

---

## 13. Open Questions

1. **OPA image pinning.** The spec uses `openpolicyagent/opa:1-static` (latest v1.x static binary). Should this be pinned to a specific version for reproducibility? The static variant has no OS dependencies, reducing supply chain risk.

2. **Policy testing tooling.** OPA has built-in test support (`opa test`). Should the PoC include a test harness for policy validation? This would let users run `opa test policies/` to verify their policies before deploying.

3. **Custom body parsers.** Should the addon support registering custom body parsers for domain-specific content types (e.g., GraphQL query parsing for `application/graphql`)? Or is this deferred to post-PoC?

4. **OPA bundles.** OPA supports loading policies from bundles (tar.gz) via HTTP. This is more production-grade than file watching but adds complexity. Defer to post-PoC?

5. **Response filtering.** This spec covers only request filtering. Should OPA also evaluate responses (e.g., prevent the agent from receiving credentials in API responses)? This would require a `response()` hook integration and response body parsing. Defer to post-PoC.
