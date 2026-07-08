# agentbox — Architecture

> This document describes **how** the system is built and **why** it is designed the way it is.
> For requirements, behavior, and the system contract, see [SPEC.md](SPEC.md).

---

## 1. Problem Statement

AI coding agents operate with the same OS permissions as the developer who starts them. They can read environment variables, traverse the filesystem, and open arbitrary network connections. This is necessary for legitimate work — installing packages, calling APIs, reading project files — but it creates a symmetric risk: anything the agent can do legitimately, a compromised agent can do maliciously.

The two dominant attack surfaces are:

**Credential theft.** The agent has access to the same API keys and credential files the developer uses. A prompt-injected agent can read these and exfiltrate them over the network, giving an attacker persistent access to the developer's accounts.

**Repository poisoning.** The agent has write access to the project's `.git/` directory. A malicious commit hook planted there executes silently on the developer's machine the next time they run a routine git operation — potentially after the agentbox session is over and the threat is no longer visible.

The architectural goal is to eliminate both attack surfaces without making the legitimate workflow impractical. See [SPEC.md §3](SPEC.md) for the use cases this architecture supports.

---

## 2. Design Principles

### 2.1 The agent container is untrusted by assumption

The core design decision is to treat the agent container as a potentially hostile process from the start — not as a hardened environment that we try to protect. This shifts the question from "how do we prevent the agent from doing bad things?" to "how do we structure the environment so the tools for bad things aren't present?"

This is analogous to the principle of least privilege in system design: rather than auditing every action, we narrow what actions are structurally possible.

### 2.2 Credentials are separated by container boundary

Real credentials live only in the proxy container. The agent container receives dummy placeholders. The proxy intercepts every outbound request and swaps in the real credential before forwarding.

This means even a fully compromised agent — one that dumps its entire environment and filesystem — cannot exfiltrate a usable credential. The credential does not exist in the agent's address space.

### 2.3 The network is a one-way valve

All agent traffic must pass through the proxy, which enforces an allowlist. The proxy can reach the internet; the agent cannot. This is structurally enforced by the container network topology, not by firewall rules the agent could potentially bypass.

The proxy acts as both a gateway and an inspection point. Every request is logged. Blocked requests are recorded. The developer can see everything the agent tried to do.

### 2.4 Git state flows through content-addressed objects only

The agent never has access to the host's `.git/` directory. Instead:

- **Input** arrives as a git bundle — a portable, read-only snapshot.
- **Output** leaves through a bare repository that the agent pushes to.

The host never executes anything from the bare repository automatically. Output is always fetched with hook execution explicitly disabled, and the developer reviews commits before merging.

This converts the git interface from an execution surface into a data transport.

### 2.5 Isolation must not require host privilege

The system must be deployable by a developer on a workstation without `sudo`. Any design that requires modifying host iptables, installing kernel modules, or editing system-wide configuration is a deployment barrier that will limit adoption and introduce operational complexity.

Podman's container networking achieves network isolation without host-level privilege. The internal network flag prevents external routing entirely, enforced inside the network namespace of the container runtime.

### 2.6 Configuration over code for extensibility

The proxy's L7 request policy, credential injection rules, and provider settings are all driven by a YAML configuration file. Adding a new LLM provider requires editing a configuration file, not writing code. This makes the system usable by developers who are not contributors to the project.

---

## 3. Trust Boundaries

The system has three distinct trust zones separated by explicit boundaries:

