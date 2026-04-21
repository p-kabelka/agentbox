# agentbox — Architecture

> This document describes **why** the system is designed the way it is.
> For **what** each component does in detail, see [SPEC.md](SPEC.md).

---

## 1. Problem Statement

AI coding agents operate with the same OS permissions as the developer who starts them. They can read environment variables, traverse the filesystem, and open arbitrary network connections. This is necessary for legitimate work — installing packages, calling APIs, reading project files — but it creates a symmetric risk: anything the agent can do legitimately, a compromised agent can do maliciously.

The two dominant attack surfaces are:

**Credential theft.** The agent has access to the same API keys and credential files the developer uses. A prompt-injected agent can read these and exfiltrate them over the network, giving an attacker persistent access to the developer's accounts.

**Repository poisoning.** The agent has write access to the project's `.git/` directory. A malicious commit hook planted there executes silently on the developer's machine the next time they run a routine git operation — potentially after the agentbox session is over and the threat is no longer visible.

The architectural goal is to eliminate both attack surfaces without making the legitimate workflow impractical.

---

## 2. Use Cases

The following scenarios define the space the architecture must support. Each drives specific structural requirements.

### 2.1 Isolated feature implementation

A developer assigns an agent to implement a feature on a branch. The developer's primary concern is that the agent, if manipulated through malicious content in the codebase (injected comments, fixture files, README instructions), should not be able to cause lasting harm.

**Architectural requirements:** The agent must be able to read the project, make commits, and call an LLM API — but must not be able to exfiltrate credentials, reach arbitrary endpoints, or modify the host git repository. Output must flow through a reviewable channel before it reaches the host.

### 2.2 Analysis of untrusted code

A developer uses an agent to analyze a third-party pull request, a dependency audit, or an unfamiliar codebase. The source code may contain deliberate prompt injections designed to cause the agent to exfiltrate data or plant persistent access.

**Architectural requirements:** Network isolation must hold even if the agent is fully compromised. Credential exfiltration must be structurally impossible — not merely unlikely. All network activity during the session must be logged and available for post-session review.

### 2.3 Cross-repository context

An agent implementing a feature in project A needs to read a shared library, design system, or API specification from project B. The developer wants to provide this context without giving the agent write access to project B or exposing credentials stored in project B's history or configuration.

**Architectural requirements:** Reference projects must be mountable read-only with their `.git/` directories excluded from the agent's view. The agent must be able to read file content but not modify the referenced project or discover credentials embedded in it.

### 2.4 Provider portability

A team uses different LLM providers depending on task sensitivity — a hosted provider for code generation, a local or private endpoint for tasks involving confidential data. The agent workflow and harness must be identical regardless of which provider is active.

**Architectural requirements:** Provider selection must be configuration-only. The agent harness must require no modification when switching providers. Credential handling must be fully abstracted by the proxy so the agent is unaware of which provider it is calling and never holds a credential specific to any of them.

### 2.5 Parallel workstreams

A developer runs multiple agentboxes simultaneously — one per feature branch, or multiple agents exploring different approaches to the same problem. Each agentbox must be fully isolated from the others; a credential or network event in one must have no effect on another.

**Architectural requirements:** Agentboxes must not share networks, volumes, or host ports. Container images must be shared to avoid duplicating disk usage and build time. Creating a new agentbox must not require modifying any existing agentbox's state.

### 2.6 Supervised exploration

A developer runs an agent for the first time on a project and wants to observe its network behaviour before granting it broad trust. During the session they may decide to open additional egress dynamically (e.g. allow a package registry the agent requests) or determine that a requested host should remain blocked.

**Architectural requirements:** All network traffic must be visible in real time with enough detail to make a policy decision. The egress allowlist must be modifiable without restarting the agent. Blocked requests must be clearly identifiable, with the destination hostname visible, so the developer can act on them.

---

## 3. Design Principles

### 3.1 The agent container is untrusted by assumption

The core design decision is to treat the agent container as a potentially hostile process from the start — not as a hardened environment that we try to protect. This shifts the question from "how do we prevent the agent from doing bad things?" to "how do we structure the environment so the tools for bad things aren't present?"

This is analogous to the principle of least privilege in system design: rather than auditing every action, we narrow what actions are structurally possible.

### 3.2 Credentials are separated by container boundary

Real credentials live only in the proxy container. The agent container receives dummy placeholders. The proxy intercepts every outbound request and swaps in the real credential before forwarding.

This means even a fully compromised agent — one that dumps its entire environment and filesystem — cannot exfiltrate a usable credential. The credential does not exist in the agent's address space.

