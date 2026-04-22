# agentbox Technical Specification

## 1. Overview

agentbox creates isolated, credential-safe workspaces for running AI coding agents such as Claude Code, OpenCode, and Codex CLI. Each agentbox is a pair of Podman containers — a proxy and an agent — connected by a private internal network that provides full network mediation without requiring host-level privileges.

The core problem is that AI agents run with broad process permissions. If an agent is compromised through prompt injection or malicious content in the working directory, it can read API credentials from environment variables or well-known paths, exfiltrate them to attacker-controlled endpoints, make arbitrary outbound network calls, or inject malicious hooks into the host's `.git/` directory that execute silently when a developer runs routine git operations.

agentbox defends against these threats by structuring the environment so that the primitives required for attacks are never available to the agent process: real credentials live only in the proxy container, external TCP is only reachable through a TLS-intercepting proxy that enforces an allowlist, and the host git repository is never mounted into the agent container — instead, a read-only bundle is used for input and a bare repository with immutable hooks is used for output.

---

## 2. Threat Model

### 2.1 Primary Threats Defended Against

**API credential exfiltration**
A compromised agent attempts to read `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `application_default_credentials.json`, OAuth tokens, or any other long-lived credential and transmit it to an attacker-controlled endpoint. agentbox ensures that real credentials are never present in the agent container's environment or filesystem. The agent receives dummy placeholder values; the proxy rewrites requests with real credentials before forwarding them upstream.

**Arbitrary outbound network access**
The agent attempts to connect to an attacker-controlled server to exfiltrate data, receive instructions, or download malicious payloads. The agent container is attached only to a Podman internal network with no default gateway. All external TCP connections are forced through the mitmproxy instance, which enforces a per-project allowlist and blocks connections to non-whitelisted hosts with an HTTP 403 before the connection is established.

**Git repository poisoning**
The agent writes executable scripts to `.git/hooks/` (e.g., `post-checkout`, `pre-push`) that would execute on the host when a developer runs routine git operations after retrieving the agent's output. agentbox ensures the host's `.git/` directory is never mounted into the agent container. The agent writes commits to a bare output repository whose `hooks/` directory is created with `chmod 555` (no write permission for any user), and the host fetches commits using `git -c core.hooksPath=/dev/null`.

**Prompt injection via untrusted working directory content**
Malicious content embedded in files, commit messages, or other project artifacts causes the agent to interpret attacker instructions and take actions outside the intended task scope. agentbox does not eliminate this class of threat at the network layer, but the other controls above ensure that even a successfully injected agent cannot exfiltrate real credentials, reach arbitrary network destinations, or poison the host git state.

### 2.2 Out of Scope / Residual Risks

**Data exfiltration through the LLM prompt itself.** A compromised agent can include the contents of sensitive files in its prompt to any allowed inference endpoint. The inference provider receives this data. This is out of scope for network-layer isolation; mitigation relies on using minimal-scope API keys and reviewing session transcripts.

**Covert timing channels.** An agent can encode information in request timing patterns observable to the LLM provider. This is not addressed.

**Agent writing malicious scripts to tracked hook directories.** If the project tracks a hooks directory (e.g., `.husky/`, `scripts/`) and the agent adds a malicious script to it, that script becomes part of the git commit and could execute on the host after the developer merges. This is mitigated by code review of the fetched branch before merging; it is not blocked automatically.

---

## 3. Use Cases

### Use Case 1: Isolated Feature Development

A developer creates a agentbox for a feature branch, provides the agent with a task prompt, and lets it implement the feature autonomously. The agent works inside the container, commits its changes to the output remote, and the developer reviews the auto-fetched output — including any new scripts or hook files — before merging into their working branch. The host repository and credentials are never at risk during the agent session.

### Use Case 2: Multi-Repo Context

The agent needs read access to a shared internal library while implementing changes to the main project. The developer adds the library path with `agentbox mount add ~/libs/shared-lib`. The library is mounted read-only into the agent container as an additional reference directory. The agent can read its source but cannot modify it or push to it. The mount configuration is stored in `mounts.yaml` and persists across agentbox restarts.

### Use Case 3: Restricted Provider Switching

A team running inference through Anthropic's direct API wants to switch to Vertex AI (e.g., for quota reasons) without changing the agent harness or the developer's workflow. The developer initialises the session with `agentbox init --preset vertex --start`. The proxy container starts the fake GCE metadata server; the agent's `GCE_METADATA_HOST` points to it so the Google auth library initialises normally, and the proxy addon replaces the dummy token with a real OAuth token on every Vertex API call. The agent harness code is unchanged; no GCP service account key is ever present in the agent container.

### Use Case 4: Parallel Agentboxes

A developer runs multiple simultaneous agentboxes for different projects or branches — for example, one agent refactoring a backend service and another writing tests for a frontend library. Each agentbox has its own isolated Podman network, its own proxy container with a separate credential environment, and its own web UI port assigned from a configurable port range. The agentboxes do not share network namespaces and cannot observe each other's traffic.

---

## 4. Architecture

### 4.1 Network Topology

Two Podman networks are created per agentbox:

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

The agent container receives a dummy placeholder value for the relevant API key environment variable (e.g., `ANTHROPIC_API_KEY=dummy`). When the agent makes an HTTPS call to the provider's API, the connection is intercepted by mitmproxy. The `addon.py` module reads the allowlist from `config/proxy.yaml`, verifies the destination host is permitted, strips the dummy key from the request headers, and injects the real key from the proxy container's environment. The real key never enters the agent container's memory or filesystem.

**GCP Vertex AI**

The agent container has `GCE_METADATA_HOST=proxy:9090`. The credential flow uses a two-stage dummy-token mechanism:

1. The Google auth library in the agent calls the fake metadata server at `proxy:9090/service-accounts/default/token`. The metadata server returns a fixed dummy token (`dummy-replaced-by-proxy`) — it holds no real GCP credentials. This satisfies the ADC initialization check and gives the agent a token to use in subsequent requests.

2. When the agent sends a request to the Vertex inference endpoint (`{region}-aiplatform.googleapis.com`) carrying the dummy token in its `Authorization` header, the mitmproxy addon (`addon.py`) intercepts it. The addon calls `google.auth.default()` on the proxy side — where the real credentials live via `GOOGLE_APPLICATION_CREDENTIALS` or Application Default Credentials — and replaces the dummy token with a real short-lived OAuth 2.0 access token before forwarding the request upstream.

The real token (1-hour TTL, scoped to `https://www.googleapis.com/auth/cloud-platform`) does reach the agent container's process environment after the first token refresh, but the long-lived credential (service account key or refresh token) never does.

