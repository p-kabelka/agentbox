# agent-sandbox Technical Specification

## 1. Overview

agent-sandbox creates isolated, credential-safe workspaces for running AI coding agents such as Claude Code, OpenCode, and Codex CLI. Each sandbox is a pair of Podman containers — a proxy and an agent — connected by a private internal network that provides full network mediation without requiring host-level privileges.

The core problem is that AI agents run with broad process permissions. If an agent is compromised through prompt injection or malicious content in the working directory, it can read API credentials from environment variables or well-known paths, exfiltrate them to attacker-controlled endpoints, make arbitrary outbound network calls, or inject malicious hooks into the host's `.git/` directory that execute silently when a developer runs routine git operations.

agent-sandbox defends against these threats by structuring the environment so that the primitives required for attacks are never available to the agent process: real credentials live only in the proxy container, external TCP is only reachable through a TLS-intercepting proxy that enforces an allowlist, and the host git repository is never mounted into the agent container — instead, a read-only bundle is used for input and a bare repository with immutable hooks is used for output.

---

## 2. Threat Model

### 2.1 Primary Threats Defended Against

**API credential exfiltration**
A compromised agent attempts to read `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `application_default_credentials.json`, OAuth tokens, or any other long-lived credential and transmit it to an attacker-controlled endpoint. agent-sandbox ensures that real credentials are never present in the agent container's environment or filesystem. The agent receives dummy placeholder values; the proxy rewrites requests with real credentials before forwarding them upstream.

**Arbitrary outbound network access**
The agent attempts to connect to an attacker-controlled server to exfiltrate data, receive instructions, or download malicious payloads. The agent container is attached only to a Podman internal network with no default gateway. All external TCP connections are forced through the mitmproxy instance, which enforces a per-project allowlist and blocks connections to non-whitelisted hosts with an HTTP 403 before the connection is established.

**Git repository poisoning**
The agent writes executable scripts to `.git/hooks/` (e.g., `post-checkout`, `pre-push`) that would execute on the host when a developer runs routine git operations after retrieving the agent's output. agent-sandbox ensures the host's `.git/` directory is never mounted into the agent container. The agent writes commits to a bare output repository whose `hooks/` directory is created with `chmod 555` (no write permission for any user), and the host fetches commits using `git -c core.hooksPath=/dev/null`.

**Prompt injection via untrusted working directory content**
Malicious content embedded in files, commit messages, or other project artifacts causes the agent to interpret attacker instructions and take actions outside the intended task scope. agent-sandbox does not eliminate this class of threat at the network layer, but the other controls above ensure that even a successfully injected agent cannot exfiltrate real credentials, reach arbitrary network destinations, or poison the host git state.

### 2.2 Out of Scope / Residual Risks

**Data exfiltration through the LLM prompt itself.** A compromised agent can include the contents of sensitive files in its prompt to any allowed inference endpoint. The inference provider receives this data. This is out of scope for network-layer isolation; mitigation relies on using minimal-scope API keys and reviewing session transcripts.

**Covert timing channels.** An agent can encode information in request timing patterns observable to the LLM provider. This is not addressed.

**Agent writing malicious scripts to tracked hook directories.** If the project tracks a hooks directory (e.g., `.husky/`, `scripts/`) and the agent adds a malicious script to it, that script becomes part of the git commit and could execute on the host after the developer merges. This is mitigated by code review at `sandbox fetch` time; it is not blocked automatically.

---

## 3. Use Cases

### Use Case 1: Isolated Feature Development

A developer creates a sandbox for a feature branch, provides the agent with a task prompt, and lets it implement the feature autonomously. The agent works inside the container, commits its changes to the output remote, and the developer retrieves them with `sandbox fetch`. The developer reviews the diff — including any new scripts or hook files — before merging into their working branch. The host repository and credentials are never at risk during the agent session.

### Use Case 2: Multi-Repo Context

The agent needs read access to a shared internal library while implementing changes to the main project. The developer adds the library path with `sandbox mount add ~/libs/shared-lib`. The library is mounted read-only into the agent container as an additional reference directory. The agent can read its source but cannot modify it or push to it. The mount configuration is stored in `mounts.yaml` and persists across sandbox restarts.

### Use Case 3: Restricted Provider Switching

A team running inference through Anthropic's direct API wants to switch to Vertex AI (e.g., for quota reasons) without changing the agent harness or the developer's workflow. The developer selects a Vertex preset with `sandbox run --preset vertex`. The proxy container starts the fake GCE metadata server, the agent's `GCE_METADATA_HOST` points to it, and the Google auth library fetches short-lived tokens transparently. The agent harness code is unchanged; no GCP service account key is ever present in the agent container.

### Use Case 4: Parallel Sandboxes

A developer runs multiple simultaneous sandboxes for different projects or branches — for example, one agent refactoring a backend service and another writing tests for a frontend library. Each sandbox has its own isolated Podman network, its own proxy container with a separate credential environment, and its own web UI port assigned from a configurable port range. The sandboxes do not share network namespaces and cannot observe each other's traffic.

---

## 4. Architecture

### 4.1 Network Topology

Two Podman networks are created per sandbox:

- **`agent-net`**: A Podman `internal: true` network. Containers attached to this network have no default gateway to the internet. The agent container is connected only to this network.
- **`proxy-net`**: A standard Podman network with external connectivity. Only the proxy container is attached to this network.

The proxy container is attached to both networks and is the sole egress point for all agent traffic. Because isolation is enforced by Podman's network namespace implementation rather than by iptables or nftables rules on the host, no host-level privilege is required to establish or maintain the isolation boundary.

```
┌─────────────────────────────────────────────────────────┐
│  agent container                                        │
│  (agent-net only; no internet gateway)                  │
│                                                         │
│  proxychains → proxy:8080 (all external TCP)            │
│  GCE metadata → proxy:9090 (Vertex only)                │
└────────────────────────┬────────────────────────────────┘
                         │ agent-net (internal)