```
┌─────────────────────────────────────────────────────────┐
│  HOST  (fully trusted)                                  │
│  Developer's machine, shell environment, git history    │
│                                                         │
│  ┌───────────────────────────────────────────────────┐  │
│  │  PROXY CONTAINER  (trusted executor)              │  │
│  │  Holds real credentials.                          │  │
│  │  Enforces policy (allowlist, credential swap).    │  │
│  │  Produces audit logs.                             │  │
│  │  Minimal codebase, easy to audit.                 │  │
│  └───────────────────────┬───────────────────────────┘  │
│                          │ agent-net (internal)          │
│  ┌───────────────────────┴───────────────────────────┐  │
│  │  AGENT CONTAINER  (untrusted)                     │  │
│  │  May be compromised via prompt injection.         │  │
│  │  Has no credentials, no internet route.           │  │
│  │  Cannot write to host filesystem.                 │  │
│  └───────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

**Host → Proxy**: The host mounts real credentials into the proxy as read-only volumes (environment variables or secret files) and starts the proxy container. The proxy is trusted to enforce policy correctly. It runs minimal, auditable code.

**Proxy → Agent (inbound)**: The proxy's fake metadata server issues dummy tokens to satisfy the agent's GCP auth library initialization. For direct API providers, the proxy serves as a CONNECT-tunneling proxy. The agent receives only what it needs to make API calls — not the credentials that authorise those calls. Real credentials are injected by the proxy addon on intercepted requests, never issued to the agent directly.

**Agent → Proxy (outbound)**: All agent network traffic is intercepted by the proxy. The proxy is the only entity that can reach the internet. The agent cannot bypass this without a path that does not exist in the network topology.

**Agent → Host (filesystem)**: The agent has no write-capable mount into the host filesystem. The git bundle is read-only. The output bare repository has its `hooks/` directory and `config` file bind-mounted read-only (kernel-enforced). Reference mounts default to read-only.

---

## 4. Component Topology

### 4.1 Network Topology

Two Podman networks are created per agentbox:

- **`agent-net`**: A Podman `internal: true` network. Containers attached to this network have no default gateway to the internet. The agent container is connected only to this network.
- **`proxy-net`**: A standard Podman network with external connectivity. Only the proxy container is attached to this network.

The proxy container is attached to both networks and is the sole egress point for all agent traffic.

```
┌─────────────────────────────────────────────────────────┐
│  agent container                                        │
│  (agent-net only; no internet gateway)                  │
│                                                         │
│  HTTPS_PROXY → proxy:8080 (all external TCP)            │
│  proxychains → proxy:8080 (fallback for non-proxy-aware)│
│  GCE metadata → proxy:9090 (Vertex only)                │
└────────────────────────┬────────────────────────────────┘
                         │ agent-net (internal)
┌────────────────────────┴────────────────────────────────┐
│  proxy container                                        │
│  agent-net ← | → proxy-net                              │
│                                                         │
│  mitmweb :8080 (TLS MITM, allowlist, credential inject) │
│  mitmweb UI :8081                                       │
│  config reload :8082 (hot-reload endpoint)               │
│  metadata server :9090 (Vertex only)                    │
└────────────────────────┬────────────────────────────────┘
                         │ proxy-net (external)
                         ▼
                      Internet