### 4.4 Traffic Interception

proxychains4 is configured in `dynamic_chain` mode inside the agent container:

- Connections to `proxy:8080` and `proxy:9090` are within the same `agent-net` network and are reachable directly without going through a SOCKS proxy.
- All connections to external hosts have no direct route (no default gateway on `agent-net`). proxychains intercepts these at the libc socket layer via `LD_PRELOAD` and tunnels them to mitmproxy using an HTTP CONNECT request.

mitmproxy terminates the TLS session from the agent, inspects the plaintext request, applies the allowlist, injects credentials if applicable, re-encrypts the request, and forwards it to the real upstream server. The mitmproxy CA certificate is installed in the agent container's system trust store (via `update-ca-trust`) and exported via `NODE_EXTRA_CA_CERTS` and `REQUESTS_CA_BUNDLE` so that system-level, Node.js, and Python TLS verification all succeed.

### 4.5 Git Isolation: The Two-Remote Pattern

**Source (read-only input)**

`agentbox init` creates a git bundle of the current branch at session creation time:

```
git bundle create .agentbox/sessions/<name>/source.bundle HEAD <branch>
```

The bundle is mounted read-only into the agent container at `/source/project.bundle`. `start.sh` clones from this bundle, producing a fresh `.git` directory inside the container with no connection to the host's `.git/` directory. The host git working directory and object store are never accessible from within the container.

**Output (write-only barrier)**