┌────────────────────────┴────────────────────────────────┐
│  proxy container                                        │
│  agent-net ← | → proxy-net                              │
│                                                         │
│  mitmweb :8080 (TLS MITM, allowlist, credential inject) │
│  mitmweb UI :8081                                       │
│  metadata server :9090 (Vertex only)                    │
└────────────────────────┬────────────────────────────────┘
                         │ proxy-net (external)
                         ▼
                      Internet
```

### 4.2 Container Topology

**Proxy container**

Runs mitmweb (acting as HTTP CONNECT proxy on port 8080, web UI on port 8081) and optionally the GCE metadata server (port 9090, enabled when `vertex.metadata_server: true` in `proxy.yaml`). Holds real API credentials via environment variables or mounted credential files. Connected to both `agent-net` and `proxy-net`.

**Agent container**

Runs the configured agent harness (e.g., `claude`, `opencode`, `codex`). Contains no real credentials. Connected only to `agent-net`. Uses proxychains4 for transparent TCP routing and `NODE_EXTRA_CA_CERTS` to trust the mitmproxy CA certificate for TLS verification.

### 4.3 Credential Flow

**Direct API providers (Anthropic, OpenAI, etc.)**

The agent container receives a dummy placeholder value for the relevant API key environment variable (e.g., `ANTHROPIC_API_KEY=dummy`). When the agent makes an HTTPS call to the provider's API, the connection is intercepted by mitmproxy. The `injector.py` addon reads the allowlist from `config/proxy.yaml`, verifies the destination host is permitted, strips the dummy key from the request headers, and injects the real key from the proxy container's environment. The real key never enters the agent container's memory or filesystem.

**GCP Vertex AI**

The agent container has `GCE_METADATA_HOST=proxy:9090`. When the Google auth library requests an access token, it calls the fake metadata server running in the proxy container. The metadata server holds the real GCP credentials (service account key or refresh token) and returns a short-lived OAuth 2.0 access token with a 1-hour TTL scoped to `https://www.googleapis.com/auth/cloud-platform`. The access token itself reaches the agent container (it must, to authenticate API calls), but the long-lived credential never does.

### 4.4 Traffic Interception

proxychains4 is configured in `dynamic_chain` mode inside the agent container:

