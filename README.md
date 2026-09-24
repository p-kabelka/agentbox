# agentbox

Isolated, credential-safe workspaces for AI coding agents. Each agentbox runs the agent in a microVM container with no real API keys, no direct internet access, and no write access to your original host git repository.

See [ARCHITECTURE.md](docs/ARCHITECTURE.md) for design rationale and [SPEC.md](docs/SPEC.md) for the full technical specification.

---

## How it works

Each session has a **proxy** (holds real credentials, enforces an egress allowlist, logs all traffic) and one or more **agents** (hold no credentials, have no internet route). All agent network traffic transits mitmproxy. Your project is delivered to the agent as a read-only git bundle; output comes back through a bare repository with immutable hooks.

---

## Prerequisites

- Podman with `podman compose` (`podman-compose` 1.5.0 or newer; tmpfs syntax verified with 1.5.0)
- Python 3 with PyYAML (`pip install pyyaml`, `dnf install python3-pyyaml`)
- `krun` / `crun-vm` for VM-level isolation (recommended; agent container uses `runtime: krun`)

---

## Setup

```bash
git clone https://github.com/p-kabelka/agentbox.git ~/.local/share/agentbox

# add the project to your PATH
echo 'export PATH="$HOME/.local/share/agentbox/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
# or symlink the binary
ln -s -r ~/.local/share/agentbox/bin/agentbox ~/.local/bin

# Build container images (builds proxy, base agent, and all harness images)
agentbox build
# Or build specific images only:
agentbox build base proxy claude
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
agentbox init
agentbox start -- tmux -u new-session -s agent 'bash -l' ';' send-keys -t agent 'agent' Enter

# The agent has access to your current branch (read-only bundle).
# When it's done, push its work:
#   git push

# When the agent container exits, output is fetched automatically:
#   git fetch agentbox-<name>
#   git merge agentbox-<name>/my-feature
```

Some caveats: the start command is entirely customizable on the command line. By default if you don't provide a command to run it will launch the agent harness configured in the preset. The presets usually have some configuration in .bashrc that pre-configures the agent harness to be usable from the sandbox right way. Therefore it is almost always more preferable to use the tmux session start command echoed by the init command.

To keep the agent's workspace and home directory across starts, opt in at initialization:

```bash
agentbox init --name feature --persist
agentbox start --name feature -- bash     # first start chooses the saved command
# exit bash, then restart the same container and its filesystem
agentbox start --name feature
agentbox stop --name feature              # retain the stopped agent and proxy
agentbox remove --name feature            # delete the container and unpublished work
```

Only one `start` can own a persistent session at a time. Processes, including tmux, do not survive stopping the container. Push commits to the output remote before removing the session; auto-fetch runs after each attached exit unless `remove` has deleted the session while it was running.
After the first start, edits to `services.agent` in `compose.yaml` do not reconfigure the retained agent on the next start. Create a new session for a different agent image, environment, mounts, or command.

The default environment does not include any agent harness, so it can be used as a temporary sandbox.

Currently, for all provided agents you need to create your own preset (or modify the generated compose file) to configure them. Checkout [docs](docs/) for the specific agent harness setup guide.

---

## Usage

### Session lifecycle

```bash
agentbox init   [--name NAME] [--preset NAME] [--branch BRANCH] [--no-git] [--persist] \
                [--ro-mount SRC[:DST]] [--rw-mount SRC[:DST]] [--start]
agentbox start  [--name NAME] [-- CMD]   # launch agent harness (or CMD, e.g. -- bash, -- tmux)
agentbox stop   [--name NAME]            # stop containers
agentbox remove [--name NAME]            # stop, delete session, output repo, and git remote
```

`--name` defaults to a timestamp if not specified. If a project has exactly one session, it is auto-detected. By default, `agentbox start` can run independent agent containers concurrently; the proxy stays up until the last one exits, then `compose down` tears the project down. With `init --persist`, the agent is one retained Compose service container; `start -- CMD` is only valid before its first creation, later starts use its saved command, and a second simultaneous start fails. `stop` retains both persistent service containers; `remove` deletes them. Because agents run in krun microVMs, `podman exec` cannot reach a running container; use another ephemeral start or a separate session for parallel shells.

Everywhere where `--name` can be used, the parameter `--session` can also be used when you provide the session global ID found in `agentbox list --all`.

### Monitoring