A bare repository is created at `.agentbox/sessions/<name>/output.git/` on the host during `agentbox init`. Immediately after creation, its `hooks/` directory is set to `chmod 555` as a host-side safeguard. The bare repo is mounted into the agent container as a git remote named `output`, and the hooks directory is separately bind-mounted read-only (`/output/repo.git/hooks:ro`) on top of the writable repo mount. This second mount is kernel-enforced: the agent cannot write to or chmod the hooks directory regardless of process permissions, without `CAP_SYS_ADMIN`. The agent can push commits to the `output` remote.

A host-side git remote named `agentbox-<name>` is registered pointing to the bare repo. When the agent container exits, `agentbox start` automatically fetches from it:

```
git -c core.hooksPath=/dev/null fetch agentbox-<name>
```

The `core.hooksPath=/dev/null` flag ensures that even if the agent has somehow written files into `hooks/` (which `chmod 555` prevents), none of them execute during the fetch. Fetched branches land under `agentbox-<name>/` for review before any merge. The developer can also fetch mid-session by running `git fetch agentbox-<name>` directly.

---

## 5. Components

### 5.1 `proxy/` Container

**mitmweb**

Full TLS MITM proxy. Loads one Python addon at startup.

**`addons/addon.py`**

A single mitmproxy addon module implementing allowlist enforcement, credential injection, and structured access logging via the `AgentboxAddon` class.

Reads `config/proxy.yaml` at startup and builds:
- An allowlist of hostname patterns (from `allowed_hosts` across all enabled providers plus `extra_allowed_hosts`).
- Injection rules: for each non-Vertex provider, a `(host_patterns, header, value)` tuple constructed from the provider's `inject_header`, `inject_prefix`, and the real API key read from the proxy container's environment.
- Vertex state: the Google credentials object loaded via `google.auth.default()`, a threading lock for safe token refresh, and the project/region configuration.

**`request()` hook** — for each intercepted request:
1. Checks the destination hostname against the allowlist using `fnmatch` patterns. If not listed, responds with HTTP 403 (the upstream connection is never opened) and logs the block.
2. For Vertex requests: if the host matches the configured Vertex endpoint and the `Authorization` header contains the dummy token, calls `google.auth.default()` credentials on the proxy side to obtain (and cache/refresh) a real OAuth token, then replaces the header value.
3. For all other enabled providers: injects the real API key into the configured header.

**`response()` hook** — logs all non-blocked responses to stdout as structured JSON. Each entry includes timestamp, method, URL, status code, and round-trip duration. Request headers, response headers, and bodies can be included via `logging` settings in `proxy.yaml` (all off by default except request headers).

Additional hosts are added and removed via `agentbox allow` and `agentbox deny`, which update `config/proxy.yaml` and restart the proxy container so the addon reloads the configuration.

**`metadata_server.py`**

A minimal HTTP server that implements the subset of the GCE instance metadata API used by the Google auth library. Started only when `vertex.metadata_server: true` is set in `proxy.yaml`. Binds to `0.0.0.0:9090` inside the proxy container. Holds real GCP credentials (via `GOOGLE_APPLICATION_CREDENTIALS` or Application Default Credentials mounted from the proxy-side filesystem). On token requests, calls the Google OAuth2 token endpoint and returns a short-lived access token. The long-lived credential is never forwarded to the caller.

### 5.2 `agent/` Container

**proxychains4**

Configured in `dynamic_chain` mode. The chain list contains a single entry: the mitmproxy CONNECT endpoint at `proxy:8080`. All libc-level TCP connections to external hosts are intercepted via `LD_PRELOAD` and tunneled through this chain. Works transparently for Node.js, Python, Go (dynamically linked), and other runtimes that use libc networking.

**`start.sh`**

The container entrypoint (declared as `ENTRYPOINT`, not `CMD`, so any arguments passed to `compose run` or `podman exec` are forwarded to it as `$@`). Performs in order:

1. Polls until the mitmproxy CA certificate appears at `/proxy-ca/mitmproxy-ca-cert.pem`, then installs it into the Fedora system trust store (`update-ca-trust extract`) and exports `NODE_EXTRA_CA_CERTS` and `REQUESTS_CA_BUNDLE` for Node.js and Python clients.
2. If `/source/project.bundle` is present and `/workspace/.git` does not yet exist, clones the bundle to `/workspace`, renames the `origin` remote to `source`, and adds `/output/repo.git` as the `output` remote.
3. Copies preset dotfiles from `/agentbox-dotfiles` into the container's home directory (if the directory is mounted).
4. Applies `stty inlcr` so that Node.js readline in raw mode works correctly with krun's virtio-console (which sends `\n` instead of `\r` for Enter). This is set unconditionally so that any process launched from this container — including a shell the user later starts a harness from — gets the correct terminal mode.
5. If arguments were passed (`$# > 0`), execs them directly. Otherwise execs `AGENT_HARNESS` with `AGENT_HARNESS_ARGS`, or falls back to bash.

**Base image**

`fedora:43` with `curl`, `git`, `proxychains-ng`, and `vim` installed. The Claude Code binary is staged from the host at image build time (`~/.local/bin/claude` → `/usr/local/bin/claude`). Designed to be extended for other harnesses via a derived Containerfile.

### 5.3 `bin/agentbox` CLI

A Python script in `bin/agentbox`, added to `PATH` as part of initial setup. Commands are grouped by function:

| Group | Commands |
|-------|----------|
| Setup | `build`, `update`, `preset list`, `preset edit <proxy\|agent> [name]`, `preset copy <src> <dst>` |
| Project lifecycle | `init [--preset <name>] [--harness <binary>] [--name <name>] [--branch <branch>] [--mount SRC[:DST]] [--start]`, `start [-- CMD]`, `stop`, `remove` |
| Monitoring | `logs`, `web` |
| Egress control | `allow <host>`, `deny <host>` |
| Reference mounts | `mount list`, `mount add <path>`, `mount remove <path>` |
| Observation | `status`, `list` / `ls` |

`agentbox init` is the primary entry point. It creates a timestamped (or named) session directory under `.agentbox/sessions/`, copies the chosen preset's `proxy.yaml` and `agent.yaml`, extracts any inline dotfiles from `agent.yaml`, creates a git bundle of the current branch, initialises the bare output repository (with its `hooks/` directory `chmod 555`), registers the `agentbox-<name>` git remote, assigns a web UI port, writes `.env`, generates `compose.override.yaml`, and optionally launches the session immediately if `--start` is passed.

`agentbox start` re-generates `compose.override.yaml`, starts the proxy in the background (waiting for its health check), then runs a new agent container interactively via `compose run --rm agent`. Any arguments after `--` are forwarded to `start.sh` and override what gets exec'd (e.g. `agentbox start -- tmux` or `agentbox start -- bash`). When the agent container exits, output is auto-fetched. Multiple `agentbox start` calls on the same session run independent agent containers concurrently. Because each agent runs in a krun microVM, `podman exec` cannot reach a running container.

`agentbox allow` and `agentbox deny` append or remove entries from `extra_allowed_hosts` in the session's `config/proxy.yaml`, then restart the proxy container so the addon reloads the configuration. They wait for the proxy health check to pass before returning.

All commands that operate on a specific session accept `--name <name>`. If the project has exactly one session, `--name` is optional and the session is auto-detected.

### 5.4 Configuration Files

Each session is fully self-contained under `.agentbox/sessions/<name>/`.