```

### 4.2 Proxy Container

**Base image:** `python:3.12-slim` with hash-pinned Python dependencies.

**mitmweb** — Full TLS MITM proxy on port 8080, web UI on port 8081. Loads the addon module at startup.

**Addon modules** (`addons/`):

- **`addon.py`** — The `AgentboxAddon` class. Loads `proxy.yaml` at startup and on hot-reload. Builds the combined L7 request policy from all enabled providers' `request_policy` rules plus `extra_request_policy` rules. The `requestheaders()` hook enforces the policy (HTTP 403 for non-matching requests) and delegates credential injection to `Provider` objects. The `response()` hook logs structured JSON. Exposes an async HTTP reload endpoint on port 8082 so that `agentbox allow`/`deny` can update the policy without restarting the proxy or dropping active connections — this is important because interrupting an LLM inference request mid-stream may not be recoverable. Hot-reload supports two paths: a fast path (allowlist-only, reuses existing providers) for `agentbox allow`/`deny`, and a full reload (rebuilds providers with `run_in_executor` for blocking resolver init) when provider configuration changes.

- **`provider.py`** — The `Provider` class and L7 request matching engine. Each provider's `request_policy` is compiled at startup into `CompiledRule` objects containing pre-compiled regex patterns for host (full match), port (integer or regex), path (start-anchored), and HTTP methods. The shared `rule_matches()` function is used by both the allowlist check and `Provider.matches()`. On match, the provider delegates to a `CredentialResolver` to obtain the credential value and injects it into the configured HTTP header.

- **`resolvers.py`** — Credential resolver implementations, registered via `__init_subclass__`:
  - `StaticKeyResolver` (`credential_type: static`) — Reads the API key from a file at `/run/secrets/` (via `api_key_file`) or from an environment variable (via `api_key_env`). File takes precedence.
  - `OAuthResolver` (`credential_type: oauth`) — Loads Google credentials via `google.auth.default()`, refreshes tokens as needed using a threading lock for safe concurrent access.

**`metadata_server.py`** — A minimal HTTP server implementing the subset of the GCE instance metadata API used by the Google auth library. Started only when `vertex.metadata_server: true` is set in `proxy.yaml`. Returns fixed dummy tokens (`dummy-replaced-by-proxy`) on all token requests — it holds no real GCP credentials. The dummy token satisfies the ADC initialization check; the `OAuthResolver` in the addon replaces it with a real token on intercepted requests.

**`start.sh`** — Proxy entrypoint. Conditionally starts the metadata server. Installs custom CA certificates from `/custom-certs/` if present. Launches mitmweb. Waits for CA cert generation. Manages process lifecycle with signal handling.

### 4.3 Agent Container

**Base image:** `fedora:44` with `curl`, `git`, `ca-certificates`, `proxychains-ng`, `vim`, `tmux`, and `jq`.

**Harness images** — Each supported agent harness has its own Containerfile extending the base:
- `Containerfile.claude` — Imports Claude Code's GPG signing key, adds the official RPM repository, and installs `claude-code` via `dnf`.
- `Containerfile.opencode` — Multi-stage build: downloads the OpenCode binary from GitHub and optionally verifies attestation via `gh release verify-asset` (requires `GH_TOKEN`; can be skipped with `AGENTBOX_BUILD_OPENCODE_SKIP_VERIFICATION=1`).
- `Containerfile.cursor` — Installs Cursor via the official install script.

**`start.sh`** — Agent entrypoint. Performs in order:
1. Waits for the mitmproxy CA certificate to appear, then appends it to the system PEM bundle and exports `SSL_CERT_FILE`, `CURL_CA_BUNDLE`, `GIT_SSL_CAINFO`, `REQUESTS_CA_BUNDLE`, and `NODE_EXTRA_CA_CERTS` so all common TLS libraries trust the proxy's certificate.
2. If `/source/project.bundle` is present and `/workspace/.git` does not yet exist, clones the bundle to `/workspace`, renames the bundle remote from `origin` to `source`, adds the output bare repository as `origin`, and enables `push.autoSetupRemote` so the agent can push with just `git push`.
3. Copies preset dotfiles from `/agentbox-dotfiles` into the container's home directory (if the directory is mounted).
4. If arguments were passed (`$# > 0`), execs them directly. Otherwise execs `AGENT_HARNESS` with `AGENT_HARNESS_ARGS`, or falls back to bash.

**Traffic interception:** proxychains4 is configured in `dynamic_chain` mode. All libc-level TCP connections to external hosts are intercepted via `LD_PRELOAD` and tunneled through the mitmproxy CONNECT endpoint at `proxy:8080`. Additionally, `HTTPS_PROXY` and `HTTP_PROXY` environment variables are set so that tools which respect them use the proxy directly. Tools that respect neither mechanism are caught by proxychains at the syscall level.

### 4.4 Compose Configuration