- Connections to `proxy:8080` and `proxy:9090` are within the same `agent-net` network and are reachable directly without going through a SOCKS proxy.
- All connections to external hosts have no direct route (no default gateway on `agent-net`). proxychains intercepts these at the libc socket layer via `LD_PRELOAD` and tunnels them to mitmproxy using an HTTP CONNECT request.

mitmproxy terminates the TLS session from the agent, inspects the plaintext request, applies the allowlist, injects credentials if applicable, re-encrypts the request, and forwards it to the real upstream server. The mitmproxy CA certificate is installed in the agent container's system trust store and exported via `NODE_EXTRA_CA_CERTS` so that both system-level and Node.js TLS verification succeed.

### 4.5 Git Isolation: The Two-Remote Pattern

**Source (read-only input)**

Before starting the agent, `sandbox run` creates a git bundle of the project branch:

```
git bundle create .sandbox/source.bundle <branch>
```

The bundle is mounted read-only into the agent container at `/source/project.bundle`. `start.sh` clones from this bundle, producing a fresh `.git` directory inside the container with no connection to the host's `.git/` directory. The host git working directory and object store are never accessible from within the container.

**Output (write-only barrier)**

A bare repository is created at `.sandbox/output.git/` on the host at first use. Immediately after creation, its `hooks/` directory is set to `chmod 555` (read and execute for all; write for none). The bare repo is mounted into the agent container as a git remote named `output`. The agent can push commits to this remote. When the developer is ready to review the agent's work, they run:

```
sandbox fetch <branch>
```

This executes:

```
git -c core.hooksPath=/dev/null fetch .sandbox/output.git <branch>:refs/sandbox/<branch>
```

The `core.hooksPath=/dev/null` flag ensures that even if the agent has somehow written files into `hooks/` (which `chmod 555` prevents), none of them execute during the fetch. The fetched commits land in `refs/sandbox/<branch>` for review before any merge.

---

## 5. Components

### 5.1 `proxy/` Container

**mitmweb**

Full TLS MITM proxy. Loads two Python addons at startup.

**`addons/injector.py`**

Reads `config/proxy.yaml` at startup. For each intercepted request:

1. Checks the destination hostname against the configured allowlist. If not listed, responds with HTTP 403 and logs the block. The upstream connection is never opened.
2. If the destination matches a configured provider, strips any existing authorization header and injects the real credential (API key, Bearer token) from the proxy container's environment.

The allowlist always includes the configured provider endpoints. Additional hosts are added and removed at runtime via `sandbox allow` and `sandbox deny`, which signal mitmproxy to reload the addon.

**`addons/logger.py`**

Writes a structured JSON access log to `/var/log/proxy/access.log`. Each entry includes timestamp, method, URL, response status, response size, and a `blocked` boolean. Request and response bodies can optionally be logged for debugging. Blocked requests are logged before the connection is refused.

**`metadata_server.py`**

A minimal HTTP server that implements the subset of the GCE instance metadata API used by the Google auth library. Started only when `vertex.metadata_server: true` is set in `proxy.yaml`. Binds to `0.0.0.0:9090` inside the proxy container. Holds real GCP credentials (via `GOOGLE_APPLICATION_CREDENTIALS` or Application Default Credentials mounted from the proxy-side filesystem). On token requests, calls the Google OAuth2 token endpoint and returns a short-lived access token. The long-lived credential is never forwarded to the caller.

### 5.2 `agent/` Container

**proxychains4**

Configured in `dynamic_chain` mode. The chain list contains a single entry: the mitmproxy CONNECT endpoint at `proxy:8080`. All libc-level TCP connections to external hosts are intercepted via `LD_PRELOAD` and tunneled through this chain. Works transparently for Node.js, Python, Go (dynamically linked), and other runtimes that use libc networking.

**`start.sh`**

Executed as the container entrypoint. Performs in order:

1. Installs the mitmproxy CA certificate into the system trust store and sets `NODE_EXTRA_CA_CERTS`.
2. Clones the project from the bundle at `/source/project.bundle`.
3. Adds the output bare repository as a git remote named `output`.
4. Mounts any additional reference repositories passed via `compose.override.yaml`.
5. Sets environment variables appropriate to the configured harness.
6. Launches the agent harness binary.

**Base image**

`node:22-slim` with Claude Code pre-installed. Designed to be extended for other harnesses via a derived Containerfile.