| File | Location | Purpose | Overwritten by `agentbox init`? |
|------|----------|---------|-------------------------------|
| `proxy.yaml` (preset) | `$AGENTBOX_HOME/presets/<name>/` | Global default provider config | N/A — global |
| `agent.yaml` (preset) | `$AGENTBOX_HOME/presets/<name>/` | Global default agent env and dotfiles | N/A — global |
| `config/proxy.yaml` | `.agentbox/sessions/<name>/config/` | Per-session provider config, allowlist | No — user-owned after init |
| `config/agent.yaml` | `.agentbox/sessions/<name>/config/` | Per-session agent env overrides, dotfiles | No — user-owned after init |
| `dotfiles/` | `.agentbox/sessions/<name>/dotfiles/` | Extracted dotfiles from `agent.yaml` | Only on first init |
| `mounts.yaml` | `.agentbox/` | Persistent extra reference mounts (shared across sessions) | No — user-owned |
| `compose.override.yaml` | `.agentbox/sessions/<name>/` | Generated Compose overrides (port, volumes, runtime) | Yes — regenerated on each `agentbox start` |
| `.env` | `.agentbox/sessions/<name>/` | `AGENTBOX_WEB_PORT`, `AGENT_HARNESS`, `AGENTBOX_NAME` | Created on init; port preserved on subsequent starts |
| `source.bundle` | `.agentbox/sessions/<name>/` | Read-only git snapshot of the source branch | Created once on `agentbox init` |
| `output.git/` | `.agentbox/sessions/<name>/` | Bare repo output barrier | Created once; agent pushes here |

---

## 6. Security Properties

| Property | Mechanism | Guarantee |
|----------|-----------|-----------|
| Real API keys never enter agent container | Proxy-side env; agent sees `dummy` placeholder | Keys absent from agent process memory and filesystem |
| GCP long-lived credentials never enter agent container | Fake metadata server returns dummy tokens; real tokens fetched and injected by proxy addon | SA key / refresh token stays on proxy side |
| Agent cannot make arbitrary internet connections | Podman `internal: true` network; no default gateway | All external TCP must transit mitmproxy |
| Agent can only reach allowlisted API endpoints | mitmproxy `addon.py` enforces allowlist | Non-listed hosts receive HTTP 403; upstream connection never opened |
| Host `.git/` is never accessible to agent | Source delivered as git bundle; no host git mount | No filesystem path to host's `.git/` exists in the container |
| Agent cannot plant executable hooks in host git | `hooks/` is bind-mounted read-only (`:ro`) over the writable repo mount; auto-fetch uses `core.hooksPath=/dev/null` | Kernel-enforced read-only mount prevents writes regardless of process permissions; hooks do not execute on fetch |
| All agent network traffic is auditable | mitmproxy full TLS termination; structured JSON access log | Every request, including blocked ones, is logged with URL and status |
| Agentbox network isolation requires no host privilege | Podman internal networks via container runtime namespaces | No `sudo`, no host iptables or nftables changes needed |

---

## 7. Operational Workflows

### 7.1 First-Time Setup (One Machine)

```bash
# Clone agentbox and add bin/ to PATH (e.g. via ~/.bashrc or ~/.zshrc)
git clone <repo> ~/.local/share/agentbox
export PATH="$HOME/.local/share/agentbox/bin:$PATH"

agentbox preset edit proxy               # enable provider — set anthropic.enabled: true
export ANTHROPIC_API_KEY=sk-ant-...     # add to shell profile

agentbox build                           # builds container images; ~2 minutes; once per machine
```

`AGENTBOX_HOME` defaults to `~/.local/share/agentbox` and can be overridden in the environment.

### 7.2 Starting a Project Agentbox

```bash
cd ~/projects/my-app

# Init and launch in a single step:
agentbox init --start                    # uses default preset and harness (claude)

# Or specify options:
agentbox init --preset webdev --harness opencode --start

# Or init first, then start separately:
agentbox init --name mywork
agentbox start --name mywork
# proxy starts in background; agent starts interactively
```

### 7.3 During a Session

```bash
agentbox allow pypi.org                  # add a host to the egress allowlist (restarts proxy)
agentbox deny pypi.org                   # remove it (restarts proxy)

agentbox logs                            # tail the JSON access log from the proxy
agentbox web                             # print the mitmweb traffic-monitor URL

agentbox mount add ~/libs/shared-lib     # add a read-only reference mount
agentbox start                           # restart session to apply new mount

agentbox list                            # list all sessions for this project with status
agentbox status                          # list all running agentbox containers across projects
```

### 7.4 Retrieving Agent Output

