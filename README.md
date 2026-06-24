# agentbox

Isolated, credential-safe workspaces for AI coding agents. Each agentbox runs the agent in a microVM container with no real API keys, no direct internet access, and no write access to your original host git repository.

See [ARCHITECTURE.md](ARCHITECTURE.md) for design rationale and [SPEC.md](SPEC.md) for the full technical specification.

---

## How it works

Two containers run per session: a **proxy** (holds real credentials, enforces an egress allowlist, logs all traffic) and an **agent** (holds no credentials, has no internet route). All agent network traffic transits mitmproxy. Your project is delivered to the agent as a read-only git bundle; output comes back through a bare repository with immutable hooks.

---

## Prerequisites

- Podman with `podman compose`
- Python 3 with PyYAML (`pip install pyyaml`, `dnf install python3-pyyaml`)
- `krun` / `crun-vm` for VM-level isolation (recommended; agent container uses `runtime: krun`)

---

## Setup

```bash
git clone <this-repo> ~/.local/share/agentbox

# add the project to your PATH
echo 'export PATH="$HOME/.local/share/agentbox/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
# or symlink the binary
ln -s -r ~/.local/share/agentbox/bin/agentbox ~/.local/bin

# Build container images (builds proxy, base agent, and all harness images)
agentbox build
# Or build specific harness images only:
agentbox build claude opencode
```

Create a configuration preset for an agentbox:

```bash
# Enable a provider in the default preset - set enabled: true
agentbox preset edit proxy
# or create a custom preset
agentbox preset copy default new-preset
agentbox preset edit proxy new-preset
agentbox preset edit agent new-preset
```

---

## Quickstart

```bash
cd ~/projects/my-app

# Initialise a session and launch tmux with the agent harness
agentbox init --preset claude-vertex
agentbox start -- tmux -u new-session -s agent 'bash -l' ';' send-keys -t agent 'agent' Enter

# The agent has access to your current branch (read-only bundle).
# When it's done, push its work:
#   git push

# When the agent container exits, output is fetched automatically:
#   git fetch agentbox-<name>
#   git merge agentbox-<name>/my-feature
```

Some caveats: the start command is entirely customizable on the command line. By default if you don't provide a command to run it will launch the agent harness configured in the preset. The presets usually have some configuration in .bashrc that pre-configures the agent harness to be usable from the sandbox right way. Therefore it is almost always more preferable to use the tmux session start command echoed by the init command.

---

## Usage

### Session lifecycle

```bash
agentbox init   [--name NAME] [--preset NAME] [--branch BRANCH] [--no-git] \
                [--ro-mount SRC[:DST]] [--rw-mount SRC[:DST]] [--start]
agentbox start  [--name NAME] [-- CMD]   # launch agent harness (or CMD, e.g. -- bash, -- tmux)
agentbox stop   [--name NAME]            # stop containers
agentbox remove [--name NAME]            # stop, delete session, output repo, and git remote
```

`--name` defaults to a timestamp if not specified. If a project has exactly one session, it is auto-detected. `agentbox start` can be called multiple times on the same session to run independent agent containers concurrently. Because agents run in krun microVMs, `podman exec` cannot reach a running container — use `agentbox start -- bash` to open a shell in a new container instead.

Everywhere where `--name` can be used, the parameter `--session` can also be used when you provide the session global ID found in `agentbox list --all`.

### Monitoring

```bash
agentbox logs [--name NAME] [--tail LAST_N_LINES]   # tail structured JSON access log from the proxy
agentbox web  [--name NAME]                         # print the mitmweb traffic-monitor URL
agentbox list [--all]                               # list sessions (optionally across all projects)
agentbox status                                     # list all running agentbox containers
```

### Egress control

```bash
agentbox allow pypi.org [--name NAME]   # add host to allowlist (hot-reloads proxy config)
agentbox deny  pypi.org [--name NAME]   # remove host from allowlist
```

### Proxy management

```bash
agentbox proxy-reload  [--name NAME]    # reload proxy config without restarting
agentbox proxy-restart [--name NAME]    # restart proxy container (waits for health check)
```