### 5.3 `bin/sandbox` CLI

A Python script installed to `PATH` by `install.sh`. Commands are grouped by function:

| Group | Commands |
|-------|----------|
| Setup | `build`, `update`, `preset list`, `preset edit`, `preset copy` |
| Project lifecycle | `run [--preset <name>] [--harness <binary>]`, `start`, `stop`, `shell` |
| Monitoring | `logs`, `web` |
| Egress control | `allow <host>`, `deny <host>` |
| Code retrieval | `fetch <branch>` |
| Reference mounts | `mount list`, `mount add <path>`, `mount remove <path>` |
| Observation | `status` |

`sandbox run` is the primary entry point. It refreshes `source.bundle`, regenerates `compose.override.yaml` from `mounts.yaml` and current git state, assigns a web UI port, writes `.env`, and starts the Compose stack.

`sandbox fetch` runs the `core.hooksPath=/dev/null` fetch and reports the resulting ref for review.

`sandbox allow` and `sandbox deny` append or remove entries from the allowlist in `config/proxy.yaml` and send a reload signal to the mitmproxy process.

### 5.4 Configuration Files

| File | Location | Purpose | Overwritten by `sandbox run`? |
|------|----------|---------|-------------------------------|
| `proxy.yaml` (preset) | `~/.agent-sandbox/presets/<name>/` | Global default provider config | N/A — global |
| `config/proxy.yaml` | `.sandbox/config/` | Per-project provider config, allowlist | No — user-owned |
| `mounts.yaml` | `.sandbox/` | Persistent extra reference mounts | No — user-owned |
| `compose.override.yaml` | `.sandbox/` | Generated Compose overrides (port, volumes) | Yes — regenerated from `mounts.yaml` and git state |
| `.env` | `.sandbox/` | `SANDBOX_WEB_PORT`, `AGENT_HARNESS` | Yes |
| `source.bundle` | `.sandbox/` | Read-only git snapshot | Yes — refreshed on each `sandbox run` |
| `output.git/` | `.sandbox/` | Bare repo output barrier | Created once; agent pushes here |

---

## 6. Security Properties

| Property | Mechanism | Guarantee |
|----------|-----------|-----------|
| Real API keys never enter agent container | Proxy-side env; agent sees `dummy` placeholder | Keys absent from agent process memory and filesystem |
| GCP long-lived credentials never enter agent container | Fake metadata server returns short-lived tokens only | SA key / refresh token stays on proxy side |
| Agent cannot make arbitrary internet connections | Podman `internal: true` network; no default gateway | All external TCP must transit mitmproxy |
| Agent can only reach allowlisted API endpoints | mitmproxy injector addon enforces allowlist | Non-listed hosts receive HTTP 403; upstream connection never opened |
| Host `.git/` is never accessible to agent | Source delivered as git bundle; no host git mount | No filesystem path to host's `.git/` exists in the container |
| Agent cannot plant executable hooks in host git | `output.git/hooks/` is `chmod 555` at creation; fetch uses `core.hooksPath=/dev/null` | No new files can be written to hooks dir; hooks do not execute on fetch |
| All agent network traffic is auditable | mitmproxy full TLS termination; structured JSON access log | Every request, including blocked ones, is logged with URL and status |
| Sandbox network isolation requires no host privilege | Podman internal networks via container runtime namespaces | No `sudo`, no host iptables or nftables changes needed |

---

## 7. Operational Workflows

### 7.1 First-Time Setup (One Machine)

```bash
git clone <repo> ~/.agent-sandbox
~/.agent-sandbox/install.sh
source ~/.bashrc

sandbox preset edit                       # enable provider, e.g. anthropic.enabled: true
export ANTHROPIC_API_KEY=sk-ant-...      # add to ~/.bashrc

sandbox build                            # builds container images; ~2 minutes; once per machine
```

### 7.2 Starting a Project Sandbox

```bash
cd ~/projects/my-app
sandbox run                              # uses default preset and harness
# or:
sandbox run --preset webdev --harness opencode
# proxy starts in background; agent starts interactively
```

### 7.3 During a Session

