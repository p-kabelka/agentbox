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
- Claude Code installed at `~/.local/bin/claude`

---

## Setup

```bash
git clone <this-repo> ~/.local/share/agentbox

# add the project to your PATH
echo 'export PATH="$HOME/.local/share/agentbox/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
# or symlink the binary
ln -s -r ~/.local/share/agentbox/bin/agentbox ~/.local/bin

# Build container images
agentbox build
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

# Initialise a session and launch immediately
agentbox init --start

# The agent has access to your current branch (read-only bundle).
# When it's done, push its work:
#   git push output HEAD:my-feature

# When the agent container exits, output is fetched automatically:
#   git fetch agentbox-<name>
#   git merge agentbox-<name>/my-feature
```

---

## Usage

### Session lifecycle

```bash
agentbox init [--name NAME] [--preset NAME] [--harness BINARY] [--branch BRANCH] [--start]
agentbox start [--name NAME] [-- CMD]        # launch agent harness (or CMD, e.g. -- bash, -- tmux)
agentbox stop  [--name NAME]                 # stop containers
agentbox remove [--name NAME]                # stop, delete session, output repo, and git remote
```

`--name` defaults to a timestamp if not specified. If a project has exactly one session, it is auto-detected. `agentbox start` can be called multiple times on the same session to run independent agent containers concurrently. Because agents run in krun microVMs, `podman exec` cannot reach a running container — use `agentbox start -- bash` to open a shell in a new container instead.

### Monitoring

```bash
agentbox logs [--name NAME]    # tail structured JSON access log from the proxy
agentbox web  [--name NAME]    # print the mitmweb traffic-monitor URL
agentbox list                  # list sessions for this project with status
agentbox status                # list all running agentbox containers
```

### Egress control

```bash
agentbox allow pypi.org [--name NAME]   # add host to allowlist (restarts proxy)
agentbox deny  pypi.org [--name NAME]   # remove host from allowlist
```

### Reference mounts

Mount additional projects read-only at `/context/<name>` inside the agent:

```bash
agentbox mount add ~/libs/shared-lib [--name NAME]   # persists in mounts.yaml
agentbox mount remove shared-lib     [--name NAME]
agentbox mount list                  [--name NAME]
agentbox start                                        # restart to apply
```

### Retrieving output

Output is fetched automatically when the session ends. To fetch mid-session:

```bash
git fetch agentbox-<name>
git log agentbox-<name>/my-feature
git diff HEAD agentbox-<name>/my-feature
git merge agentbox-<name>/my-feature
```

---

## Configuration

### Providers

Each session has its own `config/proxy.yaml` at `.agentbox/sessions/<name>/config/proxy.yaml`, copied from the preset on `agentbox init`. Edit it to enable providers:

```yaml
providers:
  - name: anthropic
    enabled: true          # flip this
    api_key_env: ANTHROPIC_API_KEY
    inject_header: x-api-key
    inject_prefix: ""
    allowed_hosts:
      - api.anthropic.com
```

The real API key is read from the proxy container's environment - never from the agent.

### Vertex AI

```yaml
providers:
  - name: vertex
    enabled: true
    metadata_server: true
    allowed_hosts:
      - "*-aiplatform.googleapis.com"

environment:
  GOOGLE_CLOUD_PROJECT: my-gcp-project
  VERTEX_PROJECT_ID: my-gcp-project
  VERTEX_REGION: us-east5
```

Place GCP credentials at `$AGENTBOX_HOME/secrets/credentials.json`, or let the proxy use your local gcloud ADC (`~/.config/gcloud` is mounted read-only when Vertex is enabled).

### Presets

Presets live at `$AGENTBOX_HOME/presets/<name>/` and contain `proxy.yaml` (provider config) and optionally `agent.yaml` (environment overrides and dotfiles).

```bash
agentbox preset list
agentbox preset copy default mypreset
agentbox preset edit proxy mypreset    # edit provider config
agentbox preset edit agent mypreset    # edit agent env / dotfiles
agentbox init --preset mypreset --start
```

### Agent harness

The default harness is `claude`. Override per session:

```bash
agentbox init --harness opencode --start
```

Or set it in the preset's `agent.yaml`:

```yaml
environment:
  AGENT_HARNESS: opencode
  AGENT_HARNESS_ARGS: --some-flag
```

To install a harness not in the base image, derive from it:

```dockerfile
FROM localhost/agentbox-agent:latest
RUN npm install -g opencode
```

Then `agentbox build`.

---

## Updating

```bash
git -C ~/.local/share/agentbox pull
agentbox update    # rebuild images without cache
```
