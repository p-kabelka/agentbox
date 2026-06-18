# agentbox Technical Specification

## 1. Overview

agentbox creates isolated, credential-safe workspaces for running AI coding agents such as Claude Code, OpenCode, and Codex CLI. Each agentbox is a pair of Podman containers — a proxy and an agent — connected by a private internal network that provides full network mediation without requiring host-level privileges.

The core problem is that AI agents run with broad process permissions. If an agent is compromised through prompt injection or malicious content in the working directory, it can read API credentials from environment variables or well-known paths, exfiltrate them to attacker-controlled endpoints, make arbitrary outbound network calls, or inject malicious hooks into the host's `.git/` directory that execute silently when a developer runs routine git operations.

agentbox defends against these threats by structuring the environment so that the primitives required for attacks are never available to the agent process: real credentials live only in the proxy container, external TCP is only reachable through a TLS-intercepting proxy that enforces an allowlist, and the host git repository is never mounted into the agent container — instead, a read-only bundle is used for input and a bare repository with immutable hooks is used for output.

For the internal structure and design decisions behind this architecture, see [ARCHITECTURE.md](ARCHITECTURE.md).

---

## 2. Threat Model

### 2.1 Primary Threats Defended Against

**API credential exfiltration**
A compromised agent attempts to read `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `application_default_credentials.json`, OAuth tokens, or any other long-lived credential and transmit it to an attacker-controlled endpoint. agentbox ensures that real credentials are never present in the agent container's environment or filesystem. The agent receives dummy placeholder values; the proxy rewrites requests with real credentials before forwarding them upstream.

**Arbitrary outbound network access**
The agent attempts to connect to an attacker-controlled server to exfiltrate data, receive instructions, or download malicious payloads. The agent container is attached only to a Podman internal network with no default gateway. All external TCP connections are forced through the proxy, which enforces a per-project allowlist and blocks connections to non-whitelisted hosts with an HTTP 403 before the connection is established.

**Git repository poisoning**
The agent writes executable scripts to `.git/hooks/` (e.g., `post-checkout`, `pre-push`) that would execute on the host when a developer runs routine git operations after retrieving the agent's output. agentbox ensures the host's `.git/` directory is never mounted into the agent container. The agent writes commits to a bare output repository whose `hooks/` directory and `config` file are bind-mounted read-only (kernel-enforced), and the host fetches commits using `git -c core.hooksPath=/dev/null`.

**Prompt injection via untrusted working directory content**
Malicious content embedded in files, commit messages, or other project artifacts causes the agent to interpret attacker instructions and take actions outside the intended task scope. agentbox does not eliminate this class of threat at the network layer, but the other controls above ensure that even a successfully injected agent cannot exfiltrate real credentials, reach arbitrary network destinations, or poison the host git state.

### 2.2 Out of Scope / Residual Risks

**Data exfiltration through the LLM prompt itself.** A compromised agent can include the contents of sensitive files in its prompt to any allowed inference endpoint. The inference provider receives this data. This is out of scope for network-layer isolation; mitigation relies on using minimal-scope API keys and reviewing session transcripts.

**Covert timing channels.** An agent can encode information in request timing patterns observable to the LLM provider. This is not addressed.

**Agent writing malicious scripts to tracked hook directories.** If the project tracks a hooks directory (e.g., `.husky/`, `scripts/`) and the agent adds a malicious script to it, that script becomes part of the git commit and could execute on the host after the developer merges. This is mitigated by code review of the fetched branch before merging; it is not blocked automatically.

---

## 3. Use Cases

### Use Case 1: Isolated Feature Development

A developer creates an agentbox for a feature branch, provides the agent with a task prompt, and lets it implement the feature autonomously. The agent works inside the container, commits its changes, and pushes them to the output remote. The developer reviews the auto-fetched output — including any new scripts or hook files — before merging into their working branch. The host repository and credentials are never at risk during the agent session.

### Use Case 2: Multi-Repo Context

The agent needs read access to a shared internal library while implementing changes to the main project. The developer adds the library path with `agentbox mount add ~/libs/shared-lib`. The library is mounted read-only into the agent container as an additional reference directory. The agent can read its source but cannot modify it or push to it. The mount is recorded in the session's `compose.yaml` and persists across agentbox restarts. Writable mounts are also supported via `--rw-mount` or `agentbox mount add -w` for cases where the agent needs to write to a reference directory.

### Use Case 3: Restricted Provider Switching

A team running inference through Anthropic's direct API wants to switch to Vertex AI (e.g., for quota reasons) without changing the agent harness or the developer's workflow. The developer creates a session with a Vertex-enabled preset. The proxy container starts a fake GCE metadata server; the agent's Google auth library initialises normally with dummy tokens, and the proxy replaces the dummy token with a real OAuth token on every Vertex API call. The agent harness code is unchanged; no GCP service account key is ever present in the agent container.

### Use Case 4: Parallel Agentboxes

A developer runs multiple simultaneous agentboxes for different projects or branches — for example, one agent refactoring a backend service and another writing tests for a frontend library. Each agentbox has its own isolated Podman network, its own proxy container with a separate credential environment, and its own web UI port assigned from a configurable port range. The agentboxes do not share network namespaces and cannot observe each other's traffic.

### Use Case 5: Supervised Exploration

A developer runs an agent for the first time on a project and wants to observe its network behaviour before granting it broad trust. During the session they may decide to open additional egress dynamically (e.g., allow a package registry the agent requests) or determine that a requested host should remain blocked. All network traffic is visible in real time via the mitmweb UI. The egress allowlist is modifiable without restarting the agent or dropping active connections. Blocked requests show the destination hostname so the developer can act on them.

---

## 4. System Behavior

### 4.1 Network Isolation

The agent container has no default gateway to the internet. It is attached only to an internal Podman network. The proxy container is attached to both the internal network and an external network with internet connectivity.

All agent TCP connections to external hosts are routed through the proxy. The proxy is the sole egress point. This isolation is enforced by the container runtime's network namespace, not by host-level firewall rules, so no host privileges are required.

### 4.2 Credential Separation

**Direct API providers (Anthropic, OpenAI, etc.)**

The agent container receives dummy placeholder values for API key environment variables (e.g., `ANTHROPIC_API_KEY=dummy`). When the agent makes an HTTPS call to a provider's API, the connection is intercepted by the proxy. The proxy verifies the destination host is permitted, strips the dummy key from the request headers, and injects the real key. The real key never enters the agent container's memory or filesystem.

The proxy supports reading API keys from environment variables or from files mounted at a secrets path, allowing teams to avoid placing secrets in shell profiles.

**GCP Vertex AI**

The agent container has `GCE_METADATA_HOST` pointed at the proxy's metadata server. The credential flow uses a two-stage dummy-token mechanism:

1. The Google auth library in the agent calls the fake metadata server for a token. The metadata server returns a fixed dummy token (`dummy-replaced-by-proxy`) — it holds no real GCP credentials. This satisfies the ADC initialization check and gives the agent a token to use in subsequent requests.

2. When the agent sends a request to the Vertex inference endpoint carrying the dummy token in its `Authorization` header, the proxy intercepts it. The proxy obtains a real OAuth 2.0 access token using credentials available only on the proxy side (via `GOOGLE_APPLICATION_CREDENTIALS` or Application Default Credentials), and replaces the dummy token before forwarding the request upstream.

The long-lived credential (service account key or refresh token) never enters the agent container. The credential directory (e.g., `~/.config/gcloud`) is mounted into the proxy container via the preset's `proxy_volumes` configuration, keeping provider-specific mount logic out of the agentbox manager.

### 4.3 Traffic Mediation

All agent traffic passes through a TLS-intercepting proxy. The proxy terminates TLS from the agent, inspects the plaintext request, and either allows or blocks it.

**Allowlist enforcement:** Each request's destination hostname is checked against a configurable allowlist built from enabled provider hosts and extra allowed hosts. Non-listed hosts receive an HTTP 403 response; the upstream connection is never opened. The 403 response includes the blocked hostname and a suggested `agentbox allow` command.

**Credential injection:** For requests to allowed provider endpoints, the proxy injects real credentials into the appropriate request header. Injection is scoped by host pattern (fnmatch), path pattern (fnmatch), and optionally a token-replacement check, ensuring credentials are only injected on actual API calls — not on arbitrary requests to the same host.

**Allowlist updates:** The allowlist can be modified at runtime via `agentbox allow` and `agentbox deny` without restarting the proxy or dropping active connections. This is important because interrupting an LLM inference request mid-stream may not be recoverable.

**Logging:** Every request — including blocked ones — is logged as structured JSON with timestamp, method, URL, status code, and round-trip duration. Request headers, response headers, and bodies can optionally be included via configuration. A web-based traffic monitor is available for real-time inspection.

### 4.4 Git Isolation

**Source (read-only input)**

`agentbox init` creates a git bundle of the current branch at session creation time. The bundle is mounted read-only into the agent container. The agent clones from this bundle, producing a fresh `.git` directory inside the container with no connection to the host's `.git/` directory.

**Output (write-only barrier)**

A bare repository is created on the host during `agentbox init`. Its `hooks/` directory and `config` file are bind-mounted read-only into the agent container (kernel-enforced). This prevents the agent from writing executable hooks or modifying git configuration (such as setting `core.hooksPath` to bypass the hooks protection) regardless of process permissions. The agent pushes commits to this repository using the `origin` remote, with `push.autoSetupRemote` enabled so that `git push` works without specifying remote or refspec.

A host-side git remote is registered pointing to the bare repo. When the agent container exits, the CLI automatically fetches from it with `core.hooksPath=/dev/null`, ensuring no hooks execute during the fetch. Fetched branches are available for review before any merge. The developer can also fetch mid-session.

**No-git mode.** For projects not in a git repository, or when `--no-git` is passed to `agentbox init`, the project directory is mounted directly into the agent container and the git bundle/output mechanism is skipped.

---

## 5. CLI Interface

### 5.1 Command Reference

| Group | Commands |
|-------|----------|
| Setup | `build [HARNESS...]`, `update [HARNESS...]`, `preset list`, `preset edit <proxy\|agent> [name]`, `preset copy <src> <dst>` |
| Project lifecycle | `init [DIR] [--preset <name>] [--name <name>] [--branch <branch>] [--no-git] [--ro-mount SRC[:DST]] [--rw-mount SRC[:DST]] [--start]`, `start [--name NAME] [-- CMD]`, `stop [--name NAME]`, `remove [--name NAME]` |
| Monitoring | `logs [--name NAME]`, `web [--name NAME]` |
| Egress control | `allow <host> [--name NAME]`, `deny <host> [--name NAME]` |
| Proxy management | `proxy-reload [--name NAME]`, `proxy-restart [--name NAME]` |
| Reference mounts | `mount list [--name NAME]`, `mount add [-w] <SRC[:DST]> [--name NAME]`, `mount remove <DST> [--name NAME]` |
| Observation | `status`, `list` / `ls` |
| Maintenance | `remote-cleanup` |

### 5.2 Key Command Behaviors

`agentbox init` creates a timestamped (or named) session directory under `.agentbox/sessions/`, copies the chosen preset's `proxy.yaml` (without its `environment` field) to the session directory, extracts any inline dotfiles from the preset's `agent.yaml`, creates a git bundle of the current branch (unless `--no-git`), initialises the bare output repository with read-only hooks and config, registers a git remote, and generates a unified `compose.yaml`. The session is optionally launched immediately if `--start` is passed.

`agentbox start` starts the proxy in the background (waiting for its health check), then runs a new agent container interactively. The `compose.yaml` is persistent — edits made directly to it are preserved across restarts. Any arguments after `--` are forwarded to the agent entrypoint and override what gets exec'd (e.g., `agentbox start -- tmux` or `agentbox start -- bash`). When the agent container exits, output is auto-fetched. Multiple `agentbox start` calls on the same session run independent agent containers concurrently.

`agentbox allow` and `agentbox deny` append or remove entries from `extra_allowed_hosts` in the session's `proxy.yaml`, then trigger a hot-reload of the proxy configuration without restarting the container or dropping active connections.

`agentbox build` and `agentbox update` build container images. Both accept optional harness names to build specific harness images (e.g., `agentbox build claude opencode`). `update` rebuilds without cache.

`agentbox remote-cleanup` removes stale `agentbox-*` git remotes that no longer have a corresponding session directory.

All commands that operate on a specific session accept `--name <name>`. If the project has exactly one session, `--name` is optional and the session is auto-detected.

---

## 6. Configuration

### 6.1 Session Directory Layout

Each session is fully self-contained under `.agentbox/sessions/<name>/`.

| File | Purpose |
|------|---------|
| `compose.yaml` | Unified Compose file (volumes, ports, env, runtime) — user-editable, regenerated on `agentbox init` |
| `proxy.yaml` | Per-session provider config and allowlist (no `environment` field) — user-owned after init |
| `dotfiles/` | Extracted dotfiles from preset `agent.yaml` |
| `source.bundle` | Read-only git snapshot of the source branch |
| `output.git/` | Bare repo output barrier — agent pushes here |

### 6.2 Preset Configuration

Presets are stored at `$AGENTBOX_HOME/presets/<name>/` (built-in) or `$AGENTBOX_HOME/custom/presets/<name>/` (user-defined, takes precedence). Each preset contains:

- **`proxy.yaml`** — Provider configuration, allowlist, trusted certificates, environment variables, logging settings.
- **`agent.yaml`** — Agent container image, environment variables, dotfiles injected into the agent's home directory.

Custom presets in `custom/presets/` override built-in presets of the same name. When a preset file is not found in the selected preset directory, the `default` preset is used as fallback.

The agent harness is determined by the preset: the `agent_image` field specifies the container image (which has the harness binary installed), and `AGENT_HARNESS` in the environment specifies which binary to exec. Each harness has a dedicated container image built from its own Containerfile.

### 6.3 Provider Configuration (`proxy.yaml`)

```yaml
providers:
  - name: anthropic
    enabled: true
    credential_type: static       # "static" (API key) or "oauth" (Google OAuth)
    api_key_env: ANTHROPIC_API_KEY # env var on the proxy side holding the real key
    # api_key_file: ~/secrets/key  # alternative: read key from file (takes precedence)
    inject_header: x-api-key       # HTTP header to inject the credential into
    inject_prefix: ""              # prefix before the credential value (e.g., "Bearer ")
    allowed_hosts:                 # hosts this provider's requests may reach
      - api.anthropic.com
    path_prefixes:                 # only inject credentials on requests matching these paths (fnmatch patterns)
      - /v1/messages
      - /v1/messages/*
      - /v1/complete
      - /v1/models
      - /v1/models/*

  - name: vertex
    enabled: true
    credential_type: oauth
    metadata_server: true          # start fake GCE metadata server
    inject_header: Authorization
    inject_prefix: "Bearer "
    replace_token: "dummy-replaced-by-proxy"  # match this token value before replacing
    allowed_hosts:
      - "aiplatform.googleapis.com"
      - "*-aiplatform.googleapis.com"
    path_prefixes:
      - "/v1/projects/${VERTEX_PROJECT_ID}/locations/${VERTEX_REGION}/publishers/*"

# Volumes to mount into the proxy container for credentials.
# proxy_volumes:
#   - src: "~/.config/gcloud"
#     dst: "/root/.config/gcloud"

# Custom CA certificates (filenames from custom/certs/)
# trusted_certificates:
#   - my-internal-ca.crt

# Additional egress hosts (exact or *.wildcard)
extra_allowed_hosts: []

# Proxy container environment variables
# environment:
#   VERTEX_PROJECT_ID: my-project
#   VERTEX_REGION: global

logging:
  log_request_headers: true
  log_response_headers: false
  log_bodies: false
```

**Provider fields:**

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Provider identifier |
| `enabled` | Yes | Whether the provider is active |
| `credential_type` | Yes | `static` (API key from env/file) or `oauth` (Google OAuth token) |
| `api_key_env` | No | Environment variable holding the API key (static type) |
| `api_key_file` | No | Path to file containing the API key; takes precedence over env var (static type) |
| `inject_header` | Yes | HTTP header where the credential is injected |
| `inject_prefix` | No | String prepended to the credential value (e.g., `"Bearer "`) |
| `replace_token` | No | If set, only inject when this token value is found in the header (oauth type) |
| `metadata_server` | No | Start fake GCE metadata server for this provider (oauth type) |
| `allowed_hosts` | Yes | Hostname patterns (fnmatch) that this provider's requests may reach |
| `path_prefixes` | No | Only inject credentials on requests whose path matches one of these fnmatch patterns |

**Top-level proxy.yaml fields:**

| Field | Required | Description |
|-------|----------|-------------|
| `proxy_volumes` | No | List of `{src, dst}` volume mounts for the proxy container (e.g., credential directories). All mounts are read-only. `src` supports `~` expansion. |
| `extra_allowed_hosts` | No | Additional egress hostnames (exact or fnmatch wildcard patterns) |
| `trusted_certificates` | No | Filenames from `custom/certs/` to install into the proxy's system trust store |

### 6.4 Agent Configuration (`agent.yaml`)

```yaml
agent_image: localhost/agentbox-agent-claude:latest

environment:
  AGENT_HARNESS: claude
  AGENT_HARNESS_ARGS: --dangerously-skip-permissions

dotfiles:
  .gitconfig: |
    [user]
        name = Agent
        email = agent@agentbox
  .bashrc: |
    export PS1="\u@agentbox:\w\$ "
```

### 6.5 Custom CA Certificates

Custom CA certificates can be placed in `$AGENTBOX_HOME/custom/certs/`. When listed in `trusted_certificates` in `proxy.yaml`, they are installed into the proxy's system trust store at startup so the proxy trusts upstream servers using these CAs.

---

## 7. Security Properties

| Property | Mechanism | Guarantee |
|----------|-----------|-----------|
| Real API keys never enter agent container | Proxy-side env/files; agent sees `dummy` placeholder | Keys absent from agent process memory and filesystem |
| GCP long-lived credentials never enter agent container | Fake metadata server returns dummy tokens; real tokens obtained and injected by proxy | SA key / refresh token stays on proxy side |
| Agent cannot make arbitrary internet connections | Podman `internal: true` network; no default gateway | All external TCP must transit proxy |
| Agent can only reach allowlisted API endpoints | Proxy enforces allowlist per request | Non-listed hosts receive HTTP 403; upstream connection never opened |
| Host `.git/` is never accessible to agent | Source delivered as git bundle; no host git mount | No filesystem path to host's `.git/` exists in the container |
| Agent cannot plant executable hooks in host git | `hooks/` is bind-mounted read-only over the writable repo mount; auto-fetch uses `core.hooksPath=/dev/null` | Kernel-enforced read-only mount prevents writes regardless of process permissions; hooks do not execute on fetch |
| Agent cannot modify output repo git config | `config` is bind-mounted read-only over the writable repo mount | Prevents agent from setting `core.hooksPath` or other config to bypass protections |
| All agent network traffic is auditable | Proxy TLS termination; structured JSON access log | Every request, including blocked ones, is logged with URL and status |
| Agentbox network isolation requires no host privilege | Podman internal networks via container runtime namespaces | No `sudo`, no host iptables or nftables changes needed |

---

## 8. Operational Workflows

### 8.1 Starting a Project Agentbox

```bash
cd ~/projects/my-app

# Init and launch in a single step:
agentbox init --start                    # uses default preset

# Or specify a preset:
agentbox init --preset claude-vertex --start

# Or init first, then start separately:
agentbox init --name mywork
agentbox start --name mywork
```

### 8.2 During a Session

```bash
agentbox allow pypi.org                  # add a host to the egress allowlist (hot-reloads proxy)
agentbox deny pypi.org                   # remove it (hot-reloads proxy)

agentbox logs                            # tail the JSON access log from the proxy
agentbox web                             # print the mitmweb traffic-monitor URL

agentbox mount add ~/libs/shared-lib     # add a read-only reference mount
agentbox mount add -w ~/data/scratch     # add a writable reference mount
agentbox start                           # restart session to apply new mount

agentbox list                            # list all sessions for this project with status
agentbox status                          # list all running agentbox containers across projects
```

### 8.3 Retrieving Agent Output

```bash
# Inside the agent container, the agent runs:
#   git push

# Output is auto-fetched from the output repo when the session ends (agent container exits).
# The CLI prints the available remote branches and suggests a merge command.

# To fetch mid-session without stopping:
git fetch agentbox-mywork

git log agentbox-mywork/agent-work       # review commit history
git diff HEAD agentbox-mywork/agent-work # review all changes before merging
git merge agentbox-mywork/agent-work     # merge after review

agentbox stop                            # tear down containers and networks
agentbox remove                          # stop, delete session dir, output repo, and git remote
```

### 8.4 Maintenance

```bash
agentbox remote-cleanup                  # remove stale agentbox-* git remotes with no matching session
agentbox proxy-reload                    # reload proxy config without restart
agentbox proxy-restart                   # restart proxy container (waits for health check)
```

---

## 9. Known Limitations

**Data exfiltration via LLM prompts.** A compromised agent can include the contents of sensitive files in its prompts to any allowed inference endpoint. The inference provider receives this data. This is not addressable at the network layer because the traffic is legitimate use of the allowed endpoint. Mitigation: use minimal-scope API keys; review session transcripts; do not allow the agent to access files outside the project scope.

**Access token window (Vertex AI).** A 1-hour OAuth access token is present in the agent container's process environment. An agent that reads and exfiltrates this token has up to 1 hour to use it, limited to the IAM roles bound to the service account. Mitigation: bind the service account to the minimum required roles (e.g., `roles/aiplatform.user` only).

**DNS resolution of external hostnames.** The agent container can resolve external hostnames via the container runtime's DNS resolver, but cannot TCP-connect to them (no default gateway). DNS-based data exfiltration (encoding data in query labels sent to an attacker-controlled nameserver) is not blocked. Mitigation: configure a restricted DNS resolver in the agent container and add `/etc/hosts` entries for all required hostnames, disabling recursive resolution.

**`core.hooksPath` set in tracked configuration files.** If the project tracks a file (e.g., `.gitconfig`, `.husky/.huskyrc`) that sets `core.hooksPath = ./hooks`, and the agent adds a malicious executable script to that directory, the script becomes part of the git commit and could execute on the host after the developer merges and runs a git operation. The read-only bind mount of `output.git/hooks/` and `output.git/config` does not protect against this because the malicious script lives in the tracked working tree, not in the bare repo's hooks directory. Mitigation: inspect all new or modified scripts in hook-related directories before merging the fetched branch.

**proxychains LD_PRELOAD bypass by static binaries.** proxychains intercepts socket calls by injecting a shared library via `LD_PRELOAD`, which applies only to dynamically linked executables. A static binary (one that does not use libc's dynamic linker) would bypass proxychains and, if it had a way to route to the internet, could make unmediated connections. In practice, no common agent harness ships as a fully static binary; this is noted as a residual risk for custom harness configurations.