### 3.3 The network is a one-way valve

All agent traffic must pass through the proxy, which enforces an allowlist. The proxy can reach the internet; the agent cannot. This is structurally enforced by the container network topology, not by firewall rules the agent could potentially bypass.

The proxy acts as both a gateway and an inspection point. Every request is logged. Blocked requests are recorded. The developer can see everything the agent tried to do.

### 3.4 Git state flows through content-addressed objects only

The agent never has access to the host's `.git/` directory. Instead:

- **Input** arrives as a git bundle — a portable, read-only snapshot.
- **Output** leaves through a bare repository that the agent pushes to.

The host never executes anything from the bare repository automatically. Output is always fetched with hook execution explicitly disabled, and the developer reviews commits before merging.

This converts the git interface from an execution surface into a data transport.

### 3.5 Isolation must not require host privilege

The system must be deployable by a developer on a workstation without `sudo`. Any design that requires modifying host iptables, installing kernel modules, or editing system-wide configuration is a deployment barrier that will limit adoption and introduce operational complexity.

Podman's container networking achieves network isolation without host-level privilege. The internal network flag prevents external routing entirely, enforced inside the network namespace of the container runtime.

### 3.6 Configuration over code for extensibility

The proxy's allowlist, credential injection rules, and provider settings are all driven by a YAML configuration file. Adding a new LLM provider requires editing a configuration file, not writing code. This makes the system usable by developers who are not contributors to the project.

---

## 4. Trust Boundaries

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

**Host → Proxy**: The host mounts real credentials into the proxy as read-only volumes and starts the proxy container. The proxy is trusted to enforce policy correctly. It runs minimal, auditable code.

**Proxy → Agent (inbound)**: The proxy issues short-lived tokens (Vertex) or serves as a CONNECT-tunneling proxy (direct APIs). The agent receives only what it needs to make API calls — not the credentials that authorise those calls.

**Agent → Proxy (outbound)**: All agent network traffic is intercepted by the proxy. The proxy is the only entity that can reach the internet. The agent cannot bypass this without a path that does not exist in the network topology.