```bash
# Inside the agent container, the agent runs:
#   git push output HEAD:agent-work

# Output is auto-fetched from the output repo when the session ends (agent container exits).
# The CLI prints the available remote branches and suggests a merge command.

# To fetch mid-session without stopping:
git fetch agentbox-mywork                # core.hooksPath=/dev/null applied automatically at session end

git log agentbox-mywork/agent-work       # review commit history
git diff HEAD agentbox-mywork/agent-work # review all changes before merging
git merge agentbox-mywork/agent-work     # merge after review

agentbox stop                            # tear down containers and networks
agentbox remove                          # stop, delete session dir, output repo, and git remote
```

---

## 8. Extension Points

**New LLM provider**

Add a stanza to `config/proxy.yaml` specifying `name`, `enabled`, `api_key_env`, `inject_header`, `inject_prefix`, and `allowed_hosts`. The injector addon reads this configuration at startup and on reload. No code changes are required.

**New agent harness**

For harnesses that are not already installed in the agent image, extend the Containerfile:

```dockerfile
FROM localhost/agentbox-agent:latest
RUN npm install -g opencode   # or pip install codex, etc.
```

Then rebuild with `agentbox build`. Set `AGENT_HARNESS=<binary>` via `--harness <binary>` on `agentbox init`, or by setting it in `config/agent.yaml` under `environment`. `start.sh` execs the specified binary as the final step, passing `AGENT_HARNESS_ARGS` and any additional arguments.

**VM-level isolation**

Install `crun-vm` / `krun` on the host. `agentbox start` always generates `compose.override.yaml` with `runtime: krun` for the agent container; if `krun` is present, each agent runs in its own KVM microVM. The two-network topology and credential flow are unchanged; the isolation boundary moves from Linux namespaces to a hypervisor boundary.

**Custom preset**

```bash
agentbox preset copy default mypreset
agentbox preset edit proxy mypreset
agentbox init --preset mypreset --start
```

Presets are stored in `$AGENTBOX_HOME/presets/<name>/` and contain a `proxy.yaml` (provider config) and optionally an `agent.yaml` (environment overrides and dotfiles). Copying from `default` gives a starting point with all fields documented.

---

## 9. Known Limitations

**Data exfiltration via LLM prompts.** A compromised agent can include the contents of sensitive files in its prompts to any allowed inference endpoint. The inference provider receives this data. This is not addressable at the network layer because the traffic is legitimate use of the allowed endpoint. Mitigation: use minimal-scope API keys; review session transcripts; do not allow the agent to access files outside the project scope.

**Access token window (Vertex AI).** A 1-hour OAuth access token is present in the agent container's process environment. An agent that reads and exfiltrates this token has up to 1 hour to use it, limited to the IAM roles bound to the service account. Mitigation: bind the service account to the minimum required roles (e.g., `roles/aiplatform.user` only).

**DNS resolution of external hostnames.** The agent container can resolve external hostnames via the container runtime's DNS resolver, but cannot TCP-connect to them (no default gateway). DNS-based data exfiltration (encoding data in query labels sent to an attacker-controlled nameserver) is not blocked. Mitigation: configure a restricted DNS resolver in the agent container and add `/etc/hosts` entries for all required hostnames, disabling recursive resolution.

**`core.hooksPath` set in tracked configuration files.** If the project tracks a file (e.g., `.gitconfig`, `.husky/.huskyrc`) that sets `core.hooksPath = ./hooks`, and the agent adds a malicious executable script to that directory, the script becomes part of the git commit and could execute on the host after the developer merges and runs a git operation. The `chmod 555` on `output.git/hooks/` does not protect against this because the malicious script lives in the tracked working tree, not in the bare repo's hooks directory. Mitigation: inspect all new or modified scripts in hook-related directories before merging the fetched branch.

**proxychains LD_PRELOAD bypass by static binaries.** proxychains4 intercepts socket calls by injecting a shared library via `LD_PRELOAD`, which applies only to dynamically linked executables. A static binary (one that does not use libc's dynamic linker) would bypass proxychains and, if it had a way to route to the internet, could make unmediated connections. In practice, no common agent harness ships as a fully static binary; this is noted as a residual risk for custom harness configurations.