```bash
agentbox logs [--name NAME] [--tail LAST_N_LINES]   # tail structured JSON access log from the proxy
agentbox web  [--name NAME]                         # print the mitmweb traffic-monitor URL
agentbox list [--all]                               # list sessions (optionally across all projects)
agentbox containers [--name NAME] [--json]          # list session containers and their states
agentbox status                                     # list all running agentbox containers
```

### Egress control

```bash
agentbox allow pypi.org [--name NAME]   # add host to allowlist (hot-reloads proxy config)
agentbox deny  pypi.org [--name NAME]   # remove host from allowlist
```

### Proxy management

```bash
agentbox proxy-reload  [--name NAME]    # synchronize keys and reload providers in place
agentbox proxy-restart [--name NAME]    # restart proxy, repopulate keys, and reload
```

Provider files are synchronized **only** by `start`, `proxy-reload`, and `proxy-restart`.
Edit the current session's `proxy-config/proxy.yaml` or replace a host key file, then run
`proxy-reload` to apply the change without interrupting active requests. Every `start`
synchronizes, including when it shares an existing proxy. One Compose exec transfers all
provider secrets and applies the reload. There is no file watcher.

**Direct `podman compose up` and automatic container restarts do not synchronize keys.**
Their tmpfs starts empty. Requests requiring an unavailable credential receive HTTP 503
until a synchronization command runs; healthy providers and ordinary allowlist rules
continue working. A non-empty `api_key_env` is used as a fallback when configured.

`allow` and `deny` save the edit and reload only non-provider settings. If provider changes
are pending, the CLI reports that the saved edit is not active: run `proxy-reload` once to
apply both changes. You do not need to repeat `allow` or `deny`.

### Reference mounts

Mount additional projects at `/context/<name>` when initializing a session:

```bash
agentbox init --ro-mount ~/libs/shared-lib:shared-lib --rw-mount ~/data/scratch:scratch
```

For a persistent session, set mounts during `init`. Existing containers keep their original mounts; create a new session to change them.

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

Each session has its own `$AGENTBOX_STATE/sessions/<session-id>/proxy-config/proxy.yaml`, copied from the preset on `agentbox init`. The default state directory is `~/.local/state/agentbox`. Edit the session file to enable providers:

```yaml
providers:
  - name: anthropic
    enabled: true
    credential_type: static        # "static" (API key) or "oauth" (Google OAuth)
    api_key_env: ANTHROPIC_API_KEY # env var on the proxy side
    # api_key_file: ~/secrets/key  # host source synchronized by explicit commands
    inject_header: x-api-key
    inject_prefix: ""
    request_policy:
      - host: 'api\.anthropic\.com'
        paths: ['/v1/messages(/.*)?$', '/v1/complete$', '/v1/models(/.*)?$']
```

The real API key is read from the proxy container's environment or synchronized tmpfs — never
from the agent. `api_key_file` takes precedence. Host `~` expansion is supported; relative paths
resolve against the session's project directory, even with `--session` from another directory.
Replacing a file atomically or retargeting a symlink takes effect on the next synchronization.
Keys and their content digests travel through stdin, never command arguments or Compose.
Injected headers are redacted in structured access logs. Environment changes still require
container recreation.

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
    request_policy:
      - host: '(.*-)?aiplatform\.googleapis\.com'
        paths: ['/v1/projects/${VERTEX_PROJECT_ID}/locations/${VERTEX_REGION}/publishers/.*']

proxy_volumes:
  - src: ~/.config/gcloud
    dst: /root/.config/gcloud

environment:
  GOOGLE_CLOUD_PROJECT: my-gcp-project
  VERTEX_PROJECT_ID: my-gcp-project
  VERTEX_REGION: global
```

The example mounts local gcloud ADC read-only. Alternatively, explicitly mount a service-account
file at `/oauth/credentials.json` using `proxy_volumes` and set
`GOOGLE_APPLICATION_CREDENTIALS: /oauth/credentials.json` in the preset's `environment`.
OAuth discovery retains its existing behavior and is separate from `api_key_file` synchronization.

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

## Verification

Run `python3 -m unittest discover -s tests -v` with PyYAML installed. Installing
`podman-compose==1.5.0` enables the Compose tmpfs argument check; installing the proxy's
mitmproxy dependency enables the real HTTP streaming/reload regression. The latter requires
working local TCP listeners and reports a skip when the environment cannot provide them.