`compose-base.yaml` defines the shared service template: two networks (`agent-net` internal, `proxy-net` external), two named volumes (`proxy-ca` for the mitmproxy CA cert, `proxy-logs` for access logs), and base service definitions for the proxy and agent.

At `agentbox init` time, the CLI merges `compose-base.yaml` with the preset configuration to produce a single `compose.yaml` in the session directory. This includes: port mapping for the mitmweb UI, volume mounts for the session's proxy config, source bundle, output repo (with read-only overlays for `hooks/` and `config`), dotfiles, context mounts, Vertex credential mounts, custom certificate mounts, and provider secret file mounts.

---

## 5. Architectural Layers

The system is structured in five layers, each with a distinct responsibility:

```
┌────────────────────────────────────────────────────────┐
│  5. ORCHESTRATION                                      │
│     agentbox CLI — project lifecycle, preset management │
│     compose-base.yaml — shared service definitions     │
└──────────────────────────────┬─────────────────────────┘
                               │
┌──────────────────────────────┴─────────────────────────┐
│  4. WORKSPACE                                          │
│     git bundle (read-only input snapshot)              │
│     bare output repository (write-only, hooks locked)  │
│     reference mounts (read-only or read-write, /context/*)│
└──────────────────────────────┬─────────────────────────┘
                               │
┌──────────────────────────────┴─────────────────────────┐
│  3. CREDENTIAL                                         │
│     mitmproxy addon (provider matching, credential     │
│       injection via static key or OAuth resolvers)     │
│     GCE metadata server (dummy token for ADC init)     │
│     proxy.yaml (provider config, allowlist)            │
└──────────────────────────────┬─────────────────────────┘
                               │
┌──────────────────────────────┴─────────────────────────┐
│  2. INTERCEPTION                                       │
│     mitmproxy TLS termination and re-encryption        │
│     proxychains libc-level socket interception         │
│     HTTPS_PROXY / HTTP_PROXY environment variables     │
│     CA certificate shared via named volume             │
└──────────────────────────────┬─────────────────────────┘
                               │
┌──────────────────────────────┴─────────────────────────┐
│  1. ISOLATION                                          │
│     Podman internal network (agent-net)                │
│     Podman external network (proxy-net)                │
│     Container namespaces (PID, network, filesystem)    │
└────────────────────────────────────────────────────────┘
```

Each layer enforces a constraint independently. A failure at one layer (e.g. an mitmproxy bug that allows a blocked request through) does not automatically compromise the layer below it (the network still has no route to the internet from the agent container).

---

## 6. Key Design Decisions

### 6.1 mitmproxy over a forwarding proxy (Squid)

A forwarding proxy like Squid can enforce allowlists for HTTPS connections using the CONNECT method, but it cannot inspect or modify the content of those connections — it only tunnels the encrypted stream.

Credential injection requires modifying request headers inside the TLS layer. This demands full TLS termination (MITM), which mitmproxy provides natively. The tradeoff is that the agent container must trust a custom CA certificate, which is handled automatically by sharing the certificate through a named volume.

The gain is significant: a single proxy handles allowlist enforcement, credential injection, and request logging with full header visibility, using a programmable Python addon API.

### 6.2 proxychains over HTTPS_PROXY environment variable

The HTTPS_PROXY environment variable is respected by curl, wget, Python's `requests` library, and many other tools — but not by Node.js's built-in `https` module. Claude Code and most other Node.js-based harnesses make HTTP requests without consulting HTTPS_PROXY.

proxychains intercepts at the `connect()` syscall level via LD_PRELOAD, which applies regardless of the HTTP client library in use. `dynamic_chain` mode allows direct connections to hosts on the internal network (the proxy itself, the metadata server) while routing all external connections through mitmproxy.

Both mechanisms are set. Tools that respect HTTPS_PROXY use it directly; tools that do not are caught by proxychains.

### 6.3 Fake GCE metadata server over credential file injection