**Agent → Host (filesystem)**: The agent has no write-capable mount into the host filesystem. The git bundle is read-only. The output bare repository has immutable hooks. Other mounts are explicitly read-only.

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
│     reference mounts (read-only, /context/*)           │
└──────────────────────────────┬─────────────────────────┘
                               │
┌──────────────────────────────┴─────────────────────────┐
│  3. CREDENTIAL                                         │
│     mitmproxy addon (header swap; Vertex token swap)   │
│     GCE metadata server (dummy token for ADC init)     │
│     proxy.yaml (provider config, allowlist)            │
└──────────────────────────────┬─────────────────────────┘
                               │
┌──────────────────────────────┴─────────────────────────┐
│  2. INTERCEPTION                                       │
│     mitmproxy TLS termination and re-encryption        │
│     proxychains libc-level socket interception         │
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

The gain is significant: a single proxy handles allowlist enforcement, credential injection, and request logging with full header visibility, using a programmable Python addon API — all implemented in one auditable module (`addons/addon.py`).

### 6.2 proxychains over HTTPS_PROXY environment variable

The HTTPS_PROXY environment variable is respected by curl, wget, Python's `requests` library, and many other tools — but not by Node.js's built-in `https` module. Claude Code and most other Node.js-based harnesses make HTTP requests without consulting HTTPS_PROXY.

proxychains intercepts at the `connect()` syscall level via LD_PRELOAD, which applies regardless of the HTTP client library in use. `dynamic_chain` mode allows direct connections to hosts on the internal network (the proxy itself, the metadata server) while routing all external connections through mitmproxy.

Both mechanisms are set. Tools that respect HTTPS_PROXY use it directly; tools that do not are caught by proxychains.

### 6.3 Fake GCE metadata server over credential file injection

GCP authentication via the Application Default Credentials chain ultimately exchanges a long-lived credential (service account key or user refresh token) for a short-lived access token. Putting the long-lived credential in the agent container would leave it exposed for the entire session.

The GCE metadata server protocol is Google's own mechanism for credential distribution in cloud environments: a well-known HTTP endpoint returns tokens on demand without the caller ever seeing the underlying credential. Implementing a fake version of this server in the proxy container allows the agent to authenticate normally while the actual credential remains exclusively on the proxy side.

The `GCE_METADATA_HOST` environment variable, respected by all Google auth client libraries, redirects token requests to the fake server without requiring any modification to the agent harness.

### 6.4 Git bundle over direct directory mount

Mounting the project directory directly into the agent container gives the agent write access to `.git/` and all tracked files simultaneously. Even a read-only mount exposes the git history, configuration, and potential credentials stored there.

A git bundle is a single portable file containing the complete history of a branch. When the agent clones from it, it gets a fresh git repository with no mount path back to the host. The agent can commit, branch, and rebase freely inside the container without any of those operations affecting the host repository.

The output bare repository provides the reverse channel. It accepts pushes from the agent but cannot be used to inject code that runs on the host: the `hooks/` directory is bind-mounted read-only into the agent container (kernel-enforced, not just `chmod`), and the developer retrieves commits with hook execution explicitly disabled.

### 6.5 Two compose files over a generated monolith

The base compose file defines services, networks, volumes, and all configuration that is identical across every project. Per-project concerns (mitmweb port, git mounts, reference mounts) live in a generated override file in the project's `.agentbox/` directory.

`podman compose -f base.yaml -f override.yaml` merges these at runtime. This means images are defined once and reused across all projects. A change to the proxy's Python addons requires rebuilding one image, not regenerating every project's configuration.

The override file is regenerated from persistent state files (`mounts.yaml`, `.env`, presence of `source.bundle`/`output-<name>.git`) rather than being hand-maintained, so it can be safely overwritten without losing user configuration.

---

## 7. Extensibility Model

The architecture is designed so that the most common customisations require no code changes:

| Customisation | Mechanism | Requires code change? |
|--------------|-----------|----------------------|
| Add a new LLM provider | Add a stanza to `proxy.yaml` | No |
| Open additional egress | `agentbox allow <host>` | No |
| Switch agent harness | Set `AGENT_HARNESS` | No |
| Add a read-only reference project | `agentbox mount add` | No |
| Use a different preset per project | `agentbox init --preset` | No |
| Enable VM-level isolation | Install `crun-vm` / `krun`; `runtime: krun` is always generated in the override | No |
| Add a new harness package | Extend the agent Containerfile | One Containerfile change |
| Add a new credential protocol | New addon script or metadata server | Python code |

The proxy's addon interface (mitmproxy's Python API) is the primary extension point for new credential schemes. The `addon.py` module handles the common cases: header-based API key injection for direct providers, and Vertex OAuth token replacement. New protocols — HMAC signing, OAuth client credentials, AWS SigV4 — can be added as separate addon scripts or extending the existing module without modifying the rest of the system.

---

## 8. Quality Attributes

**Security.** The system is designed so that the tools required for the primary attack classes are structurally absent from the agent container, not merely restricted by policy that could be circumvented. The proxy's code surface is intentionally minimal — one Python addon module (`addon.py`), a small HTTP server (`metadata_server.py`), and a shell entrypoint — to make auditing practical.

**Transparency.** Every network request the agent makes is visible. The mitmweb interface shows live traffic. The JSON access log is persistent. Blocked requests are recorded. A developer running an agent for the first time can watch exactly what it does.

**Usability.** The target for starting a new project agentbox is a single command after one-time setup. Configuration is in files, not flags. Mounts, allowlist changes, and harness selection are all handled without stopping and recreating the agentbox from scratch.

**Portability.** The system requires Podman and Python 3 with PyYAML. It runs on any Linux system without root privileges. The optional krun runtime for VM-level isolation requires KVM but is opt-in and does not change the configuration interface.

**Reproducibility.** The proxy and agent images are built once and reused. A project's `.agentbox/` directory fully describes its configuration. Two developers with the same base images and the same `.agentbox/` directory get equivalent environments.

---

## 9. Deliberate Non-Goals

**Preventing data exfiltration through the inference endpoint.** The agent is allowed to call the configured LLM API. If it sends sensitive file contents in a prompt to a model the attacker controls, that is outside the scope of network-layer isolation. Mitigations exist (prompt content inspection, rate limiting, audit logging) but they are not built into this architecture.

**Defending against a malicious proxy container.** The proxy is trusted. A compromised proxy image could exfiltrate credentials. The mitigation is to keep the proxy image auditable and build it from source rather than pulling from a registry.

**Process-level isolation within the agent container.** The architecture isolates the agent container from the host but does not restrict what the agent can do within its own container. A contained agent process can still read other files in the container, spawn subprocesses, and use all available CPU and memory. This is a deliberate trade-off for compatibility — adding seccomp profiles or capability restrictions would break legitimate agent workflows.

**Protecting against a developer who deliberately misconfigures the agentbox.** The system trusts the developer's intent. If a developer mounts sensitive files read-write, disables the proxy, or copies real credentials into the agent container, the security properties no longer hold. The design makes the secure path the easy path, not the only path.