```bash
sandbox allow pypi.org                  # add a host to the egress allowlist
sandbox deny pypi.org                   # remove it

sandbox logs                            # tail the JSON access log from the proxy
sandbox web                             # open traffic monitor at http://localhost:<port>

sandbox mount add ~/libs/shared-lib     # add a read-only reference mount
sandbox start                           # restart agent container to apply new mount

sandbox status                          # show container state, assigned port, active mounts
```

### 7.4 Retrieving Agent Output

```bash
# Inside the agent container, the agent has run:
#   git push output HEAD:agent-work

# On the host:
sandbox fetch agent-work                # fetches to refs/sandbox/agent-work using core.hooksPath=/dev/null

git log refs/sandbox/agent-work         # review commit history
git diff HEAD refs/sandbox/agent-work   # review all changes before merging
git merge refs/sandbox/agent-work       # merge after review

sandbox stop                            # tear down containers and networks
```

---

## 8. Extension Points

**New LLM provider**

Add a stanza to `config/proxy.yaml` specifying `name`, `enabled`, `api_key_env`, `inject_header`, `inject_prefix`, and `allowed_hosts`. The injector addon reads this configuration at startup and on reload. No code changes are required.

**New agent harness**

Extend the agent Containerfile:

```dockerfile
FROM localhost/agent-sandbox-agent:latest
RUN npm install -g opencode   # or pip install codex, etc.
```

Set `AGENT_HARNESS=<binary>` in `.sandbox/.env` or pass `--harness <binary>` to `sandbox run`. `start.sh` execs the specified binary as the final step.

**VM-level isolation**

Uncomment `runtime: krun` in `.sandbox/compose.override.yaml` (requires `crun-vm` / `krun` installed on the host). Each container runs in its own KVM microVM. The two-network topology and credential flow are unchanged; the isolation boundary moves from Linux namespaces to a hypervisor boundary.

**Custom preset**

```bash
sandbox preset copy default mypreset
sandbox preset edit mypreset
sandbox run --preset mypreset
```

Presets are stored in `~/.agent-sandbox/presets/<name>/` and contain a `proxy.yaml` with provider configuration. Copying from `default` gives a starting point with all fields documented.

---

## 9. Known Limitations

**Data exfiltration via LLM prompts.** A compromised agent can include the contents of sensitive files in its prompts to any allowed inference endpoint. The inference provider receives this data. This is not addressable at the network layer because the traffic is legitimate use of the allowed endpoint. Mitigation: use minimal-scope API keys; review session transcripts; do not allow the agent to access files outside the project scope.

**Access token window (Vertex AI).** A 1-hour OAuth access token is present in the agent container's process environment. An agent that reads and exfiltrates this token has up to 1 hour to use it, limited to the IAM roles bound to the service account. Mitigation: bind the service account to the minimum required roles (e.g., `roles/aiplatform.user` only).

**DNS resolution of external hostnames.** The agent container can resolve external hostnames via the container runtime's DNS resolver, but cannot TCP-connect to them (no default gateway). DNS-based data exfiltration (encoding data in query labels sent to an attacker-controlled nameserver) is not blocked. Mitigation: configure a restricted DNS resolver in the agent container and add `/etc/hosts` entries for all required hostnames, disabling recursive resolution.

**`core.hooksPath` set in tracked configuration files.** If the project tracks a file (e.g., `.gitconfig`, `.husky/.huskyrc`) that sets `core.hooksPath = ./hooks`, and the agent adds a malicious executable script to that directory, the script becomes part of the git commit and could execute on the host after the developer merges and runs a git operation. The `chmod 555` on `output.git/hooks/` does not protect against this because the malicious script lives in the tracked working tree, not in the bare repo's hooks directory. Mitigation: inspect all new or modified scripts in hook-related directories at `sandbox fetch` time before merging.

**proxychains LD_PRELOAD bypass by static binaries.** proxychains4 intercepts socket calls by injecting a shared library via `LD_PRELOAD`, which applies only to dynamically linked executables. A static binary (one that does not use libc's dynamic linker) would bypass proxychains and, if it had a way to route to the internet, could make unmediated connections. In practice, no common agent harness ships as a fully static binary; this is noted as a residual risk for custom harness configurations.