GCP authentication via the Application Default Credentials chain ultimately exchanges a long-lived credential (service account key or user refresh token) for a short-lived access token. Putting the long-lived credential in the agent container would leave it exposed for the entire session.

The GCE metadata server protocol is Google's own mechanism for credential distribution in cloud environments: a well-known HTTP endpoint returns tokens on demand without the caller ever seeing the underlying credential. Implementing a fake version of this server in the proxy container allows the agent to authenticate normally while the actual credential remains exclusively on the proxy side. The fake server returns only dummy tokens — real OAuth tokens are injected by the mitmproxy addon on intercepted requests, keeping credential handling centralised in a single component.

The `GCE_METADATA_HOST` environment variable, respected by all Google auth client libraries, redirects token requests to the fake server without requiring any modification to the agent harness.

### 6.4 Git bundle over direct directory mount

Mounting the project directory directly into the agent container gives the agent write access to `.git/` and all tracked files simultaneously. Even a read-only mount exposes the git history, configuration, and potential credentials stored there.

A git bundle is a single portable file containing the complete history of a branch. When the agent clones from it, it gets a fresh git repository with no mount path back to the host. The agent can commit, branch, and rebase freely inside the container without any of those operations affecting the host repository.

The output bare repository provides the reverse channel. It accepts pushes from the agent but cannot be used to inject code that runs on the host: the `hooks/` directory and `config` file are bind-mounted read-only into the agent container (kernel-enforced, not just `chmod`), and the developer retrieves commits with hook execution explicitly disabled. The `config` bind-mount specifically prevents the agent from setting `core.hooksPath` to a writable directory to bypass the hooks protection.

### 6.5 Centralized session storage

Sessions are stored in a central state directory (`$AGENTBOX_STATE`, defaulting to `$XDG_STATE_HOME/agentbox` or `~/.local/state/agentbox`) rather than inside each project directory. Each session directory is named `<name>-<sha256(project_dir)>`, making lookup by session name and project directory O(1).

The project directory and session name are stored in the session's `compose.yaml` as `x-metadata.project-dir` and `x-metadata.name` (Compose extension fields, ignored by the container runtime). This replaces the previous approach of inferring the project directory from the filesystem path.

This design keeps project directories clean (no `.agentbox/` directories), enables cross-project session management (`agentbox list --all`), and follows the XDG Base Directory Specification for state data.

### 6.6 Unified compose.yaml per session

At `agentbox init` time, `compose-base.yaml` (the shared template defining images, networks, and base environment) is merged with the preset configuration (provider volumes, environment variables, runtime) to produce a single `compose.yaml` in the session directory.

`podman compose -f compose.yaml` uses this self-contained file at runtime. Images are defined once in `compose-base.yaml` and reused across all projects — a change to the proxy's Python addons requires rebuilding one image, not regenerating every session's configuration.

The `compose.yaml` is generated once at init and then user-owned: edits persist across `agentbox start` calls. Running `agentbox init` again regenerates it, preserving the session's web port and any context mounts already registered via `agentbox mount add`.

### 6.7 Hot-reload over proxy restart for allowlist changes

When a developer adds or removes a host from the egress allowlist, the proxy configuration must be reloaded. Restarting the proxy container would terminate all active HTTPS tunnels, potentially interrupting LLM inference requests mid-stream — which may not be recoverable by the agent.

The addon exposes an async HTTP endpoint on port 8082 that re-reads `proxy.yaml` and atomically swaps the in-memory configuration. The CLI's `allow` and `deny` commands call this endpoint via `compose exec`, achieving near-instant allowlist updates without affecting active connections.

### 6.8 Resolver pattern for credential injection

Credential injection uses a `CredentialResolver` abstraction with implementations registered via `__init_subclass__`. Each provider in `proxy.yaml` specifies a `credential_type` (e.g., `static`, `oauth`) that maps to a resolver class. This separates provider matching (L7 request policy rules matching host, port, path, and method) from credential acquisition (reading a file, refreshing an OAuth token), making it straightforward to add new credential protocols without modifying the matching logic.