### Reference mounts

Mount additional projects at `/context/<name>` inside the agent:

```bash
agentbox mount add ~/libs/shared-lib [--name NAME]   # read-only (default)
agentbox mount add -w ~/data/scratch [--name NAME]   # writable
agentbox mount remove shared-lib     [--name NAME]
agentbox mount list                  [--name NAME]
agentbox start                                       # restart to apply
```

### Retrieving output

Output is fetched automatically when the session ends. To fetch mid-session:

```bash
git fetch agentbox-<name>
git log agentbox-<name>/my-feature
git diff HEAD agentbox-<name>/my-feature
git merge agentbox-<name>/my-feature
```

### Maintenance

```bash
agentbox remote-cleanup   # remove stale agentbox-* git remotes with no matching session
```

---

## Configuration

### Providers

Each session has its own `proxy.yaml` at `.agentbox/sessions/<name>/proxy.yaml`, copied from the preset on `agentbox init`. Edit it to enable providers:

```yaml
providers:
  - name: anthropic
    enabled: true
    credential_type: static        # "static" (API key) or "oauth" (Google OAuth)
    api_key_env: ANTHROPIC_API_KEY # env var on the proxy side
    # api_key_file: ~/secrets/key  # alternative: read key from file (takes precedence)
    inject_header: x-api-key
    inject_prefix: ""
    allowed_hosts:
      - api.anthropic.com
    path_prefixes:                 # only inject credentials on matching paths
      - /v1/messages
      - /v1/messages/*
      - /v1/complete
      - /v1/models
```

The real API key is read from the proxy container's environment or a mounted secret file — never from the agent.

### Vertex AI

```yaml
providers:
  - name: vertex
    enabled: true
    credential_type: oauth
    metadata_server: true
    inject_header: Authorization
    inject_prefix: "Bearer "
    replace_token: "dummy-replaced-by-proxy"
    allowed_hosts:
      - "aiplatform.googleapis.com"
      - "*-aiplatform.googleapis.com"
    path_prefixes:
      - "/v1/projects/${VERTEX_PROJECT_ID}/locations/${VERTEX_REGION}/publishers/*"

environment:
  GOOGLE_CLOUD_PROJECT: my-gcp-project
  VERTEX_PROJECT_ID: my-gcp-project
  VERTEX_REGION: global
```

Place GCP credentials at `$AGENTBOX_HOME/secrets/credentials.json`, or let the proxy use your local gcloud ADC (`~/.config/gcloud` is mounted read-only when Vertex is enabled).

### Presets

Presets live at `$AGENTBOX_HOME/presets/<name>/` (built-in) or `$AGENTBOX_HOME/custom/presets/<name>/` (user-defined, takes precedence). Each contains `proxy.yaml` (provider config) and optionally `agent.yaml` (agent image, environment overrides, and dotfiles).

```bash
agentbox preset list
agentbox preset copy default mypreset
agentbox preset edit proxy mypreset    # edit provider config
agentbox preset edit agent mypreset    # edit agent env / dotfiles
agentbox init --preset mypreset --start
```

### Agent harness

The agent harness is configured via the preset's `agent.yaml`. Each harness has a dedicated container image with the harness binary installed. Built-in presets are available for `claude-vertex`, `opencode-vertex`, and the `default` (base image, no harness).

Set the harness in `agent.yaml`:

```yaml
agent_image: localhost/agentbox-agent-claude:latest

environment:
  AGENT_HARNESS: claude
  AGENT_HARNESS_ARGS: --dangerously-skip-permissions
```

To add a harness not already supported, create a `Containerfile.<name>` in the `agent/` directory:

```dockerfile
FROM localhost/agentbox-agent-base:latest
RUN npm install -g my-agent
```

Then `agentbox build <name>` or `agentbox build` to build all images.

---

## Updating

```bash
git -C ~/.local/share/agentbox pull
agentbox update              # rebuild all images without cache
agentbox update claude       # rebuild only the claude harness image
```