---

## 7. Extensibility Model

The architecture is designed so that the most common customisations require no code changes:

| Customisation | Mechanism | Requires code change? |
|--------------|-----------|----------------------|
| Add a new LLM provider | Add a stanza to `proxy.yaml` | No |
| Open additional egress | `agentbox allow <host>` | No |
| Switch agent harness | Create or select a preset with the desired `agent_image` and `AGENT_HARNESS` | No |
| Add a read-only reference project | `agentbox mount add` | No |
| Use a different preset per project | `agentbox init --preset` | No |
| Trust a custom CA certificate | Add cert to `custom/certs/`, list in `trusted_certificates` | No |
| Enable VM-level isolation | Install `crun-vm` / `krun`; `runtime: krun` is always generated in the compose | No |
| Add a new harness package | Add a `Containerfile.<name>` in the agent directory | One Containerfile |
| Add a new credential protocol | Add a `CredentialResolver` subclass in `resolvers.py` | Python code |

The proxy's addon interface (mitmproxy's Python API) is the primary extension point for new credential schemes. The `resolvers.py` module handles the common cases: `StaticKeyResolver` for header-based API key injection and `OAuthResolver` for Google OAuth token replacement. New protocols — HMAC signing, OAuth client credentials, AWS SigV4 — can be added as new resolver subclasses without modifying the matching or logging logic.

---

## 8. Quality Attributes

**Security.** The system is designed so that the tools required for the primary attack classes are structurally absent from the agent container, not merely restricted by policy that could be circumvented. The proxy's code surface is intentionally minimal — three Python modules (`addon.py`, `provider.py`, `resolvers.py`), a small HTTP server (`metadata_server.py`), and a shell entrypoint — to make auditing practical.

**Transparency.** Every network request the agent makes is visible. The mitmweb interface shows live traffic. The JSON access log is persistent. Blocked requests are recorded. A developer running an agent for the first time can watch exactly what it does.

**Usability.** The target for starting a new project agentbox is a single command after one-time setup. Configuration is in files, not flags. Mounts, allowlist changes, and harness selection are all handled without stopping and recreating the agentbox from scratch.

**Portability.** The system requires Podman (with `podman compose`) and Python 3 with PyYAML. It runs on any Linux system without root privileges. The optional krun runtime for VM-level isolation requires KVM but is opt-in and does not change the configuration interface.

**Reproducibility.** The proxy and agent images are built once and reused. Each session's directory under `$AGENTBOX_STATE/sessions/` fully describes its configuration. Two developers with the same base images and equivalent session directories get equivalent environments.

---

## 9. Deliberate Non-Goals

**Preventing data exfiltration through the inference endpoint.** The agent is allowed to call the configured LLM API. If it sends sensitive file contents in a prompt to a model the attacker controls, that is outside the scope of network-layer isolation. Mitigations exist (prompt content inspection, rate limiting, audit logging) but they are not built into this architecture.

**Defending against a malicious proxy container.** The proxy is trusted. A compromised proxy image could exfiltrate credentials. The mitigation is to keep the proxy image auditable and build it from source rather than pulling from a registry.

**Process-level isolation within the agent container.** The architecture isolates the agent container from the host but does not restrict what the agent can do within its own container. A contained agent process can still read other files in the container, spawn subprocesses, and use all available CPU and memory. This is a deliberate trade-off for compatibility — adding seccomp profiles or capability restrictions would break legitimate agent workflows.

**Protecting against a developer who deliberately misconfigures the agentbox.** The system trusts the developer's intent. If a developer mounts sensitive files read-write, disables the proxy, or copies real credentials into the agent container, the security properties no longer hold. The design makes the secure path the easy path, not the only path.
